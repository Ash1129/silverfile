# Demo case 1: clean run

**Taxpayer:** Eleanor Whitfield, single, born 1954-03-14 (age 71 at year end), test SSN 912-34-5678.
**Documents:** one W-2 as a photographed image (`demo/w2_image_1.jpg`, Tompkins Public Library), a 1099-INT, a 1099-R with box 2b unchecked, an SSA-1099. Estimated payments of 500 stated in intake.
**Expected outcome:** the draft completes with no human intervention; review passes in one round.

## Run it

Terminal (prefilled intake, about 30 seconds online, 2 seconds offline):

```bash
python run.py demo case1_clean
```

Dashboard: `python run.py try` creates demo staff accounts (log in as `ada`, password `silverfile-demo-2025`), seeds demo returns and opens the browser. Click **New return**, pick fixture `case1_clean`, then watch the return move through the agents on the return page. Or hold the conversation yourself, below.

## Hold the conversation yourself (the clean run, one question at a time)

```bash
python run.py chat
```

Type the answers in the right-hand column. They are the ones in `data/fixtures/case1_clean/answers.json`, and your own words work too. At the upload step, type `case1_clean` to attach the demonstration documents (the W-2 image and the three 1099s). The questions below are shortened; SilverFile asks each one in full, and adds a one-line explanation the first time a tax term appears.

| # | SilverFile asks | You type |
| ---: | --- | --- |
| 1 | Am I speaking with the person whose tax return this is? | `Yes, it's my return` |
| 2 | What is your full name, as it appears on your Social Security card? | `Eleanor Whitfield` |
| 3 | What Social Security number should go on the return? (test numbers 900 to 999 only) | `912-34-5678` |
| 4 | What is your date of birth? | `March 14, 1954` |
| 5 | Are you legally blind? (explains *legally blind* and *standard deduction*) | `No` |
| 6 | At the end of 2025, were you single, married, widowed, or divorced? | `Single` |
| 7 | Which filing status do you expect to use? (explains *filing status*) | `Single` |
| 8 | Who is the first dependent you'll list on this return? (explains *dependent*) | `None` |
| 9 | Can someone else claim you as a dependent on their own tax return? | `No` |
| 10 | Which tax forms did you receive for 2025? | `I have a W-2 from the library, my SSA-1099, a 1099-R from my pension, and a 1099-INT from the bank` |
| 11 | Did you have any other income in 2025 that isn't on one of those forms? | `No` |
| 12 | In total, how much did you send the IRS in estimated tax payments during 2025? | `500` |
| 13 | What is the total of any adjustments to income you have to report? | `None` |
| 14 | Did a preparer give you an amount for 'additional income' on Schedule 1? | `None` |
| 15 | …for additional taxes on Schedule 2? | `None` |
| 16 | …for credits on Schedule 3? | `None` |
| 17 | If you're due a refund, where should it go? | `My checking account at Finger Lakes Savings` |
| 18 | Is that account in your own name? | `Yes` |
| 19 | Last step: please upload your tax forms now. | `case1_clean` |
| 20 | Here's what I have… Is that all correct? | `Yes` |

Twenty questions, no pauses for a person, and the draft below. The automated version of exactly this conversation is `test_plain_case1_conversation_matches_the_prefilled_numbers` in `tests/test_intake.py`.

To see the dignity rules at work on the same case, swap in the awkward answers from the demo script at the end of `docs/INTAKE_DIGNITY.md`. Examples: a real-looking SSN at question 3, or "Actually, can we go back to my date of birth?" at question 11. The draft comes out the same. An advice question at question 7 also pauses the return for a Kushim employee: type `{"action":"continue"}` at the prompt, answer `Single`, and the run finishes as before.

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
