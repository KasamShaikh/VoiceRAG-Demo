"""Phase 5 - Composed path: Speech SDK STT -> AOAI streaming -> Speech TTS.

Critical perf trick: as the AOAI chat stream produces text deltas, we flush
*sentence-by-sentence* to TTS instead of waiting for the full completion.
This lets us start playing audio while the model is still generating, which
is what makes the composed path competitive with Voice Live (Path A).

Auth: token-based, no keys.
  AAD bearer (cognitiveservices.azure.com)
    -> exchanged at <region>.api.cognitive.microsoft.com/sts/v1.0/issueToken
    -> 10-min Speech auth token used by Speech SDK + TTS REST.

Wire (browser <-> bridge), JSON text frames:
  client -> bridge:
    {"type":"input_audio_buffer.append","audio":<base64 PCM16 24 kHz>}
  bridge -> client:
    {"type":"transcript.partial","text":...}
    {"type":"transcript.final","text":...}
    {"type":"audio.delta","audio":<base64 raw PCM16 24 kHz>,"text":<sentence>}
    {"type":"response.done"}
    {"type":"error","error":...}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import azure.cognitiveservices.speech as speechsdk
import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import config, telemetry
from .search import embed, hybrid_semantic_search

logger = logging.getLogger("bridge.composed")
router = APIRouter()

_SENTENCE_END = (".", "!", "?", "\n")
_MIN_SENT_LEN = 24  # don't TTS tiny fragments like "Yes."
# Phase 6: flush the FIRST sentence as soon as we see any boundary, no minimum.
# The remaining sentences use _MIN_SENT_LEN to avoid 1-2 word fragments.
_FAST_FIRST_MIN_LEN = 2


# ---- speech token (cached) -------------------------------------------------

_TOKEN_TTL = 9 * 60  # tokens last 10 min; refresh at 9.
_token_cache: dict[str, Any] = {"token": None, "region": None, "exp": 0.0}
_token_lock = asyncio.Lock()


async def _issue_speech_token(force: bool = False) -> tuple[str, str]:
    region = config.AZURE_SPEECH_REGION
    endpoint = config.AZURE_SPEECH_ENDPOINT
    if not region:
        raise RuntimeError("AZURE_SPEECH_REGION is not set")
    if not endpoint:
        raise RuntimeError("AZURE_SPEECH_ENDPOINT is not set")
    now = time.time()
    if (
        not force
        and _token_cache["token"]
        and _token_cache["region"] == region
        and _token_cache["exp"] > now
    ):
        return _token_cache["token"], region
    async with _token_lock:
        if not force and _token_cache["token"] and _token_cache["exp"] > time.time():
            return _token_cache["token"], region
        aad = config.credential().get_token(config.AZURE_VOICE_LIVE_SCOPE).token
        # AAD-based issueToken MUST hit the resource's custom-domain endpoint.
        # The regional <region>.api.cognitive.microsoft.com host is key-only
        # and returns 400 Bad Request when given an AAD bearer token.
        url = f"{endpoint.rstrip('/')}/sts/v1.0/issueToken"
        client = _shared_http()
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {aad}", "Content-Length": "0"},
        )
        r.raise_for_status()
        token = r.text
        _token_cache.update(
            {"token": token, "region": region, "exp": time.time() + _TOKEN_TTL}
        )
        return token, region


# ---- shared httpx client (Phase 6) -----------------------------------------

_http_client: httpx.AsyncClient | None = None


def _shared_http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
        )
    return _http_client


async def prewarm() -> None:
    """Open Speech token + warm up TTS DNS/TLS at startup."""
    try:
        await _issue_speech_token(force=True)
        logger.info("composed: speech token prewarmed")
    except Exception:  # noqa: BLE001
        logger.warning("composed prewarm failed", exc_info=True)


async def shutdown() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


# ---- TTS (REST, raw PCM streamed) ------------------------------------------


def _ssml(text: str) -> str:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<speak version='1.0' xml:lang='en-US' "
        "xmlns:mstts='http://www.w3.org/2001/mstts'>"
        f"<voice name='{config.AZURE_SPEECH_TTS_VOICE}'>"
        f"<prosody rate='+5%'>{safe}</prosody>"
        "</voice></speak>"
    )


async def _tts_pcm(
    http: httpx.AsyncClient, text: str, token: str, region: str
) -> bytes:
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": f"raw-{config.AZURE_SPEECH_AUDIO_RATE // 1000}khz-16bit-mono-pcm",
        "User-Agent": "voicerag-bridge",
    }
    r = await http.post(url, content=_ssml(text).encode("utf-8"), headers=headers)
    r.raise_for_status()
    return r.content


# ---- RAG pipeline (per finalized utterance) --------------------------------


# Tuned defaults for Path B optimization: moderate context expansion while
# preserving low prefill latency.
_LLM_TOP_K = config.COMPOSED_LLM_TOP_K
_LLM_SNIPPET_MAX = config.COMPOSED_SNIPPET_MAX


def _format_sources(hits: list[Any]) -> str:
    out: list[str] = []
    for i, h in enumerate(hits[:_LLM_TOP_K], start=1):
        snippet = (h.content or "").strip().replace("\n", " ")
        if len(snippet) > _LLM_SNIPPET_MAX:
            snippet = snippet[:_LLM_SNIPPET_MAX] + "..."
        out.append(f"[doc{i}] {h.title} - {snippet}")
    return "\n\n".join(out)


async def _retrieve_hits(query: str) -> tuple[list[Any], dict[str, float]]:
    """Hybrid primary retrieval + small vector expansion for Path B.

    This keeps Path B closer to customer Path C retrieval quality while
    controlling latency through lower default Ks than Path C.
    """
    timings: dict[str, float] = {}
    t = time.perf_counter()
    vec = await embed(query)
    timings["embed_ms"] = (time.perf_counter() - t) * 1000.0

    t = time.perf_counter()
    primary_task = asyncio.create_task(
        hybrid_semantic_search(
            query,
            top_k=config.COMPOSED_PRIMARY_K,
            query_vector=vec,
        )
    )
    expansion_task: asyncio.Task | None = None
    if config.COMPOSED_EXPANSION_K > 0:
        expansion_task = asyncio.create_task(
            hybrid_semantic_search(
                query,
                top_k=config.COMPOSED_EXPANSION_K,
                vector_k=config.COMPOSED_EXPANSION_VECTOR_K,
                query_vector=vec,
            )
        )
    primary, _ = await primary_task
    expansion: list[Any] = []
    if expansion_task is not None:
        expansion, _ = await expansion_task
    timings["search_ms"] = (time.perf_counter() - t) * 1000.0

    merged = list(primary)
    seen = {h.id for h in primary}
    target = config.COMPOSED_PRIMARY_K + config.COMPOSED_EXPANSION_K
    for h in expansion:
        if h.id in seen:
            continue
        merged.append(h)
        seen.add(h.id)
        if len(merged) >= target:
            break
    return merged, timings


async def _safe_send(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:  # noqa: BLE001
        pass


async def _send_tts(
    ws: WebSocket,
    http: httpx.AsyncClient,
    text: str,
    token: str,
    region: str,
    state: dict[str, Any],
) -> None:
    if state.get("cancelled"):
        return
    t = time.perf_counter()
    pcm = await _tts_pcm(http, text, token, region)
    if state.get("cancelled"):
        return
    if state.get("tts_first_byte_ms") is None:
        state["tts_first_byte_ms"] = (time.perf_counter() - t) * 1000.0
    if state.get("first_audio_ms") is None and state.get("turn_started") is not None:
        now = time.perf_counter()
        state["first_audio_ms"] = (now - state["turn_started"]) * 1000.0
        if state.get("final_started") is not None:
            state["final_to_first_audio_ms"] = (now - state["final_started"]) * 1000.0
        await _safe_send(
            ws,
            {
                "type": "metrics",
                "first_audio_ms": round(state["first_audio_ms"]),
                "final_to_first_audio_ms": round(state.get("final_to_first_audio_ms"))
                if state.get("final_to_first_audio_ms") is not None
                else None,
                "breakdown": {
                    "embed_ms": round(state.get("embed_ms") or 0),
                    "search_ms": round(state.get("search_ms") or 0),
                    "llm_ttft_ms": round(state.get("llm_ttft_ms") or 0),
                    "tts_first_byte_ms": round(state["tts_first_byte_ms"]),
                },
            },
        )
    await _safe_send(
        ws,
        {
            "type": "audio.delta",
            "audio": base64.b64encode(pcm).decode(),
            "text": text,
        },
    )


async def _run_turn(
    ws: WebSocket,
    query: str,
    token: str,
    region: str,
    turn_started: float,
    final_started: float | None,
    state: dict[str, Any] | None = None,
    speculative: tuple[list[Any], dict[str, float]] | None = None,
) -> None:
    if state is None:
        state = {}
    state.setdefault("turn_started", turn_started)
    state.setdefault("final_started", final_started)
    state.setdefault("first_audio_ms", None)
    state.setdefault("final_to_first_audio_ms", None)
    state.setdefault("llm_ttft_ms", None)
    state.setdefault("tts_first_byte_ms", None)
    state.setdefault("cancelled", False)
    http = _shared_http()
    tts_q: asyncio.Queue[str | None] = asyncio.Queue()
    tts_worker: asyncio.Task | None = None

    async def _tts_worker() -> None:
        while True:
            text = await tts_q.get()
            try:
                if text is None:
                    return
                await _send_tts(ws, http, text, token, region, state)
            except Exception as e:  # noqa: BLE001
                logger.exception("tts worker failed")
                state["cancelled"] = True
                await _safe_send(ws, {"type": "error", "error": f"tts: {e}"})
            finally:
                tts_q.task_done()

    try:
        if speculative is not None:
            hits, _spec_timings = speculative
            # Speculative retrieval runs while user is speaking, so it should not
            # inflate silence-first-audio breakdown. Use near-zero timings here.
            if not hits:
                hits, timings = await _retrieve_hits(query)
            else:
                timings = {"embed_ms": 0.0, "search_ms": 0.0}
        else:
            hits, timings = await _retrieve_hits(query)
        state["embed_ms"] = timings["embed_ms"]
        state["search_ms"] = timings["search_ms"]
    except Exception as e:  # noqa: BLE001
        logger.exception("retrieval failed")
        await _safe_send(ws, {"type": "error", "error": f"retrieval: {e}"})
        return

    if not hits:
        text = "I don't have that information in the knowledge base."
        await _send_tts(ws, http, text, token, region, state)
        full_ms = (time.perf_counter() - turn_started) * 1000.0
        telemetry.record(
            path="composed",
            first_audio_ms=state.get("first_audio_ms"),
            full_ms=full_ms,
            **timings,
        )
        await _safe_send(
            ws, {"type": "response.done", "text": text, "full_ms": round(full_ms)}
        )
        return

    user_msg = (
        f"Question: {query}\n\n"
        f"Sources:\n{_format_sources(hits)}\n\n"
        "Answer concisely (under 60 spoken words) using ONLY the sources above. "
        "Cite sources inline using the [doc#] tags."
    )

    client = config.aoai_client()
    t_llm = time.perf_counter()
    try:
        stream = await client.chat.completions.create(
            model=config.AZURE_OPENAI_CHAT_DEPLOYMENT,
            temperature=0.2,
            max_tokens=180,
            stream=True,
            messages=[
                {"role": "system", "content": config.COMPOSED_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("chat stream open failed")
        await _safe_send(ws, {"type": "error", "error": f"chat: {e}"})
        return

    full_text: list[str] = []
    buf = ""
    sentences_sent = 0
    try:
        async for chunk in stream:
            if state.get("cancelled"):
                break
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue
            if state["llm_ttft_ms"] is None:
                # Pure LLM time: from chat.create() call to first token.
                state["llm_ttft_ms"] = (time.perf_counter() - t_llm) * 1000.0
            full_text.append(delta)
            buf += delta
            # flush every sentence boundary; first sentence flushes ASAP.
            while True:
                idxs = [buf.find(c) for c in _SENTENCE_END]
                idxs = [i for i in idxs if i != -1]
                if not idxs:
                    break
                cut = min(idxs) + 1
                min_len = _FAST_FIRST_MIN_LEN if sentences_sent == 0 else _MIN_SENT_LEN
                if cut < min_len:
                    next_idx = -1
                    for c in _SENTENCE_END:
                        j = buf.find(c, cut)
                        if j != -1 and (next_idx == -1 or j < next_idx):
                            next_idx = j
                    if next_idx == -1:
                        break
                    cut = next_idx + 1
                sentence = buf[:cut].strip()
                buf = buf[cut:]
                if sentence:
                    if sentences_sent == 0:
                        await _send_tts(ws, http, sentence, token, region, state)
                    else:
                        if tts_worker is None:
                            tts_worker = asyncio.create_task(_tts_worker())
                        await tts_q.put(sentence)
                    sentences_sent += 1
    except Exception as e:  # noqa: BLE001
        logger.exception("chat stream failed")
        await _safe_send(ws, {"type": "error", "error": f"chat: {e}"})
        if tts_worker is not None:
            tts_worker.cancel()
            try:
                await tts_worker
            except asyncio.CancelledError:
                pass
        return

    tail = buf.strip()
    if tail and not state.get("cancelled"):
        if sentences_sent == 0:
            await _send_tts(ws, http, tail, token, region, state)
        else:
            if tts_worker is None:
                tts_worker = asyncio.create_task(_tts_worker())
            await tts_q.put(tail)

    if tts_worker is not None:
        await tts_q.put(None)
        await tts_q.join()
        await tts_worker

    if state.get("cancelled"):
        # do not emit response.done; client already stopped this turn
        return

    full = "".join(full_text).strip()
    full_ms = (time.perf_counter() - turn_started) * 1000.0
    telemetry.record(
        path="composed",
        first_audio_ms=state.get("first_audio_ms"),
        full_ms=full_ms,
        llm_ttft_ms=state.get("llm_ttft_ms"),
        tts_first_byte_ms=state.get("tts_first_byte_ms"),
        extra={"final_to_first_audio_ms": state.get("final_to_first_audio_ms")},
        **timings,
    )
    await _safe_send(
        ws,
        {
            "type": "response.done",
            "text": full,
            "full_ms": round(full_ms),
        },
    )


# ---- WebSocket endpoint ----------------------------------------------------


@router.websocket("/composed/ws")
async def composed_ws(client: WebSocket) -> None:
    await client.accept()
    loop = asyncio.get_running_loop()

    try:
        token, region = await _issue_speech_token()
    except Exception as e:  # noqa: BLE001
        logger.exception("speech token failed")
        await _safe_send(client, {"type": "error", "error": f"token: {e}"})
        await client.close()
        return

    fmt = speechsdk.audio.AudioStreamFormat(
        samples_per_second=config.AZURE_SPEECH_AUDIO_RATE,
        bits_per_sample=16,
        channels=1,
    )
    push_stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
    audio_cfg = speechsdk.audio.AudioConfig(stream=push_stream)
    sc = speechsdk.SpeechConfig(auth_token=token, region=region)
    sc.speech_recognition_language = config.AZURE_SPEECH_STT_LANGUAGE
    sc.set_property(
        speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
        str(config.COMPOSED_STT_SEGMENTATION_MS),
    )
    recognizer = speechsdk.SpeechRecognizer(speech_config=sc, audio_config=audio_cfg)

    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    speaking = {"started": None}  # perf_counter at first partial of an utterance
    current: dict[str, Any] = {
        "task": None,
        "state": None,
        "spec_task": None,
        "spec_cache": None,
    }

    def _post(ev: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(events.put_nowait, ev)

    def _on_recognizing(evt: Any) -> None:
        text = (evt.result.text or "").strip()
        if speaking["started"] is None and text:
            speaking["started"] = time.perf_counter()
            # Barge-in: a new utterance has begun while we may still be
            # synthesising the previous answer. Signal the consumer to
            # cancel any in-flight turn and clear the client's playback.
            _post({"type": "barge_in"})
        _post({"type": "partial", "text": evt.result.text or ""})

    def _on_recognized(evt: Any) -> None:
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
            started = speaking["started"] or time.perf_counter()
            speaking["started"] = None
            _post({"type": "final", "text": evt.result.text or "", "started": started})

    def _on_canceled(evt: Any) -> None:
        _post({"type": "canceled", "details": getattr(evt, "error_details", str(evt))})

    recognizer.recognizing.connect(_on_recognizing)
    recognizer.recognized.connect(_on_recognized)
    recognizer.canceled.connect(_on_canceled)
    recognizer.start_continuous_recognition_async()

    async def _consume() -> None:
        async def _speculate(query: str) -> None:
            try:
                hits, timings = await _retrieve_hits(query)
                current["spec_cache"] = {
                    "query": query,
                    "hits": hits,
                    "timings": timings,
                    "ts": time.perf_counter(),
                }
            except Exception:  # noqa: BLE001
                pass
            finally:
                current["spec_task"] = None

        def _use_spec_cache(
            final_text: str,
        ) -> tuple[list[Any], dict[str, float]] | None:
            cache = current.get("spec_cache")
            if not cache:
                return None
            age_ms = (time.perf_counter() - cache.get("ts", 0.0)) * 1000.0
            if age_ms > config.COMPOSED_SPEC_MAX_AGE_MS:
                return None
            q = (cache.get("query") or "").strip().lower()
            f = (final_text or "").strip().lower()
            if not q or not f:
                return None
            if f.startswith(q) or q.startswith(f) or (len(q) >= 24 and q in f):
                return cache.get("hits") or [], cache.get("timings") or {}
            return None

        while True:
            ev = await events.get()
            kind = ev.get("type")
            if kind == "barge_in":
                state = current.get("state")
                task = current.get("task")
                spec_task = current.get("spec_task")
                if state is not None:
                    state["cancelled"] = True
                if task is not None and not task.done():
                    task.cancel()
                if spec_task is not None and not spec_task.done():
                    spec_task.cancel()
                current["task"] = None
                current["state"] = None
                current["spec_task"] = None
                current["spec_cache"] = None
                # tell the client to stop playing buffered TTS audio
                await _safe_send(client, {"type": "playback.clear"})
            elif kind == "partial":
                partial_text = (ev.get("text") or "").strip()
                await _safe_send(
                    client,
                    {"type": "transcript.partial", "text": partial_text},
                )
                if (
                    config.COMPOSED_ENABLE_SPECULATIVE
                    and len(partial_text) >= config.COMPOSED_SPEC_PARTIAL_MIN_CHARS
                    and current.get("task") is None
                    and current.get("spec_task") is None
                ):
                    cached = current.get("spec_cache")
                    if not cached or cached.get("query") != partial_text:
                        current["spec_task"] = asyncio.create_task(
                            _speculate(partial_text)
                        )
            elif kind == "final":
                text = (ev.get("text") or "").strip()
                final_started = time.perf_counter()
                await _safe_send(
                    client,
                    {"type": "transcript.final", "text": text},
                )
                # Drop spurious tiny finals (filler noise / mic picking up
                # the bot's own audio). Real questions are >= 3 words or 12 chars.
                if not text or (len(text) < 12 and len(text.split()) < 3):
                    continue
                # if a previous turn is still running, cancel it before
                # starting the new one
                prev_state = current.get("state")
                prev_task = current.get("task")
                if prev_state is not None:
                    prev_state["cancelled"] = True
                if prev_task is not None and not prev_task.done():
                    prev_task.cancel()
                spec = _use_spec_cache(text)
                state: dict[str, Any] = {}
                current["state"] = state
                current["task"] = asyncio.create_task(
                    _run_turn(
                        client,
                        text,
                        token,
                        region,
                        ev["started"],
                        final_started,
                        state,
                        speculative=spec,
                    )
                )
                current["spec_cache"] = None
            elif kind == "canceled":
                await _safe_send(
                    client,
                    {"type": "error", "error": ev.get("details", "canceled")},
                )
                return

    consumer = asyncio.create_task(_consume())

    try:
        while True:
            msg = await client.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            payload = msg.get("text")
            data = msg.get("bytes")
            if data is not None:
                push_stream.write(data)
            elif payload is not None:
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "input_audio_buffer.append":
                    b64 = obj.get("audio")
                    if b64:
                        try:
                            push_stream.write(base64.b64decode(b64))
                        except Exception:  # noqa: BLE001
                            pass
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("composed ws pump failed")
    finally:
        try:
            push_stream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            recognizer.stop_continuous_recognition_async()
        except Exception:  # noqa: BLE001
            pass
        consumer.cancel()
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
