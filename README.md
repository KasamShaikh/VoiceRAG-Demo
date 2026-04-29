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

### 4. Voice call demo (Phase 4)

With the bridge running (`./scripts/dev.ps1`), open
[http://127.0.0.1:8000/web/](http://127.0.0.1:8000/web/) in Chrome / Edge,
click **Start call**, allow microphone, and ask one of the gold questions
(e.g. *"What is my sum insured?"*). The page streams 24 kHz PCM16 to
`/voicelive/ws`, which relays to Azure Voice Live and exposes a single
`kb_search` tool that the realtime model can invoke. Per-turn first-audio
and full-response latency render live in the metrics table.

Voice Live env knobs (defaults usually fine):

| Variable | Default |
| --- | --- |
| `AZURE_VOICE_LIVE_WSS_URL` | derived from `AZURE_OPENAI_ENDPOINT` + `/openai/realtime` |
| `AZURE_VOICE_LIVE_MODEL` | `gpt-4o-mini-realtime-preview` |
| `AZURE_VOICE_LIVE_VOICE` | `alloy` |
| `AZURE_VOICE_LIVE_API_VERSION` | `2025-04-01-preview` |

### 5. Composed path (Phase 5)

Toggle **Path B** in the demo page; the same mic stream is routed to
`/composed/ws`. The bridge runs continuous STT via the Speech SDK,
streams retrieval + AOAI chat completion, and flushes each *sentence*
to Speech TTS as soon as the boundary appears in the LLM stream — that's
what keeps the composed path within striking distance of Voice Live.

Auth is keyless: the bridge MI fetches an AAD token, exchanges it at
`<region>.api.cognitive.microsoft.com/sts/v1.0/issueToken` for a 10-minute
Speech token, and passes it as `Authorization: Bearer …` to STT/TTS.

| Variable | Default |
| --- | --- |
| `AZURE_SPEECH_REGION` | (set by Bicep output) |
| `AZURE_SPEECH_STT_LANGUAGE` | `en-US` |
| `AZURE_SPEECH_TTS_VOICE` | `en-US-JennyNeural` |
| `AZURE_SPEECH_AUDIO_RATE` | `24000` |

## Status

- [x] Phase 1 — Bicep authored
- [ ] Phase 1 — Deployed
- [x] Phase 2 — Indexer authored
- [ ] Phase 2 — Indexed
- [x] Phase 3 — Retrieval service (`/search` + semantic cache)
- [x] Phase 4 — Voice Live path (WS relay + `kb_search` tool + browser client)
- [x] Phase 5 — Composed path (Speech STT → AOAI streaming → Speech TTS)
- [x] Phase 6 — Latency tuning (token cache, persistent HTTP/2 pool, fast first-sentence flush, prewarm)
- [x] Phase 7 — Telemetry (`/metrics` ring buffer + App Insights via OTel)
