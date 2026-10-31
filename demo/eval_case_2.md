# Demo case 2: human in the loop

**Taxpayer:** Harold Benson, born 1949-11-02, blind, test SSN 923-45-6789. His wife Margaret (born 1951-06-21) died on 2025-05-09. He is not sure what his filing status should be.
**Documents:** SSA-1099 (26,400 net, 1,200 withheld), 1099-R (31,200 taxable, box 2b unchecked, 2,800 withheld), 1099-DIV (1,320.55 ordinary, 1,100 qualified).
**Expected outcome:** the return pauses at the filing-status escalation. Once a Kushim employee decides, it completes.

## 1. Trigger the escalation

```bash
python run.py demo case2_hitl
```

The run stops after eligibility with:

```
PAUSED for a Kushim employee: [filing_status] A spouse died during the tax year.
Filing status (married filing jointly, qualifying surviving spouse or single) must be decided by a Kushim employee.
```

Nothing has been computed yet: Computation never runs on an unresolved filing status. This CLI demo uses an in-memory checkpointer and a new return ID on each invocation. Its pause and resolution happen within the same process; closing the terminal loses that demo thread. Running the command again starts a new demo, rather than resuming the previous one.

For a persistent dashboard demonstration, start the dashboard separately and open its seeded case 2 under **Needs a person**, or create a return using the case 2 fixture. The dashboard service uses encrypted SQLite checkpoints. It shows the reason, citation (REF-ES-1), relevant facts and available conversation; the prefilled fixture has no intake conversation. A CLI demo thread does not automatically appear in that queue.

## 2. Resolve it

Pick the status and continue. On the command line:

```bash
python run.py demo case2_hitl --resolve '{"action":"continue","resolved_by":"preparer","note":"Spouse died 2025-05; MFJ allowed for the year of death","facts_patch":{"filing_status":"mfj"}}'
```

In the dashboard: open the persistent case 2 item, set filing status to *Married filing jointly*, add the evidence note, and select **Continue and verify**. The resume is a `Command(resume=...)` into that dashboard thread. Completing the correction invalidates stale calculations, checks eligibility, computes once, runs review, and records a deterministic continuation result of `yes` or `no`. A negative result keeps the return paused with the remaining actions.

The prefilled case 2 fixture explicitly confirms `dual_status_alien=false`; spouse itemising is not applicable to MFJ. In a fresh conversation, the deduction-chart questions are still awaiting intake integration. Staff can confirm missing chart facts through Facts JSON after checking the evidence; they are not silently defaulted to false.

Other valid resolutions: `{"action":"halt","note":"..."}` ends the return with the reason on the dashboard (for instance, if the preparer wants to take it over by hand); a `facts_patch` of `{"filing_status":"single"}` is accepted too and produces a different draft.

## 3. What you should see after the resolution

Eligibility now reports MFJ with **three boxes on line 12d** (taxpayer 65+, taxpayer blind, spouse 65+; a spouse who died during the year still counts). Computation runs, review passes, the draft completes.

| Line | Amount | Note |
| --- | ---: | --- |
| 3b dividends | 1,320.55 | 1099-DIV |
| 5b pensions | 31,200.00 | 1099-R |
| 6a / 6b Social Security | 26,400.00 / 7,462.47 | MFJ worksheet, 32,000 / 44,000 tiers |
| 9 total income | 39,983.02 | |
| 12e standard deduction | 36,300.00 | 31,500 + 3 × 1,600 |
| 13b senior deduction | 12,000.00 | two qualifying persons, no phase-out |
| 15 taxable income | 0.00 | deductions exceed income |
| 16 tax | 0.00 | |
| 25d withholding | 4,000.00 | 1099-R 2,800 + SSA 1,200 |
| 34 refund | 4,000.00 | |

The dashboard's **Official 1040-SR draft PDF** places these amounts on the supplied official 2025 form, followed by provenance and review appendices. Its first four pages include the deduction chart; MFJ and the three age/blindness boxes are checked. Line 34 shows the 4,000.00 overpayment. Refund/application elections, bank details, address and signatures remain blank, and every official page is marked draft/not filed. **Review lines and sources** retains the HTML provenance cards; printed monetary line labels in the PDF link to its source appendix.

The escalation record stays on the return with the resolution, who made it, when, and the note. Omitting the hub between workers, the recovery path is `eligibility → escalation → eligibility → computation → review → resolution_check → finalize`. The eval `spouse_died_mfj` pins these numbers; `tests/test_escalations.py` covers the pause, the patch, the halt path, and the box count. Blocking-review cases have an additional explicit resolve-event, save-explanation and Accept sequence; case 2 is a filing-status escalation and does not use Accept.
