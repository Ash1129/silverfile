# Sample traces

Real runs on 2026-10-25 with Claude (Sonnet vision, Opus review) against the synthetic fixtures. SSNs are masked.

| File | What it is |
| --- | --- |
| `online_case1.log` | Terminal transcript of `python run.py demo case1_clean`: events, every line with its method and sources |
| `case1_clean_online_state.json` | The full return state of a clean run: intake facts, extracted documents (with the vision confidence and issues), eligibility determinations with citations, every line's provenance, the review round (recomputed lines, model, tokens), handoff log, token log |
| `case1_clean_draft_form.html` | The draft form for that run, provenance card per amount |
| `online_case2.log` | Terminal transcript of the HITL case resolved from the command line |
| `case2_hitl_resolved_state.json` | State after the filing-status escalation was resolved in the dashboard (resolution, note, patch, the second eligibility pass, the Opus review) |
| `case2_hitl_draft_form.html`, `case2_hitl_draft_form.pdf` | The draft form for that run, as the dashboard serves it |

`docs/metrics.json` holds the per-document token counts behind `docs/metrics.md`. Checkpoint databases (`*.sqlite`) are not committed; `python run.py serve` creates one under `data/runtime/`.
