## Phase / level

<!-- e.g. Phase 2 — Level 2: BigQuery Hybrid + Reranker (`level2_hybrid`) -->

## Summary

<!-- What changed and why, in a few bullets. -->

## Definition of Done (spec.md §9)

- [ ] **D1** Ingestion job ran from the UI on the configured project; `ingest_runs` row + tier counts (job id: `…`)
- [ ] **D2** Engine emits every canonical stage itself; `routes.py` has no stage synthesis or timers
- [ ] **D3** ≥ 3 live golden runs with citations and judge metrics (run ids: `…`)
- [ ] **D4** UI: stage rail + live architecture diagram driven by real events; light/dark/system verified
- [ ] **D5** Recordings for all golden questions per model pair; offline replay verified
- [ ] **D6** `pytest backend/tests -q` and `npm run build && npm test` green (counts: `…`)
- [ ] **D7** Owner sign-off after live demo (date: `…`)

## Leak hygiene

- [ ] No project id / project number / bucket / account email / home path in code, docs, recordings, screenshots
- [ ] Identity values only in the git-ignored `.env`

## Evidence

<!-- Link the task.md evidence section and any screenshots. -->
