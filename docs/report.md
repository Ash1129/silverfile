# SilverFile: a multi-agent preparation aid for Form 1040-SR drafts

*Report for the Kushim proof of concept (ENMGT 5405). Draft for the author to edit; about five pages.*

## 1. What was asked and what was built

Kushim asked for a system that takes an older taxpayer from a short conversation and a few uploaded documents to a draft Form 1040-SR for tax year 2025, with a provenance record for every number, a dashboard on which a Kushim employee can see every return and resolve the cases the system must not decide, and the engineering evidence that the system behaves: tests, evaluations, traces, a measured context-window strategy, and two demonstrations (a clean run and a run that needs a person).

SilverFile does that. It is a LangGraph application with six agents and one deterministic tool server. The Orchestrator routes; Intake talks to the taxpayer; Document Extraction reads uploads; Eligibility & Rules decides what applies; Computation builds the lines by calling tools; Review audits and sends work back but never fixes. Every number on the draft is the return value of a tool call or a fact the taxpayer typed, and the provenance record for the line says which, with the inputs, the source documents, the transport and the rulebook citation. The dashboard shows the queue, the conversation, the agent trace, the token log, the escalations and their resolutions, and the draft form as a page with a provenance card on every amount and as a PDF.

The system runs with or without an API key. Online, Claude Haiku parses intake answers, Claude Sonnet reads document images, and Claude Opus audits the draft. Offline, a scripted parser, sidecar files and rule checks take their places, and the numbers are identical. That property is what made the test suite possible: 212 tests and 10 evaluation cases run in about two seconds without a network.

## 2. The design stage: what Blueprint gave and what it did not

Before writing code, the brief was run through Blueprint, the agent-architecture guide built earlier in the course. The point was to see how far a design tool gets on a brief that already prescribes its own architecture. The run record and both starter kits are in `docs/blueprint/`.

Blueprint's useful contributions were the principles its rulebook already held: numbers must come from deterministic tools, never from a model; a checker must be independent and must not fix; a human gate is a stop, not a step that fires and proceeds; every output needs a record of where it came from. All four shaped SilverFile and all four held up under the brief's own constraints.

Its gaps were instructive. Its decision engine, given a one-job framing, recommended a workflow tool and no agent, and its rules recommended a chat app, neither acknowledging that the brief required six separate agents on LangGraph. It produced no design for the conversation with the taxpayer, although that conversation is most of the product. It had no place to list this system's stop conditions, no guidance on bounding a review-and-revise cycle or guarding against two agents handing work back and forth, and it did not ask for the context-strategy measurement that turned out to be the most convincing evidence in the deliverable. Two tool defects also showed: with a long brief, the "keep it as one job" path designs from a thousand-character slice, and the reading overlay can stick after that choice. These were recorded for the Blueprint project as general items, not as rules about tax.

The honest summary is that Blueprint is a stage-one tool. It settles whether an agent is warranted and which guardrails apply; it does not produce a running system, and on a prescriptive brief its output is a checklist to build against rather than a design to build from.

## 3. Architecture

**Topology.** A hub with one conditional router. Every worker returns to the Orchestrator, whose decision is one ordered table (`decide_next`). The brief's control flow is full of return paths: a document that contradicts an answer goes back to Intake; a transcription finding goes back to Extraction; a resolved escalation re-runs Eligibility. In a pipeline each of those is an edge; in a hub each is a row in a table that the tests cover row by row. The hub also gives the loop guard one place to stand, because the Orchestrator sees every handoff.

**The parallel branch.** Extraction fans out one `Send` per document and joins in an aggregation node. The shared key uses a custom reducer that merges by document id, newer version winning, so branches cannot overwrite one another and a re-extraction of one document replaces one record. A test runs the same four documents in forward and reverse order and asserts the final state is identical.

**The bounded cycle.** Review can send the return back to Computation, Extraction, Eligibility or Intake with findings, at most three times. A test injects a fault that Computation repeats every round and asserts exactly three failed rounds, then an escalation to a person rather than a fourth attempt. A second test injects a fault that Computation corrects on the next pass and asserts the return completes at version two.

**The loop guard.** Two detectors in the Orchestrator: a ceiling of forty handoffs among the working agents, and a repeated two- or three-agent pattern seen three times without reaching review. Intake is excluded, because a long conversation is dozens of legitimate handoffs, and the computation–review pair is excluded because it has its own bound. A test turns on a fault flag that makes Eligibility and Computation ping-pong and asserts that the guard trips, names the pattern, and hands the return to a person. After a person resolves it the log carries a reset marker so the guard judges only what follows.

**Escalation as an interrupt.** The escalation node calls LangGraph's `interrupt()` with the open item. The thread becomes a checkpoint row in SQLite and nothing runs until a person resumes it with a `Command`. The same mechanism carries the taxpayer conversation: every intake question is an interrupt, which is why a session abandoned on one day resumes on another at the same question with a recap, for free.

