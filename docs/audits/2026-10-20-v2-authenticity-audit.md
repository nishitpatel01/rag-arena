# v2.0 Authenticity Audit — 2026-10-20

Read-only audit of `rag-arena` v2.0 (`git df45aff`) performed before the v3.0 reset. Two independent
auditors (backend, frontend) read every engine, `routes.py`, evaluation, ingestion, and all React
components. Findings are cited as `file:line` at commit `df45aff`.

## A. Backend — real vs. simulated vs. stubbed in `DEMO_MODE=live`

| Engine | Real GCP calls | Simulated / stubbed | Evidence |
| :-- | :-- | :-- | :-- |
| `level1_naive` | `embed_content`, BigQuery `VECTOR_SEARCH`, `generate_content` | `eval_metrics` constant `(0.65,0.60,0.55)`; cost from `len(prompt)//4` × fixed rates; uploaded sessions use local token overlap | `level1_naive.py:150-154, 156-170, 180-189, 205, 143-147` |
| `level2_hybrid` | `embed_content`, BigQuery RRF SQL, Discovery Engine `rank()`, `generate_content` | **`except Exception: pass`** on rerank → silent lexical heuristic; constant metrics `(0.85,0.82,0.86)` | `level2_hybrid.py:124-125, 127-145, 309` |
| `vertex_vector_v2` | `embed_content`, `generate_content` only | `MatchServiceClient` has no `search_data_objects` → always `[]` → local-PDF fallback; sparse vector is a BLAKE2b hash; request dict never sent | `vertex_vector_v2.py:33-60, 63-102, 139-144, 204-207`; `gcp_clients.py:140-159` |
| `alloydb_scann` | `embed_content` (result discarded when `ALLOYDB_DSN` empty) | local-PDF fallback; `psycopg` not in requirements; constant metrics | `alloydb_scann.py:230-238, 296`; `requirements.txt` |
| `level3_multimodal` | `embed_content` (text-only query), BigQuery RRF on `embedding_multimodal`, `generate_content` | **Document AI never imported; Gemini vision never called**; "diagram" text is a planted banner + prefix; constant metrics `(0.92,0.90,0.91)` | `parsers.py:15-19, 131-141, 236-240`; `level3_multimodal.py:177` |
| `level4_agentic` | `InMemoryRunner.run` attempted | runner called without `create_session`, synchronously, inside `except Exception: return None`; **ADK answer discarded**; tool trace built by Python in fixed order; `_AMENDMENT_LEDGER` hardcoded; default clause `"CLIN-0004-AB"`; `calculate` defaults 2000 hours; constant metrics `(0.98,0.97,0.96)` | `level4_agentic.py:43-93, 312, 362-408, 434, 492, 506-598, 515-517, 607` |
| `managed_vertex` | `SearchServiceClient.search` | on **any** exception → local search + Gemini presented as the managed answer; synthetic chunk ids/bbox/scores; constant metrics | `managed_vertex.py:123-142, 143-158, 174` |

### A.1 Stage telemetry is a timer simulation
`routes.py:139-172`: `chunking` emitted before `run_query` starts; `embedding` after `asyncio.wait(timeout=0.25)`;
`retrieving` after a further `0.35s`; `reranking_or_agent` and `synthesizing` emitted **after** the result already
exists; `retrieval_complete.latency_ms = result.latency_ms // 3` (`routes.py:164`). Engines expose no
callbacks (`base.py:98-105`). Blueprint strings in `battlefield.py:73-522` are static, including a
`code_ref` to a non-existent `_discover_dynamic_amendment_From_chunks` (`battlefield.py:418`).

### A.2 Metrics and diagnostics provenance
- Live `eval_complete` metrics **are** LLM-judged (`evaluator.py:89-135`) — the one honest signal.
- `strengths` / `missed_capabilities` / `verdict_reason` are static per-engine strings (`battlefield.py:679-770`),
  asserting "Discovery Engine cross-encoder reranking" and "Google ADK invoked" unconditionally.
- `/api/v1/leaderboard` and README accuracy percentages come from hand-typed `precomputed_runs.json`.
- Upload endpoint returns `"status": "indexed"` without embedding or indexing (`routes.py:458-465`).

### A.3 Tests affected by the v3 reset
`backend/tests/`: `test_api_and_sse.py`, `test_architectural_hardening.py`, `test_corpus_and_benchmarks.py`,
`test_engines.py`, `test_ingestion_and_guard.py`, `test_publish_manifest.py` (48 passing at `df45aff`).
Tests that encode the theater and must be rewritten: `test_engines.py:371-435` (procedural L4 in live mode),
`:191-239` and `:438-504` (hand-typed replay metrics), `:59-95` (`_AMENDMENT_LEDGER`).

## B. Frontend

| Finding | Evidence |
| :-- | :-- |
| Width capped at 1280px | `App.tsx:13, 55` `max-w-7xl` |
| No theme system: no `darkMode` key, no CSS variables, no provider, `dark:` used 0 times; `<html class="dark">` inert | `tailwind.config.js`, `index.css`, `index.html:2` |
| ≈350 hardcoded palette class tokens across 13 files (slate/blue/amber/emerald/indigo) + raw hex in `DocumentViewer.tsx:84-105`, `MetricRadar.tsx:68-77`, `index.html:7-43` | hand count |
| Timer-driven visuals: `ChunkingAnimator` `setInterval 950ms` tier reveal unrelated to runs; `PipelineFlowVisualizer` `setTimeout 1050ms` fake stage sim overriding real progress; Auto-Play rotates tabs on `setInterval 3500ms` | `ChunkingAnimator.tsx:228`, `PipelineFlowVisualizer.tsx:165-177, 213-216`, `ArenaDashboard.tsx:322` |
| Run start pre-rolls `progressPct:12`, `activePipelineStage:"chunking"` before any event | `ArenaDashboard.tsx:354-384` |
| Reducer keeps only the latest snapshot per engine; drops `label/description/code_ref/sql_or_api`; no timestamps or durations | `ArenaDashboard.tsx:70-143` |
| Step 2 visualizer unmounted while streaming unless its tab is open; run force-switches tabs | `ArenaDashboard.tsx:465-470, 511-557` |
| Project/bucket names hardcoded in UI | `ArchitectureInspector.tsx:65, 68, 142, 216, 365, 489, 492, 642, 690, 744` |
| Accessibility: clickable `<div>`s, infinite animations without `prefers-reduced-motion`, `text-[10px]`, tabs without `aria-*`, unlabeled input, colour-only status | see component list |

## C. Consequences adopted in v3.0 (`spec.md §2`)
P1 measured-not-simulated · P2 fail loud · P3 no hardcoded scores · P4 one level at a time ·
P5 honest labels · P6 recordings-based replay.
