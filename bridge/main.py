"""
Phase 3-5 - FastAPI bridge.

Endpoints:
  GET  /healthz                 liveness
  POST /search                  cached hybrid+semantic retrieval (Phase 3)
  WS   /voicelive/ws            Path A: relay to Azure Voice Live  (Phase 4)
  WS   /composed/ws             Path B: Speech SDK STT -> AOAI -> Speech TTS (Phase 5)
  GET  /metrics                 per-turn latency telemetry (Phase 7)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import config
from . import telemetry as tel
from .cache import CacheEntry, get_cache
from .composed import prewarm as composed_prewarm
from .composed import router as composed_router
from .composed import shutdown as composed_shutdown
from .pathc import prewarm_c, router as pathc_router, shutdown_c
from .search import embed, hybrid_semantic_search
from .voicelive import router as voicelive_router

logger = logging.getLogger("bridge")
logging.basicConfig(level=logging.INFO)


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    tel.configure_app_insights(app)
    try:
        await composed_prewarm()
    except Exception:  # noqa: BLE001
        logger.warning("prewarm failed", exc_info=True)
    try:
        await prewarm_c()
    except Exception:  # noqa: BLE001
        logger.warning("pathc prewarm failed", exc_info=True)
    yield
    try:
        await composed_shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        await shutdown_c()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="Voice RAG Bridge", version="0.7.0", lifespan=_lifespan)
app.include_router(voicelive_router)
app.include_router(composed_router)
app.include_router(pathc_router)

# Serve the web client (Path A demo) when the folder is present.
import os as _os  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_web_dir = _os.environ.get(
    "WEB_DIR", _os.path.join(_os.path.dirname(__file__), "..", "web")
)
if _os.path.isdir(_web_dir):
    app.mount("/web", StaticFiles(directory=_web_dir, html=True), name="web")


# ---- models ----------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int | None = None
    use_cache: bool = True


class Citation(BaseModel):
    id: str
    title: str
    section: str | None = None
    page: int | None = None
    source_url: str | None = None


class SearchResponse(BaseModel):
    query: str
    answer: str
    citations: list[Citation]
    cache_hit: bool
    cache_similarity: float | None = None
    timings_ms: dict[str, float]


# ---- helpers ---------------------------------------------------------------


def _format_sources(hits: list[Any]) -> str:
    out: list[str] = []
    for i, h in enumerate(hits, start=1):
        loc = []
        if h.section:
            loc.append(h.section)
        if h.page is not None:
            loc.append(f"p.{h.page}")
        loc_str = " - ".join(loc) if loc else h.title
        snippet = (h.content or "").strip().replace("\n", " ")
        if len(snippet) > 1200:
            snippet = snippet[:1200] + "..."
        out.append(f"[doc{i}] ({h.title} - {loc_str})\n{snippet}")
    return "\n\n".join(out)


async def _synthesize(query: str, hits: list[Any]) -> str:
    if not hits:
        return "I don't have that information in the knowledge base."
    sources = _format_sources(hits)
    user_msg = (
        f"Question: {query}\n\n"
        f"Sources:\n{sources}\n\n"
        "Answer concisely (under 60 spoken words) using ONLY the sources above. "
        "Cite sources inline using the [doc#] tags."
    )
    client = config.aoai_client()
    resp = await client.chat.completions.create(
        model=config.AZURE_OPENAI_CHAT_DEPLOYMENT,
        temperature=0.2,
        max_tokens=180,
        messages=[
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


# ---- endpoints -------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics(path: str | None = None, limit: int = 50) -> dict[str, Any]:
    return {
        "summary": {
            "all": tel.summary(None),
            "voicelive": tel.summary("voicelive"),
            "composed": tel.summary("composed"),
        },
        "recent": tel.recent(limit=limit, path=path),
    }


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    timings: dict[str, float] = {}
    cache = get_cache()

    # 1) Embed once for cache + retrieval.
    t0 = time.perf_counter()
    try:
        vec = await embed(req.query)
    except Exception as e:
        logger.exception("embed failed")
        raise HTTPException(status_code=502, detail=f"embed failed: {e}") from e
    timings["embed"] = (time.perf_counter() - t0) * 1000.0

    # 2) Cache lookup.
    if req.use_cache:
        t1 = time.perf_counter()
        cached = await cache.lookup(vec)
        timings["cache_lookup"] = (time.perf_counter() - t1) * 1000.0
        if cached is not None:
            entry, sim = cached
            timings["total"] = sum(timings.values())
            logger.info("cache hit sim=%.3f q=%r", sim, req.query[:80])
            return SearchResponse(
                query=req.query,
                answer=entry.answer,
                citations=[Citation(**c) for c in entry.citations],
                cache_hit=True,
                cache_similarity=sim,
                timings_ms=timings,
            )

    # 3) Retrieve.
    try:
        hits, search_timings = await hybrid_semantic_search(
            req.query, top_k=req.top_k, query_vector=vec
        )
    except Exception as e:
        logger.exception("search failed")
        raise HTTPException(status_code=502, detail=f"search failed: {e}") from e
    timings["search"] = search_timings.get("search", 0.0)

    # 4) Synthesize answer.
    t2 = time.perf_counter()
    try:
        answer = await _synthesize(req.query, hits)
    except Exception as e:
        logger.exception("synthesize failed")
        raise HTTPException(status_code=502, detail=f"synthesize failed: {e}") from e
    timings["synthesize"] = (time.perf_counter() - t2) * 1000.0

    citations = [
        Citation(
            id=h.id,
            title=h.title,
            section=h.section,
            page=h.page,
            source_url=h.source_url,
        )
        for h in hits
    ]

    # 5) Cache write (best-effort).
    if req.use_cache:
        try:
            await cache.put(
                CacheEntry(
                    question=req.query,
                    vec=vec,
                    answer=answer,
                    citations=[c.model_dump() for c in citations],
                    created=time.time(),
                )
            )
        except Exception:
            logger.warning("cache put failed", exc_info=True)

    timings["total"] = sum(v for k, v in timings.items() if k != "total")
    return SearchResponse(
        query=req.query,
        answer=answer,
        citations=citations,
        cache_hit=False,
        timings_ms=timings,
    )
