"""Phase 4 - Voice Live (AOAI Realtime / Voice Live) WebSocket relay.

Wire protocol assumed: Azure OpenAI Realtime / Voice Live event schema
(`session.update`, `input_audio_buffer.*`, `response.*`, function calling
via `response.function_call_arguments.done`).

Flow per browser session:

    browser  <--WS-->  bridge  <--WS-->  Azure Voice Live

  - bridge authenticates upstream with DefaultAzureCredential (no key).
  - bridge sends `session.update` configuring voice, server VAD, and a
    single tool: `kb_search` -> our /search retrieval (without the LLM
    synthesis step; the realtime model does that itself).
  - bridge transparently relays everything else.
  - On `response.function_call_arguments.done` for `kb_search`, the bridge
    runs retrieval locally, returns top-k snippets via
    `conversation.item.create` + `response.create`, and logs a
    per-turn timing for the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import config
from .search import embed, hybrid_semantic_search

logger = logging.getLogger("bridge.voicelive")
router = APIRouter()


KB_TOOL = {
    "type": "function",
    "name": "kb_search",
    "description": (
        "Search the insurance knowledge base (policy wording + customer policy "
        "certificate) for facts. Use for any user question about coverage, "
        "benefits, exclusions, premium, waiting periods, claims, or specific "
        "values from the customer's policy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Concise search query in English.",
            }
        },
        "required": ["query"],
    },
}


def session_update_event() -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "voice": config.AZURE_VOICE_LIVE_VOICE,
            "instructions": config.SYSTEM_PROMPT,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 200,
                "silence_duration_ms": 350,
                "create_response": True,
            },
            "tools": [KB_TOOL],
            "tool_choice": "auto",
            "temperature": 0.6,
        },
    }


def _format_kb_result(hits: list[Any]) -> str:
    if not hits:
        return json.dumps({"status": "no_results"})
    out = []
    for i, h in enumerate(hits, 1):
        out.append(
            {
                "tag": f"doc{i}",
                "title": h.title,
                "section": h.section,
                "page": h.page,
                "snippet": (h.content or "")[:1200],
            }
        )
    return json.dumps({"status": "ok", "results": out})


async def _run_kb_search(query: str) -> tuple[str, dict[str, float]]:
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    vec = await embed(query)
    timings["embed"] = (time.perf_counter() - t0) * 1000.0
    hits, st = await hybrid_semantic_search(query, query_vector=vec)
    timings["search"] = st.get("search", 0.0)
    return _format_kb_result(hits), timings


async def _upstream_connect() -> websockets.WebSocketClientProtocol:
    url = config.voice_live_url()
    token = config.credential().get_token(config.AZURE_VOICE_LIVE_SCOPE).token
    headers = [("Authorization", f"Bearer {token}")]
    logger.info("voicelive upstream: %s", url.split("?")[0])
    return await websockets.connect(
        url,
        additional_headers=headers,
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    )


async def _handle_function_call(
    upstream: websockets.WebSocketClientProtocol,
    call_id: str,
    name: str,
    arguments_json: str,
) -> None:
    if name != "kb_search":
        logger.warning("unknown tool %s", name)
        return

    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}
    query = (args.get("query") or "").strip()
    if not query:
        result = json.dumps({"status": "error", "error": "empty query"})
    else:
        try:
            result, timings = await _run_kb_search(query)
            logger.info(
                "kb_search q=%r embed=%.0fms search=%.0fms",
                query[:80],
                timings.get("embed", 0.0),
                timings.get("search", 0.0),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("kb_search failed")
            result = json.dumps({"status": "error", "error": str(e)})

    # Return tool output to the model and ask it to continue speaking.
    await upstream.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }
        )
    )
    await upstream.send(json.dumps({"type": "response.create"}))


async def _client_to_upstream(
    client: WebSocket, upstream: websockets.WebSocketClientProtocol
) -> None:
    try:
        while True:
            msg = await client.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            data = msg.get("bytes")
            if text is not None:
                await upstream.send(text)
            elif data is not None:
                await upstream.send(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("client->upstream pump failed")
    finally:
        try:
            await upstream.close()
        except Exception:
            pass


async def _upstream_to_client(
    client: WebSocket, upstream: websockets.WebSocketClientProtocol
) -> None:
    pending_calls: dict[str, dict[str, str]] = {}
    try:
        async for raw in upstream:
            if isinstance(raw, (bytes, bytearray)):
                await client.send_bytes(bytes(raw))
                continue

            # text frame: forward as-is, then peek for tool calls.
            await client.send_text(raw)
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")
            if etype == "response.output_item.added":
                item = evt.get("item") or {}
                if item.get("type") == "function_call":
                    pending_calls[item.get("call_id", "")] = {
                        "name": item.get("name", ""),
                        "args": "",
                    }
            elif etype == "response.function_call_arguments.delta":
                cid = evt.get("call_id", "")
                if cid in pending_calls:
                    pending_calls[cid]["args"] += evt.get("delta", "")
            elif etype == "response.function_call_arguments.done":
                cid = evt.get("call_id", "")
                state = pending_calls.pop(cid, None)
                if state is None:
                    state = {
                        "name": evt.get("name", ""),
                        "args": evt.get("arguments", ""),
                    }
                # `arguments` may also be on .done directly
                args = state.get("args") or evt.get("arguments", "")
                asyncio.create_task(
                    _handle_function_call(upstream, cid, state.get("name", ""), args)
                )
    except websockets.ConnectionClosed:
        pass
    except Exception:
        logger.exception("upstream->client pump failed")
    finally:
        try:
            await client.close()
        except Exception:
            pass


@router.websocket("/voicelive/ws")
async def voicelive_ws(client: WebSocket) -> None:
    await client.accept()
    try:
        upstream = await _upstream_connect()
    except Exception as e:  # noqa: BLE001
        logger.exception("voicelive upstream connect failed")
        await client.send_text(
            json.dumps({"type": "bridge.error", "error": f"upstream connect: {e}"})
        )
        await client.close()
        return

    # Configure session + tool BEFORE relaying audio.
    try:
        await upstream.send(json.dumps(session_update_event()))
    except Exception:
        logger.exception("session.update failed")
        await upstream.close()
        await client.close()
        return

    await asyncio.gather(
        _client_to_upstream(client, upstream),
        _upstream_to_client(client, upstream),
    )
