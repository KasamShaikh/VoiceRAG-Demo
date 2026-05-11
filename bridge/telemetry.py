"""Phase 7 - per-turn telemetry ring buffer + optional App Insights wiring."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Iterable

logger = logging.getLogger("bridge.telemetry")

_MAX = int(os.environ.get("METRICS_BUFFER", "500"))
_lock = threading.Lock()
_turns: deque[dict[str, Any]] = deque(maxlen=_MAX)


def record(
    *,
    path: str,
    first_audio_ms: float | None = None,
    full_ms: float | None = None,
    embed_ms: float | None = None,
    search_ms: float | None = None,
    llm_ttft_ms: float | None = None,
    tts_first_byte_ms: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    row = {
        "ts": time.time(),
        "path": path,
        "first_audio_ms": first_audio_ms,
        "full_ms": full_ms,
        "embed_ms": embed_ms,
        "search_ms": search_ms,
        "llm_ttft_ms": llm_ttft_ms,
        "tts_first_byte_ms": tts_first_byte_ms,
    }
    if extra:
        row.update(extra)
    with _lock:
        _turns.append(row)


def _percentile(values: Iterable[float], p: float) -> float | None:
    arr = sorted(v for v in values if v is not None)
    if not arr:
        return None
    k = min(len(arr) - 1, int(p / 100.0 * len(arr)))
    return round(arr[k], 1)


def summary(path: str | None = None) -> dict[str, Any]:
    with _lock:
        rows = [r for r in _turns if path is None or r.get("path") == path]
    keys = (
        "first_audio_ms",
        "final_to_first_audio_ms",
        "full_ms",
        "embed_ms",
        "search_ms",
        "llm_ttft_ms",
        "tts_first_byte_ms",
    )
    out: dict[str, Any] = {"count": len(rows), "path": path or "all"}
    for k in keys:
        vals = [r.get(k) for r in rows if r.get(k) is not None]
        out[k] = {
            "p50": _percentile(vals, 50),
            "p95": _percentile(vals, 95),
            "n": len(vals),
        }
    return out


def recent(limit: int = 50, path: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        rows = list(_turns)
    if path is not None:
        rows = [r for r in rows if r.get("path") == path]
    return rows[-limit:]


# ---- App Insights / OpenTelemetry (best-effort) ----------------------------

_configured = False


def configure_app_insights(app: Any | None = None) -> None:
    """Wire azure-monitor-opentelemetry if a connection string is set."""
    global _configured
    if _configured:
        return
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if not conn:
        logger.info(
            "App Insights: APPLICATIONINSIGHTS_CONNECTION_STRING not set; skipping"
        )
        _configured = True
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=conn)
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(app)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "FastAPI auto-instrumentation unavailable", exc_info=True
                )
        logger.info("App Insights configured")
    except Exception:  # noqa: BLE001
        logger.warning("App Insights configuration failed", exc_info=True)
    finally:
        _configured = True
