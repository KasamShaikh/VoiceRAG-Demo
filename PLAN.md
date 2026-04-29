# Low-Latency Inbound Voice RAG Bot — Prototype Plan

A web-based voice bot in **Sweden Central** answering questions grounded in 2 PDFs. Two pipelines side-by-side so the demo itself showcases the latency delta.

- **Path A (primary):** Azure **Voice Live** — integrated STT+LLM+TTS over one WebSocket; RAG via tool/pre-fetch. Target end-to-end **~800–1200 ms p50**.
- **Path B (comparison):** Composed — Speech SDK streaming STT → Azure OpenAI streaming → Speech SDK streaming TTS. Target **~1500–2200 ms p50**.

Retrieval = **Azure AI Search (hybrid + semantic ranker + integrated vectorization)** fronted by a **Redis semantic answer cache** (sub-100 ms hits).

LLM: `gpt-5.1-mini` if available in Sweden Central, else `gpt-4.1-mini`. Embeddings: `text-embedding-3-large`.

> ⚠️ **Note**: Azure Speech *Batch Transcription* is offline file processing (minutes of latency) — unsuitable for live calls. We use Voice Live (Path A) and Speech SDK *real-time streaming* (Path B).

---

## Architecture (per turn)

```
Browser (WebRTC mic, PCM 16k)
   │
   ▼
FastAPI bridge ──► Voice Live WS ──► gpt-5.1-mini (server-side)
                        │  tool: search_kb(query)
                        ▼
               Redis semantic cache ──hit──► cached chunks (<100ms)
                        │ miss
                        ▼
               Azure AI Search (hybrid + semantic reranker, top_k=3)
                        ▼
               chunks → LLM → streaming tokens → server-side TTS
   ▲
   │  PCM 24k audio out (streamed, barge-in enabled)
Browser speaker
```

---

## Phases & Steps

### Phase 1 — Foundation (Sweden Central)
1. RG `rg-voicebot-demo-swc`.
2. Provision: Azure OpenAI (`gpt-5.1-mini` if avail, else `gpt-4.1-mini`; `text-embedding-3-large`), AI Search Standard S1 + semantic ranker, Speech / Voice Live, Azure Cache for Redis (Basic C1 w/ RediSearch), Storage (`kb-pdfs` blob container), Container App for the bridge, Application Insights.
3. Managed Identity + RBAC: bridge MI → AOAI `Cognitive Services OpenAI User`, AI Search `Search Index Data Reader` + `Search Service Contributor`, Storage `Storage Blob Data Reader`.

### Phase 2 — Indexing
4. Document cracking via AI Search **integrated vectorization** with **Document Layout skill** (preserves headings, tables, page numbers).
5. Chunking: `SplitSkill` page-aware, **512 tokens / 64 overlap**, heading prefixed.
6. Index schema: `id`, `parent_id`, `content` (analyzer `en.microsoft`), `content_vector` (HNSW `m=4, efC=400, efS=100`), `title`, `section`, `page`, `source_url`. Semantic config with `content` + `title`/`section`.
7. Indexer + skillset: Blob → Layout → Split → AzureOpenAIEmbedding skill → projections.
8. Validate retrieval with 15–20 gold Q&A before voice (top-3 recall ≥ 0.9).

### Phase 3 — Retrieval service (hot path)
9. `/search`: embed query → Redis vector sim ≥ 0.97 hit; on miss, AI Search hybrid + `queryType=semantic` `top=3` + extractive captions; async write-back to Redis (1h TTL).
10. Terse system prompt (<400 tokens) → enables AOAI **prompt caching** (~50% TTFT win on repeats).
11. `top_k=3`, `max_tokens=120`. Short spoken answers = low TTS latency.

### Phase 4 — Path A: Voice Live (primary)
12. One Voice Live WS per call: pcm16 24kHz, `server_vad` (`threshold=0.5`, `silence_duration_ms=400`) for barge-in; voice `en-US-Ava:DragonHDLatestNeural`; tool `search_kb`.
13. Web client: WebRTC mic, waveform, live transcript, dual-path toggle.

### Phase 5 — Path B: Composed (comparison)
14. `SpeechRecognizer` continuous, `SegmentationSilenceTimeoutMs=300`.
15. On `Recognized` → `/search` → AOAI `stream=true` → push to `SpeechSynthesizer` streaming output.
16. **Start TTS on first sentence** — saves 500–1000 ms.

### Phase 6 — Latency optimizations
17. Region pinning — every resource in SWC.
18. Connection pre-warming — persistent HTTP/2 pools; pre-open Voice Live WS on call ring.
19. **Speculative retrieval** on STT partials (every 200 ms), cancel stale.
20. Parallel fan-out — cache + AI Search + small-talk classifier in parallel.
21. Streaming everywhere; no buffering between stages.
22. Short outputs (`max_tokens=120`).
23. AOAI prompt caching.

### Phase 7 — Telemetry & demo polish
24. Per-turn spans to App Insights: `t_stt_final`, `t_retrieval`, `t_llm_first_token`, `t_llm_complete`, `t_tts_first_audio`, `t_end_to_end`.
25. Live web dashboard: p50/p95 per stage, **Path A vs Path B** side-by-side. *This is the demo.*
26. Pre-seed Redis with gold Q&A so opening questions hit cache.

---

## Verification gates

1. Indexing: gold Q&A → `/search` top-3 recall **≥ 0.9** before voice integration.
2. Retrieval latency: p50 cache-hit **< 60 ms**, p50 cache-miss **< 250 ms**.
3. Path A E2E p50 **≤ 1.2 s**, p95 **≤ 1.8 s**.
4. Path B E2E p50 **≤ 2.0 s**, p95 **≤ 2.8 s**.
5. Barge-in: bot stops within **300 ms**.
6. Accuracy: blind-rate 20 spoken answers; **≥ 85%** correct & grounded.
7. Cache hit-rate on replay: **≥ 60%**.

---

## Decisions

- **Vector store = Azure AI Search** (not FAISS): showcase + integrated vectorization + semantic ranker + hybrid. 30–80 ms retrieval.
- **Redis semantic cache** in front of AI Search — best of both worlds.
- **Voice Live primary, composed for comparison only.** Voice Live eliminates 2 hops + telephony-tuned VAD + faster TTS path → ~600–1000 ms p50 advantage.
- **Embeddings** = `text-embedding-3-large` (downgrade to `-small` only if latency-bound; ~3% recall loss, ~30% faster).
- **Chunking** = 512 tok / 64 overlap, page-aware, heading-prefixed.
- **Region** = Sweden Central — every resource. No exceptions.
- **Out of scope**: Genesys SIP, multi-language, human handoff, transcript persistence, web auth.

## Further considerations

1. Voice Live tool calls add 200–400 ms → **pre-fetch retrieval on STT partials** and inject as context (skip the tool round-trip).
2. `gpt-5.1-mini` SWC availability → parameterize deployment name; default `gpt-4.1-mini`.
3. Redis SKU → Basic C1 for prototype; Azure Managed Redis for production.
