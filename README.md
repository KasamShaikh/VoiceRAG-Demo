# Voice RAG Bot — PoC

Low-latency inbound voice bot prototype. See [PLAN.md](PLAN.md) for full architecture, phases, and verification gates.

## Layout

```
PoC/
├── PLAN.md                  full plan
├── azure.yaml               azd project descriptor
├── infra/                   Bicep IaC (Phase 1)
│   ├── main.bicep
│   └── main.parameters.json
├── indexer/                 AI Search index + skillset + runner (Phase 2)
├── bridge/                  FastAPI bridge: Voice Live + composed paths + /search (Phases 3–5)
├── web/                     Browser client: WebRTC mic + dual-path latency dashboard (Phases 4–7)
└── eval/                    Gold Q&A for retrieval accuracy gate (Phase 2 step 8)
```

## Quickstart (after Phase 1 deploys)

```pwsh
azd auth login
azd env new voicebot-swc
azd env set AZURE_LOCATION swedencentral
azd up
```

## Status

- [x] Phase 1 — Bicep authored
- [ ] Phase 1 — Deployed
- [ ] Phase 2 — Indexer
- [ ] Phase 3 — Retrieval service
- [ ] Phase 4 — Voice Live path
- [ ] Phase 5 — Composed path
- [ ] Phase 6 — Latency tuning
- [ ] Phase 7 — Telemetry + dashboard
