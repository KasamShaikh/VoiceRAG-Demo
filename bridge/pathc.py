"""Path C — HLD-faithful path: Azure Speech SDK STT (en-IN / hi-IN)
              → Azure AI Search hybrid retrieval (8 primary + 3 expansion = 11 chunks, 800-char snippets)
              → GPT-4.1-mini streaming with per-session conversation history
              → Azure Speech TTS sentence-by-sentence (en-IN / hi-IN voice).

This path follows ARCHITECTURE_DIAGRAM.md exactly (VAD block is handled by
Azure Speech SDK's built-in endpointing; FAISS is replaced by Azure AI Search).

Wire protocol: identical to /composed/ws (Path B).
  client → bridge:
    {"type":"input_audio_buffer.append","audio":<base64 PCM16 24kHz>}
    binary bytes (raw PCM16 24kHz) also accepted
  bridge → client:
    {"type":"transcript.partial","text":...}
    {"type":"transcript.final","text":...}
    {"type":"audio.delta","audio":<base64 PCM16 24kHz>,"text":<sentence>}
    {"type":"metrics","first_audio_ms":...,"breakdown":{...}}
    {"type":"response.done","text":...,"full_ms":...}
    {"type":"playback.clear"}
    {"type":"error","error":...}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from typing import Any

import azure.cognitiveservices.speech as speechsdk
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import config, telemetry
from .composed import _issue_speech_token, _shared_http
from .search import embed as _embed, hybrid_semantic_search

logger = logging.getLogger("bridge.pathc")
router = APIRouter()

_SENTENCE_END = (".", "!", "?", "\n")
_MIN_SENT_LEN = 24  # don't TTS tiny fragments on non-first sentences
_FAST_FIRST_MIN_LEN = 2  # flush first sentence as soon as any boundary appears


# ---- TTS (en-IN / hi-IN via PATH_C_TTS_VOICE) ----------------------------


def _ssml_c(text: str) -> str:
    voice = config.PATH_C_TTS_VOICE
    lang = "hi-IN" if "hi-IN" in voice else "en-IN"
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f"<speak version='1.0' xml:lang='{lang}' "
        "xmlns:mstts='http://www.w3.org/2001/mstts'>"
        f"<voice name='{voice}'>"
        f"<prosody rate='+5%'>{safe}</prosody>"
        "</voice></speak>"
    )


async def _tts_pcm_c(text: str, token: str, region: str) -> bytes:
    http = _shared_http()
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": (
            f"raw-{config.AZURE_SPEECH_AUDIO_RATE // 1000}khz-16bit-mono-pcm"
        ),
        "User-Agent": "voicerag-pathc",
    }
    r = await http.post(url, content=_ssml_c(text).encode("utf-8"), headers=headers)
    r.raise_for_status()
    return r.content


# ---- helpers ---------------------------------------------------------------


async def _safe_send(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:  # noqa: BLE001
        pass


async def _send_tts_c(
    ws: WebSocket,
    text: str,
    token: str,
    region: str,
    state: dict[str, Any],
) -> None:
    if state.get("cancelled"):
        return
    t = time.perf_counter()
    pcm = await _tts_pcm_c(text, token, region)
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


# ---- retrieval: 8 primary + 3 expansion, deduplicated, 800-char snippets ---


def _format_sources_c(hits: list[Any]) -> str:
    out: list[str] = []
    for i, h in enumerate(hits, start=1):
        snippet = (h.content or "").strip().replace("\n", " ")
        if len(snippet) > config.PATH_C_SNIPPET_MAX:
            snippet = snippet[: config.PATH_C_SNIPPET_MAX] + "..."
        out.append(f"[doc{i}] {h.title} - {snippet}")
    return "\n\n".join(out)


async def _retrieve_c(query: str) -> tuple[list[Any], dict[str, float]]:
    """Return up to PATH_C_TOP_K (11) deduplicated hits.

    Strategy mirrors the HLD:
      - primary_k (8): hybrid BM25 + vector with semantic reranker — best quality.
      - expansion_k (3): vector-only biased call (vector_k=30) — catches
        semantically related chunks that keyword overlap may have suppressed.
    Deduplication by hit.id; primary hits have priority.
    """
    timings: dict[str, float] = {}
    primary_k = config.PATH_C_TOP_K - config.PATH_C_EXPANSION_K  # 8

    t0 = time.perf_counter()
    vec = await _embed(query)
    timings["embed_ms"] = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    primary, _ = await hybrid_semantic_search(query, top_k=primary_k, query_vector=vec)
    expansion, _ = await hybrid_semantic_search(
        query, top_k=config.PATH_C_EXPANSION_K, vector_k=30, query_vector=vec
    )
    timings["search_ms"] = (time.perf_counter() - t1) * 1000.0

    # Merge: primary first, then expansion gap-fill, capped at total top_k.
    seen: set[str] = {h.id for h in primary}
    merged = list(primary)
    for h in expansion:
        if h.id not in seen:
            merged.append(h)
            seen.add(h.id)
        if len(merged) >= config.PATH_C_TOP_K:
            break

    return merged, timings


# ---- turn runner -----------------------------------------------------------


async def _run_turn_c(
    ws: WebSocket,
    query: str,
    token: str,
    region: str,
    turn_started: float,
    final_started: float | None,
    history: "deque[dict[str, str]]",
    state: dict[str, Any] | None = None,
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

    # -- Retrieval ------------------------------------------------------------
    try:
        hits, timings = await _retrieve_c(query)
        state["embed_ms"] = timings.get("embed_ms", 0.0)
        state["search_ms"] = timings.get("search_ms", 0.0)
    except Exception as e:  # noqa: BLE001
        logger.exception("pathc retrieval failed")
        await _safe_send(ws, {"type": "error", "error": f"retrieval: {e}"})
        return

    if not hits:
        fallback = "I don't have that information in the knowledge base."
        await _send_tts_c(ws, fallback, token, region, state)
        full_ms = (time.perf_counter() - turn_started) * 1000.0
        telemetry.record(
            path="pathc",
            first_audio_ms=state.get("first_audio_ms"),
            full_ms=full_ms,
            embed_ms=state.get("embed_ms"),
            search_ms=state.get("search_ms"),
            extra={"final_to_first_audio_ms": state.get("final_to_first_audio_ms")},
        )
        await _safe_send(
            ws,
            {"type": "response.done", "text": fallback, "full_ms": round(full_ms)},
        )
        history.append({"role": "user", "content": query[:300]})
        history.append({"role": "assistant", "content": fallback[:300]})
        return

    user_msg = (
        f"Question: {query}\n\n"
        f"Sources:\n{_format_sources_c(hits)}\n\n"
        "Answer concisely (under 60 spoken words) using ONLY the sources above. "
        "Cite sources inline using the [doc#] tags."
    )

    # Build message list: system prompt + trimmed history + current user turn.
    # History is capped at deque maxlen=4 (2 user + 2 assistant), each 300 chars.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": config.PATH_C_SYSTEM_PROMPT}
    ]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)
    messages.append({"role": "user", "content": user_msg})

    # -- LLM streaming --------------------------------------------------------
    aoai = config.aoai_client()
    t_llm = time.perf_counter()
    try:
        stream = await aoai.chat.completions.create(
            model=config.AZURE_OPENAI_CHAT_DEPLOYMENT,
            temperature=0.2,
            max_tokens=config.PATH_C_MAX_TOKENS,
            stream=True,
            messages=messages,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("pathc chat stream open failed")
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
                state["llm_ttft_ms"] = (time.perf_counter() - t_llm) * 1000.0
            full_text.append(delta)
            buf += delta
            # Flush sentence-by-sentence; first sentence flushes ASAP.
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
                    await _send_tts_c(ws, sentence, token, region, state)
                    sentences_sent += 1
    except Exception as e:  # noqa: BLE001
        logger.exception("pathc chat stream failed")
        await _safe_send(ws, {"type": "error", "error": f"chat: {e}"})
        return

    tail = buf.strip()
    if tail and not state.get("cancelled"):
        await _send_tts_c(ws, tail, token, region, state)

    if state.get("cancelled"):
        return

    full = "".join(full_text).strip()
    full_ms = (time.perf_counter() - turn_started) * 1000.0
    telemetry.record(
        path="pathc",
        first_audio_ms=state.get("first_audio_ms"),
        full_ms=full_ms,
        embed_ms=state.get("embed_ms"),
        search_ms=state.get("search_ms"),
        llm_ttft_ms=state.get("llm_ttft_ms"),
        tts_first_byte_ms=state.get("tts_first_byte_ms"),
        extra={"final_to_first_audio_ms": state.get("final_to_first_audio_ms")},
    )
    await _safe_send(
        ws,
        {"type": "response.done", "text": full, "full_ms": round(full_ms)},
    )

    # Append to history (capped to 300 chars to control token budget).
    history.append({"role": "user", "content": query[:300]})
    history.append({"role": "assistant", "content": full[:300]})


# ---- lifespan hooks --------------------------------------------------------


async def prewarm_c() -> None:
    """Pre-warm the shared speech token; no-op if already warm."""
    try:
        await _issue_speech_token(force=False)
        logger.info("pathc: speech token warmed")
    except Exception:  # noqa: BLE001
        logger.warning("pathc prewarm failed", exc_info=True)


async def shutdown_c() -> None:
    """No-op — HTTP client lifecycle is managed by composed.shutdown."""
    pass


# ---- WebSocket endpoint ----------------------------------------------------


@router.websocket("/pathc/ws")
async def pathc_ws(client: WebSocket) -> None:
    await client.accept()
    loop = asyncio.get_running_loop()

    try:
        token, region = await _issue_speech_token()
    except Exception as e:  # noqa: BLE001
        logger.exception("pathc speech token failed")
        await _safe_send(client, {"type": "error", "error": f"token: {e}"})
        await client.close()
        return

    # Speech SDK: PushAudioInputStream → SpeechRecognizer (en-IN or hi-IN).
    fmt = speechsdk.audio.AudioStreamFormat(
        samples_per_second=config.AZURE_SPEECH_AUDIO_RATE,
        bits_per_sample=16,
        channels=1,
    )
    push_stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
    audio_cfg = speechsdk.audio.AudioConfig(stream=push_stream)

    sc = speechsdk.SpeechConfig(auth_token=token, region=region)
    sc.speech_recognition_language = config.PATH_C_STT_LANGUAGE
    # Tighter endpointing (300 ms silence) — per PLAN.md Phase 5 spec.
    sc.set_property(speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs, "300")

    recognizer = speechsdk.SpeechRecognizer(speech_config=sc, audio_config=audio_cfg)

    # Per-session history: deque(maxlen=4) = 2 user + 2 assistant messages.
    # Keeps the 2100-token HLD context budget.
    history: deque[dict[str, str]] = deque(maxlen=4)

    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    speaking: dict[str, Any] = {"started": None}
    current: dict[str, Any] = {"task": None, "state": None}

    def _post(ev: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(events.put_nowait, ev)

    def _on_recognizing(evt: Any) -> None:
        text = (evt.result.text or "").strip()
        if speaking["started"] is None and text:
            speaking["started"] = time.perf_counter()
            # Barge-in: new speech started while bot may still be talking.
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
        while True:
            ev = await events.get()
            kind = ev.get("type")
            if kind == "barge_in":
                state = current.get("state")
                task = current.get("task")
                if state is not None:
                    state["cancelled"] = True
                if task is not None and not task.done():
                    task.cancel()
                current["task"] = None
                current["state"] = None
                # Tell client to clear buffered TTS audio immediately.
                await _safe_send(client, {"type": "playback.clear"})
            elif kind == "partial":
                await _safe_send(
                    client,
                    {"type": "transcript.partial", "text": ev.get("text", "")},
                )
            elif kind == "final":
                text = (ev.get("text") or "").strip()
                final_started = time.perf_counter()
                await _safe_send(
                    client,
                    {"type": "transcript.final", "text": text},
                )
                # Drop spurious tiny finals (mic noise / echo of bot audio).
                if not text or (len(text) < 12 and len(text.split()) < 3):
                    continue
                # Cancel previous in-flight turn if still running.
                prev_state = current.get("state")
                prev_task = current.get("task")
                if prev_state is not None:
                    prev_state["cancelled"] = True
                if prev_task is not None and not prev_task.done():
                    prev_task.cancel()
                state: dict[str, Any] = {}
                current["state"] = state
                current["task"] = asyncio.create_task(
                    _run_turn_c(
                        client,
                        text,
                        token,
                        region,
                        ev["started"],
                        final_started,
                        history,
                        state,
                    )
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
            data = msg.get("bytes")
            payload = msg.get("text")
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
        logger.exception("pathc ws pump failed")
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
