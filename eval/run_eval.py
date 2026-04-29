"""Phase 3 - run gold_qa.jsonl through /search and report citation hit-rate.

For each row:
  - POST /search with q
  - mark hit if ANY expected_chunks substring appears in any returned citation
    title/section, OR if expected_chunks is empty (treat as smoke-test only)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
GOLD = ROOT / "gold_qa.jsonl"


def load_gold() -> list[dict]:
    rows: list[dict] = []
    for ln in GOLD.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        rows.append(json.loads(ln))
    return rows


def chunk_match(citations: list[dict], answer: str, expected: list[str]) -> bool:
    if not expected:
        return True  # smoke only
    haystack_parts = [f"{c.get('title', '')} {c.get('section', '')}" for c in citations]
    haystack_parts.append(answer or "")
    haystack = " ".join(haystack_parts).lower()
    return any(e.lower() in haystack for e in expected if e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/search")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    rows = load_gold()
    if not rows:
        print("No gold rows.")
        return 1

    hits = 0
    totals: list[float] = []
    with httpx.Client(timeout=60.0) as client:
        for i, row in enumerate(rows, 1):
            q = row.get("q", "")
            if "REPLACE_WITH" in q.upper():
                print(f"[{i}] SKIP placeholder row")
                continue
            t0 = time.perf_counter()
            r = client.post(args.url, json={"query": q, "use_cache": not args.no_cache})
            dt = (time.perf_counter() - t0) * 1000.0
            if r.status_code != 200:
                print(f"[{i}] HTTP {r.status_code}: {r.text[:200]}")
                continue
            payload = r.json()
            ok = chunk_match(
                payload.get("citations", []),
                payload.get("answer", ""),
                row.get("expected_chunks", []),
            )
            hits += int(ok)
            totals.append(dt)
            print(
                f"[{i}] {'OK' if ok else 'MISS'} {dt:7.0f}ms "
                f"cache={payload.get('cache_hit')} "
                f"sim={payload.get('cache_similarity')} "
                f"-> {payload.get('answer', '')[:120]!r}"
            )

    if not totals:
        return 1
    print()
    print(f"hit-rate    : {hits}/{len(totals)} ({100 * hits / len(totals):.0f}%)")
    print(f"latency p50 : {statistics.median(totals):.0f} ms")
    print(f"latency p95 : {statistics.quantiles(totals, n=20)[-1]:.0f} ms")
    print(f"latency max : {max(totals):.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
