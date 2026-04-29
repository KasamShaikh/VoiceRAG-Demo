# Voice RAG Bot — PoC

Low-latency inbound voice bot prototype. See [PLAN.md](PLAN.md) for full architecture, phases, and verification gates.

## Layout

```
VoiceRAG-Demo/
├── PLAN.md                  full plan
├── azure.yaml               azd project descriptor
├── data/                    local PDFs (gitignored)
├── infra/                   Bicep IaC (Phase 1)
├── indexer/                 datasource + index + skillset + runner (Phase 2)
├── scripts/index.ps1        one-shot helper: upload PDFs + (re)build index
├── bridge/                  FastAPI bridge (Phases 3–5)
├── web/                     browser client + latency dashboard (Phases 4–7)
└── eval/                    gold Q&A retrieval-accuracy gate
```

## Quickstart

### 1. Provision infra (Phase 1)

```pwsh
azd auth login
azd env new voicebot-swc
azd env set AZURE_LOCATION swedencentral
# Optional: grant your user data-plane access for local indexing/testing
azd env set AZURE_PRINCIPAL_ID (az ad signed-in-user show --query id -o tsv)
azd up
```

### 2. Build the knowledge base (Phase 2)

Drop your PDFs into `./data/` (gitignored — never committed), then:

```pwsh
./scripts/index.ps1
```

This loads `azd` env values, uploads PDFs to the `kb-pdfs` container, then
creates/updates the AI Search datasource, index, skillset, and indexer and
runs it end-to-end. Expect ~30–60 seconds for two PDFs.

### 3. Run the retrieval service locally (Phase 3)

```pwsh
# Optional: enable Redis-backed cache
az redis list-keys -g $env:AZURE_RESOURCE_GROUP -n <redis-name> --query primaryKey -o tsv `
  | % { azd env set REDIS_PASSWORD $_ }

./scripts/dev.ps1
# in another terminal:
python eval/run_eval.py
```

`POST http://127.0.0.1:8000/search` with `{"query":"..."}` returns:

```json
{
  "answer": "...",
  "citations": [...],
  "cache_hit": false,
  "timings_ms": {"embed": 90, "search": 180, "synthesize": 410, "total": 680}
}
```

Cache policy: cosine ≥ `CACHE_SIM_THRESHOLD` (default 0.97), bounded LRU
(default 500 entries, 1 h TTL). Falls back to in-memory if `REDIS_HOST` /
`REDIS_PASSWORD` aren't set.

## Status

- [x] Phase 1 — Bicep authored
- [ ] Phase 1 — Deployed
- [x] Phase 2 — Indexer authored
- [ ] Phase 2 — Indexed
- [x] Phase 3 — Retrieval service (`/search` + semantic cache)
- [ ] Phase 4 — Voice Live path
- [ ] Phase 5 — Composed path
- [ ] Phase 6 — Latency tuning
- [ ] Phase 7 — Telemetry + dashboard
