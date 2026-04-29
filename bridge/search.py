"""Phase 3 - hybrid + semantic AI Search client (REST + Entra bearer token)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import config


@dataclass
class SearchHit:
    id: str
    title: str
    section: str | None
    page: int | None
    content: str
    source_url: str | None
    rerank_score: float | None
    score: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "section": self.section,
            "page": self.page,
            "content": self.content,
            "source_url": self.source_url,
            "rerank_score": self.rerank_score,
            "score": self.score,
        }


_SEARCH_SCOPE = "https://search.azure.com/.default"


async def _bearer() -> str:
    return config.credential().get_token(_SEARCH_SCOPE).token


async def embed(query: str) -> list[float]:
    client = config.aoai_client()
    resp = await client.embeddings.create(
        model=config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        input=query,
    )
    return resp.data[0].embedding


async def hybrid_semantic_search(
    query: str,
    *,
    top_k: int | None = None,
    vector_k: int | None = None,
    query_vector: list[float] | None = None,
) -> tuple[list[SearchHit], dict[str, float]]:
    """Run a hybrid (BM25 + vector) query with semantic ranker.

    Returns (hits, timings_ms) where timings has keys: embed, search.
    """
    if not config.AZURE_SEARCH_ENDPOINT:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT is not set")

    top_k = top_k or config.AZURE_SEARCH_TOP_K
    vector_k = vector_k or config.AZURE_SEARCH_VECTOR_K

    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    if query_vector is None:
        query_vector = await embed(query)
    timings["embed"] = (time.perf_counter() - t0) * 1000.0

    body = {
        "search": query,
        "top": top_k,
        "queryType": "semantic",
        "semanticConfiguration": "default",
        "captions": "extractive",
        "answers": "extractive|count-1",
        "select": "id,title,section,page,source_url,content",
        "vectorQueries": [
            {
                "kind": "vector",
                "vector": query_vector,
                "fields": "content_vector",
                "k": vector_k,
            }
        ],
    }

    token = await _bearer()
    url = (
        f"{config.AZURE_SEARCH_ENDPOINT.rstrip('/')}"
        f"/indexes/{config.AZURE_SEARCH_INDEX}/docs/search"
        f"?api-version={config.AZURE_SEARCH_API_VERSION}"
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    t1 = time.perf_counter()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=headers, json=body)
    timings["search"] = (time.perf_counter() - t1) * 1000.0

    if r.status_code >= 400:
        raise RuntimeError(f"AI Search {r.status_code}: {r.text}")

    payload = r.json()
    hits: list[SearchHit] = []
    for doc in payload.get("value", []):
        hits.append(
            SearchHit(
                id=str(doc.get("id", "")),
                title=doc.get("title") or "",
                section=doc.get("section"),
                page=doc.get("page"),
                content=doc.get("content") or "",
                source_url=doc.get("source_url"),
                rerank_score=doc.get("@search.rerankerScore"),
                score=doc.get("@search.score"),
            )
        )
    return hits, timings
