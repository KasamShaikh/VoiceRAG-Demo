"""
Phase 3-5 — FastAPI bridge.

Endpoints (to be implemented):
  GET  /healthz                 liveness
  POST /search                  cached hybrid+semantic retrieval
  WS   /voicelive/ws            Path A: relay to Azure Voice Live
  WS   /composed/ws             Path B: Speech SDK STT -> AOAI -> Speech SDK TTS
  GET  /metrics                 per-turn latency telemetry (p50/p95)

Stub for Phase 1 scaffolding.
"""

from fastapi import FastAPI

app = FastAPI(title="Voice RAG Bridge", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# Phases 3-5 will import and mount routers from:
#   from . import search, voicelive, composed, cache
