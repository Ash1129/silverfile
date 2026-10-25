# SilverFile reference corpus (tax year 2025)

The Eligibility & Rules agent is grounded in this corpus and cites entries by id.
Entries restate the SilverFile brief (section 3) and the Form 1040-SR instructions
for the in-scope profile only. Nothing here is tax advice.

## REF-EL-1 — Who may use Form 1040-SR
For tax year 2025 the taxpayer must have been born before 2 January 1961. On a
joint return, only one spouse needs to meet this test. There is no income
restriction. Form 1040-SR carries the same line numbers and computations as
Form 1040, in larger type.

## REF-FS-1 — Filing status
All five statuses are in scope: Single, Married filing jointly (MFJ), Married
filing separately (MFS), Head of household (HOH), Qualifying surviving spouse
(QSS). A spouse who died during the tax year creates ambiguity between MFJ, QSS
and Single and must be decided by a human (REF-ES-1, condition 5).

## REF-SD-1 — Standard Deduction Chart, base amounts
2025 base amounts: $15,750 (Single, MFS); $31,500 (MFJ, QSS); $23,625 (HOH).
Standard deduction only; itemised deductions (Schedule A) are out of scope.

## REF-SD-2 — Standard Deduction Chart, additional amounts
Each box checked on line 12d adds $2,000 if the taxpayer is unmarried and not a
surviving spouse (Single, HOH), otherwise $1,600 (MFJ, MFS, QSS).

## REF-SD-3 — Line 12d boxes
Count one box each for: taxpayer 65 or older, taxpayer blind, spouse 65 or
older, spouse blind. "65 or older" for 2025 means born before 2 January 1961.
Spouse boxes count on a joint return.

## REF-SD-4 — When the chart does not apply
The Standard Deduction Chart does not apply if the taxpayer can be claimed as a
dependent, if a separately filing spouse itemises, or if the taxpayer was a
dual-status alien. All three are escalation conditions.

## REF-SR-1 — Enhanced senior deduction (new for 2025)
An additional $6,000 per qualifying person aged 65 or older ($12,000 if both
spouses on a joint return qualify), claimed on Schedule 1-A and carried to line
13b. It phases out above $75,000 MAGI ($150,000 joint) at 6% of the excess,
applied per qualifying individual. It is in addition to the additional standard
deduction in REF-SD-2. Applies to tax years 2025 to 2028. In this profile MAGI
equals AGI (line 11a).

## REF-SS-1 — Taxable Social Security benefits
Only a portion of benefits is taxable, determined by a worksheet over
provisional income (AGI excluding benefits, plus tax-exempt interest, plus half
of benefits) against base amounts of $25,000 (Single, HOH, QSS, MFS living
apart) / $32,000 (MFJ) and second-tier amounts of $34,000 / $44,000. Up to 50%
is taxable between the tiers and up to 85% above the second tier. The worksheet
needs every other income line first, and its result (line 6b) feeds AGI.

## REF-RT-1 — 1099-R distributions
IRA distributions go to lines 4a/4b and pensions and annuities to 5a/5b. With
distribution code 7 the taxable amount is box 2a; a qualified charitable
distribution (QCD) reduces the taxable IRA amount. Any 1099-R with box 2b
"taxable amount not determined" checked needs the Simplified Method or the
General Rule, neither of which is in scope: escalate regardless of other data.

## REF-TX-1 — Tax (line 16)
Line 16 is computed from the 2025 rate schedule for the filing status. The
qualified dividends and capital gain worksheet is not applied in this profile;
the preparer is told when line 3a is non-zero.

## REF-SC-1 — In scope
Amounts from W-2, SSA-1099, 1099-R, 1099-INT, 1099-DIV; all five filing
statuses; standard deduction only; Schedule 1-A senior deduction; Schedules 1,
2 and 3 as values the user enters directly.

## REF-SC-2 — Out of scope (escalate, never attempt)
Itemised deductions (Schedule A); self-employment or business income (Schedule
C) including 1099-NEC nonemployee compensation; rental income (Schedule E);
capital gains requiring Schedule D (including 1099-B); digital assets; foreign
income or accounts; amended or prior-year returns; state returns; any 1099-R
with box 2b checked.

## REF-ES-1 — Escalation to a human
Pause and route to a Kushim employee for: (1) any out-of-scope item; (2) any
unresolved conflict between a user statement and a document; (3) any 1099-R
with box 2b checked; (4) a request for tax advice, planning or optimisation;
(5) filing-status ambiguity, most importantly a spouse who died during the tax
year; (6) any indication of financial exploitation or third-party pressure, for
example a refund routed to an account the taxpayer cannot describe, or a third
party answering on the taxpayer's behalf without a stated authority. The system
notices and stops; it does not adjudicate.

## REF-AD-1 — Advice boundary
SilverFile is a preparation aid, not a preparer and not an advisor.
Optimisation and planning questions ("Should I convert my IRA to a Roth?",
"Would I pay less filing separately?", "Can I deduct my medications?") are
advice. Transcription and arithmetic are not. The system explains its scope and
offers to note the question for the volunteer preparer.

## REF-SSN-1 — Test Social Security numbers
This is a demonstration system. It accepts only SSNs whose first three digits
are 900 to 999 (the series set aside for testing) and blocks any other SSN at
entry so a real SSN is never stored.

## REF-PV-1 — Provenance
Every monetary value on the draft carries a record showing how it was
calculated and from which source. A value typed by the user is recorded as
"direct user input" with the date and who entered it.
