"""Phase 3 - semantic answer cache.

Two backends with the same interface:
  - InMemoryCache  : default; bounded LRU of (vec, payload).
  - RedisCache     : Azure Cache for Redis Basic C1 compatible. Stores recent
                     entries in a capped Redis list; cosine similarity is
                     computed client-side (no RediSearch needed).

Match policy: cosine(query_vec, entry_vec) >= CACHE_SIM_THRESHOLD (default 0.97).
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from . import config


@dataclass
class CacheEntry:
    question: str
    vec: list[float]
    answer: str
    citations: list[dict[str, Any]]
    created: float


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class InMemoryCache:
    def __init__(self, max_entries: int = 500, ttl: int = 3600):
        self.max_entries = max_entries
        self.ttl = ttl
        self._items: deque[CacheEntry] = deque(maxlen=max_entries)

    async def lookup(self, vec: list[float]) -> tuple[CacheEntry, float] | None:
        now = time.time()
        live: list[CacheEntry] = []
        best: tuple[CacheEntry, float] | None = None
        for e in self._items:
            if now - e.created > self.ttl:
                continue
            live.append(e)
            sim = _cosine(vec, e.vec)
            if best is None or sim > best[1]:
                best = (e, sim)
        if len(live) != len(self._items):
            self._items = deque(live, maxlen=self.max_entries)
        if best and best[1] >= config.CACHE_SIM_THRESHOLD:
            return best
        return None

    async def put(self, entry: CacheEntry) -> None:
        self._items.append(entry)


class RedisCache:
    """Capped list cache. Each push trims to max_entries."""

    KEY = "voicerag:cache:list"

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        max_entries: int = 500,
        ttl: int = 3600,
    ):
        import redis

        self.client = redis.Redis(
            host=host,
            port=port,
            password=password or None,
            ssl=True,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        self.max_entries = max_entries
        self.ttl = ttl

    @staticmethod
    def _serialize(e: CacheEntry) -> str:
        return json.dumps(
            {
                "q": e.question,
                "v": e.vec,
                "a": e.answer,
                "c": e.citations,
                "t": e.created,
            }
        )

    @staticmethod
    def _deserialize(raw: str) -> CacheEntry | None:
        try:
            d = json.loads(raw)
            return CacheEntry(
                question=d["q"],
                vec=d["v"],
                answer=d["a"],
                citations=d.get("c", []),
                created=d["t"],
            )
        except Exception:
            return None

    async def _entries(self) -> Iterable[CacheEntry]:
        raws: list[str] = await asyncio.to_thread(self.client.lrange, self.KEY, 0, -1)
        out: list[CacheEntry] = []
        for r in raws:
            e = self._deserialize(r)
            if e is not None:
                out.append(e)
        return out

    async def lookup(self, vec: list[float]) -> tuple[CacheEntry, float] | None:
        now = time.time()
        best: tuple[CacheEntry, float] | None = None
        for e in await self._entries():
            if now - e.created > self.ttl:
                continue
            sim = _cosine(vec, e.vec)
            if best is None or sim > best[1]:
                best = (e, sim)
        if best and best[1] >= config.CACHE_SIM_THRESHOLD:
            return best
        return None

    async def put(self, entry: CacheEntry) -> None:
        raw = self._serialize(entry)

        def _push() -> None:
            pipe = self.client.pipeline()
            pipe.lpush(self.KEY, raw)
            pipe.ltrim(self.KEY, 0, self.max_entries - 1)
            pipe.expire(self.KEY, max(self.ttl * 2, 7200))
            pipe.execute()

        await asyncio.to_thread(_push)


_singleton: InMemoryCache | RedisCache | None = None


def get_cache() -> InMemoryCache | RedisCache:
    global _singleton
    if _singleton is not None:
        return _singleton
    if config.REDIS_HOST and config.REDIS_PASSWORD:
        try:
            _singleton = RedisCache(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD,
                max_entries=config.CACHE_MAX_ENTRIES,
                ttl=config.CACHE_TTL_SECONDS,
            )
            return _singleton
        except Exception:
            pass
    _singleton = InMemoryCache(
        max_entries=config.CACHE_MAX_ENTRIES, ttl=config.CACHE_TTL_SECONDS
    )
    return _singleton