**Tools over MCP.** The `tax_math` server is a separate process exposing twelve pure functions: the standard deduction with the line 12d boxes, the senior deduction with its phase-out, the Social Security worksheet, the 2025 rate schedules, 1099-R taxable amounts, sums, differences and the settle step. Computation calls them over stdio and keeps a call log that becomes the inputs and transport of each line's provenance. A static test parses the Computation module and asserts it contains no arithmetic operator, no augmented assignment and no numeric built-in; the attempt counter lives in the Orchestrator and the test-only fault hooks in their own module, because the first version of that test found both.

## 4. Agents and models

| Agent | Model | Why that model |
| --- | --- | --- |
| Intake parser | Haiku | Frequent, cheap, low stakes: a wrong parse is corrected by the next question, and the slots are facts, not amounts |
| Extraction | Sonnet | Vision with a per-field confidence; on the synthetic W-2 images it read every box correctly at 0.98 confidence |
| Review | Opus | Must differ from the worker; its job is to disagree |
| Orchestrator, Eligibility, Computation, Escalation | none | Deterministic by design |

The reviewer is three layers: rule checks (every line has provenance, no unsourced amount, scope, filing status consistent with the facts), an independent recomputation of the derived lines from the leaf lines, and only when those pass, an adversarial read by Opus. Two things were learned there. First, a reply that is not the requested JSON must be a finding, not a pass: the first online run hit the output cap, the parser returned nothing, and the round passed with zero findings. Second, the reviewer needs the firm's rules for a draft: without them it reported "the Tax Table must be used instead of the rate schedule" as a medium finding on every clean return, which would have sent every return to a person after three rounds. With that context it attaches two low-severity notes for the preparer (the estimated payment and the age test both rest on what the taxpayer said) and passes the round.

## 5. Intake and accessibility

Intake is the only agent that speaks to the taxpayer, and the product lives or dies there. One question per turn, in plain words, with a recap on every resume. Answers are parsed into any slot, not only the one asked, so a correction three questions later still lands. The agent asks which filing status the taxpayer expects to use and never derives one; "I'm not sure" is recorded, and escalated only where it changes the return. Only the SSNs the form needs are requested, only the 900 test series is accepted in this proof of concept, and a number volunteered elsewhere is noted as an event and removed from the text. The transcript stored in the checkpoint never keeps a Social Security number, accepted or rejected; that last point was a gap the test suite found. Advice questions are acknowledged, declined, noted for the preparer and escalated. A third party answering without stated authority, or a refund account the taxpayer cannot describe as their own, pauses the return.

## 6. Human in the loop

Ten escalation kinds pause the return, each with a reason and a rulebook citation: out of scope, not eligible for the form, the deduction chart not applicable, a document conflicting with an answer, a 1099-R with box 2b checked, an advice request, an ambiguous filing status, a sign of exploitation, an unresolved review, and the loop guard. The dashboard shows each with the facts it depends on and the conversation so far, and accepts one of three resolutions: continue with corrected facts, halt so a preparer takes over, or accept the draft with the findings noted. The demonstration case is a widower whose wife died in May; the return pauses before any computation, the employee chooses married filing jointly, Eligibility re-runs with three boxes on line 12d, and the draft completes with a 4,000 overpayment. The resolution, who made it, when, and the note stay on the return.

## 7. Evidence

**Tests.** 212, all offline, about two seconds: the decision table row by row, the merge order, the loop guard, the review cycle, every escalation kind, the intake conversation (rejected real-looking SSNs, advice questions, volunteered numbers, going back, one question at a time, resume), the MCP round trip including an error crossing the wire, extraction, and the no-arithmetic check.

**Evaluations.** Ten cases in `evals/cases.json` with expected status, escalation sequence, line values and flags, run by `evals/run_evals.py`. The two demo cases, out of scope, box 2b, an advice request, two exploitation signals, a declared document missing, two W-2s, and the loop guard.

**Context strategy.** Ten W-2 images processed in one thread by Sonnet. Replaying the whole history costs 136,234 input tokens and climbs by about 2,500 per document; selective recall (a rolling line of decisions plus the current image) costs 28,195 and climbs by about 80. That is 79 percent fewer tokens and a flat curve. The table is in `docs/metrics.md`.

**Traces.** Real online runs of both cases, with the full state, the draft forms and the PDF, in `traces/`.

## 8. Limitations and what comes next

Docker is included and untested on the development machine. The form renders to HTML and a large-type PDF rather than onto the official IRS form. Vision extraction has only seen synthetic images; real scans will need a confidence threshold and a second pass on low-confidence fields. The intake copy was written for reading aloud but no usability session was held with older taxpayers, which is the first thing to do before anyone outside the firm touches it. The evaluation set should grow toward the awkward cases: married filing separately, dependents with their own SSNs, partial-year benefits, and documents that disagree with one another rather than with the taxpayer.

## 9. AI disclosure

Three Claude models run inside the product, none of which produces a number that reaches a line; the full table is in `docs/AI_DISCLOSURE.md`. The design stage used Blueprint. The code, tests, fixtures and documents were written with Claude Code from the brief, with the author directing the work, settling the architecture questions, reviewing the runs and deciding what to keep. No real taxpayer data was used.
