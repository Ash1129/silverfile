# Demo case 1: clean run

**Taxpayer:** Eleanor Whitfield, single, born 1954-03-14 (age 71 at year end), test SSN 912-34-5678.
**Documents:** one W-2 as a photographed image (`demo/w2_image_1.jpg`, Tompkins Public Library), a 1099-INT, a 1099-R with box 2b unchecked, an SSA-1099. Estimated payments of 500 stated in intake.
**Expected outcome:** the draft completes with no human intervention; review passes in one round.

## Run it

Terminal (prefilled intake, about 30 seconds online, 2 seconds offline):

```bash
python run.py demo case1_clean
```

Dashboard: `python run.py serve`, open http://localhost:8000, click **New return**, pick fixture `case1_clean`, then watch the return move through the agents on the return page. Or hold the conversation yourself with `python run.py chat --case case1_clean` and type the answers in `data/fixtures/case1_clean/answers.json` (or your own words; the parser copes with drift).

## What you should see

Events, in order: orchestrator → extraction (four branches in parallel: the image is read by Claude Sonnet vision online, from its sidecar JSON offline) → eligibility (eligible, single, one box on 12d, chart applies, in scope, no escalations) → computation (30 `tax_math` calls over MCP stdio) → review round 1 PASS → finalize.

| Line | Amount | Where it came from |
| --- | ---: | --- |
| 1z wages | 18,400.00 | `sum_amounts` over the W-2 image (box 1) |
| 2b taxable interest | 842.17 | `sum_amounts` over the 1099-INT |
| 5a / 5b pensions | 14,200.00 / 14,200.00 | `taxable_1099r` (box 2b unchecked) |
| 6a / 6b Social Security | 21,600.00 / 13,205.84 | `taxable_social_security` worksheet (single; 34,000 tier) |
| 9 total income | 46,648.01 | `sum_amounts` |
| 12d boxes / 12e standard deduction | 1 / 17,750.00 | `count_12d_boxes`, `standard_deduction` (15,750 + 2,000) |
| 13b senior deduction | 6,000.00 | `senior_deduction` (MAGI under 75,000, no phase-out) |
| 15 taxable income | 22,898.01 | `nonnegative_difference` |
| 16 tax | 2,509.26 | `income_tax` (2025 single schedule) |
| 25d withholding | 2,110.00 | W-2 1,210 + 1099-R 600 + SSA 300 |
| 26 estimated payments | 500.00 | direct user input (intake) |
| 34 refund | 100.74 | `settle` |

Every row on the form page has a provenance panel: the tool, its inputs, the source documents, the transport (`mcp-stdio`), and the rulebook citation. The Opus review attaches two low-severity notes for the preparer (the estimated payment and the age test both rest on intake statements). Neither fails the round.

## Offline versus online

Offline the numbers are identical; the only differences are the extraction method (`offline-sidecar` instead of `vision:claude-sonnet-5-5`) and the review method (`rules` instead of `rules+claude-opus-5-5`). The eval `clean_single` in `evals/cases.json` pins these lines.
