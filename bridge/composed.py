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
    if not region:
        raise RuntimeError("AZURE_SPEECH_REGION is not set")
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
        url = f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
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


def _format_sources(hits: list[Any]) -> str:
    out: list[str] = []
    for i, h in enumerate(hits, start=1):
        loc = h.section or h.title
        snippet = (h.content or "").strip().replace("\n", " ")
        if len(snippet) > 1200:
            snippet = snippet[:1200] + "..."
        out.append(f"[doc{i}] ({h.title} - {loc})\n{snippet}")
    return "\n\n".join(out)


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
    t = time.perf_counter()
    pcm = await _tts_pcm(http, text, token, region)
    if state.get("tts_first_byte_ms") is None:
        state["tts_first_byte_ms"] = (time.perf_counter() - t) * 1000.0
    if state.get("first_audio_ms") is None and state.get("turn_started") is not None:
        state["first_audio_ms"] = (time.perf_counter() - state["turn_started"]) * 1000.0
        await _safe_send(
            ws,
            {"type": "metrics", "first_audio_ms": round(state["first_audio_ms"])},
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
) -> None:
    state: dict[str, Any] = {
        "turn_started": turn_started,
        "first_audio_ms": None,
        "llm_ttft_ms": None,
        "tts_first_byte_ms": None,
    }
    http = _shared_http()
    timings: dict[str, float] = {}
    try:
        t = time.perf_counter()
        vec = await embed(query)
        timings["embed_ms"] = (time.perf_counter() - t) * 1000.0
        t = time.perf_counter()
        hits, _ = await hybrid_semantic_search(query, query_vector=vec)
        timings["search_ms"] = (time.perf_counter() - t) * 1000.0
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
    try:
        stream = await client.chat.completions.create(
            model=config.AZURE_OPENAI_CHAT_DEPLOYMENT,
            temperature=0.2,
            max_tokens=180,
            stream=True,
            messages=[
                {"role": "system", "content": config.SYSTEM_PROMPT},
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
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue
            if state["llm_ttft_ms"] is None:
                state["llm_ttft_ms"] = (time.perf_counter() - turn_started) * 1000.0
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
                    await _send_tts(ws, http, sentence, token, region, state)
                    sentences_sent += 1
    except Exception as e:  # noqa: BLE001
        logger.exception("chat stream failed")
        await _safe_send(ws, {"type": "error", "error": f"chat: {e}"})
        return

    tail = buf.strip()
    if tail:
        await _send_tts(ws, http, tail, token, region, state)

    full = "".join(full_text).strip()
    full_ms = (time.perf_counter() - turn_started) * 1000.0
    telemetry.record(
        path="composed",
        first_audio_ms=state.get("first_audio_ms"),
        full_ms=full_ms,
        llm_ttft_ms=state.get("llm_ttft_ms"),
        tts_first_byte_ms=state.get("tts_first_byte_ms"),
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
    recognizer = speechsdk.SpeechRecognizer(speech_config=sc, audio_config=audio_cfg)

    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    speaking = {"started": None}  # perf_counter at first partial of an utterance

    def _post(ev: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(events.put_nowait, ev)

    def _on_recognizing(evt: Any) -> None:
        if speaking["started"] is None and (evt.result.text or "").strip():
            speaking["started"] = time.perf_counter()
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
        while True:
            ev = await events.get()
            kind = ev.get("type")
            if kind == "partial":
                await _safe_send(
                    client,
                    {"type": "transcript.partial", "text": ev.get("text", "")},
                )
            elif kind == "final":
                text = (ev.get("text") or "").strip()
                await _safe_send(
                    client,
                    {"type": "transcript.final", "text": text},
                )
                if text:
                    asyncio.create_task(
                        _run_turn(client, text, token, region, ev["started"])
                    )
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
