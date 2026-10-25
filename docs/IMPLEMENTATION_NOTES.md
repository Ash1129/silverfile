# Implementation notes

What was built, what was decided along the way, and what is known to be unfinished. Written for the person who picks this up after the course.

## 1. How the design stage went (Blueprint)

The brief was first run through **Blueprint**, the agent-architecture guide built earlier in this course, to see how much of the design it could produce before any code. The full run record and both starter kits are in `docs/blueprint/`. Summary of what it gave and what it missed:

- **It gave** a sound decision on *whether* an agent is warranted, a reasonable component list (deterministic tax tools, an audit step, a human gate), and a starter kit with a rules checklist. The "numbers come from tools, never from the model" principle and the "reviewer must not fix" principle were both in its rulebook before this project existed.
- **It missed** the brief's explicit mandate for six separate agents on LangGraph (its decision engine recommended a workflow tool and no agent for the one-job framing), produced no intake-conversation design, no scope gate, and no bounded review loop. Its one-job path also designed from a 1,000-character slice of a long brief, which is a Blueprint bug recorded for that project.
- **Net effect on this build:** Blueprint's output shaped the provenance record and the "deterministic tools + adversarial reviewer" split; the graph topology, the intake design and the escalation model were designed here from the brief. The learnings were fed back to Blueprint as general items, not as tax rules.

## 2. Decisions

**Hub-and-spoke, not a pipeline.** `decide_next` is one ordered decision table. Each row is a test. Adding a new "go back to X" path is a row, not an edge.

**Escalation as `interrupt()`.** The alternative, a polling "waiting" node, would keep the thread live. With an interrupt the thread is a checkpoint row in SQLite and nothing runs until a person resumes it. The same mechanism serves the taxpayer conversation: every intake question is an interrupt, so the chat is resumable for free.

**One reducer per shared key.** `extracted` uses `merge_documents` (by document id, newer version wins) because the extraction branches run in parallel and a re-extraction must replace one record only. `escalations` uses `merge_escalations` (by id, resolved beats open). An earlier version used `operator.add` for escalations and the open copy survived every resolution, which produced an infinite escalation loop; that bug is why the merge is by id now.

**Computation calls tools over MCP stdio, in a subprocess.** The `tax_math` server is a separate process with twelve pure functions. The client keeps a call log (tool, arguments, result, transport, duration) that becomes the `inputs`/`transport` part of each line's provenance. Tests set `SILVERFILE_TAXMATH_DIRECT=1` to call the same functions in-process; the transport is recorded either way, so a test can assert that a real run used `mcp-stdio`.

**Review is three layers.** Rule checks (every line has provenance; no unsourced amount; scope; filing status consistent with the facts), an independent recomputation of the derived lines from the leaf lines, and only if those pass, an adversarial read by a different model. The model's findings carry a severity; `high`/`medium` fail the round and name a target agent, `low` is attached to the round for the preparer. A reply that is not the requested JSON (for example one cut off at the output cap) is itself a finding, not a pass.

**Reviewer context.** The reviewer is told the firm's rules for a *draft*: line 16 is the 2025 rate-schedule formula (the Tax Table banding is the preparer's step at filing), the deduction figures are the rulebook's, and intake-stated amounts are legitimate draft sources to flag at low severity. Without that it reported "Tax Table must be used" as a medium finding on every clean return, which would have sent every return to a person after three rounds.

**Loop guard scope.** Only the working agents count toward the handoff ceiling and the pattern detector. Intake is excluded because a long conversation is dozens of legitimate intake→orchestrator handoffs. The computation↔review pair is excluded because that cycle has its own bound. After a person resolves a loop-guard stop the log carries a `reset` marker so the guard judges only what follows.

**Context strategy (section 4.5).** `ContextWindow` in `memory.py` builds the prompt for a model step in a long thread. The baseline replays everything, images included. The strategy keeps a rolling line of decisions plus the current item. Measured over ten W-2 images with Sonnet: 136,234 input tokens against 28,195 (79% fewer), a curve that climbs by about 2,500 tokens per document against one that climbs by about 80. Table in `docs/metrics.md`.

**Models.** Haiku parses intake answers into slot updates (cheap, frequent, low stakes: a wrong parse is corrected by the next question). Sonnet reads document images (vision, with a per-field confidence and a free-text issues list). Opus audits (the reviewer must differ from the worker). None of them produce a number that reaches a line.

## 3. Tax rules

All rule constants live in `tax_math/rules.py` with a `REF-*` citation to the brief's rulebook sections. The 2025 figures used: base standard deduction 15,750 / 31,500 / 23,625; additional 2,000 per box (single, head of household) or 1,600 otherwise; senior deduction 6,000 per qualifying person, reduced by 6% of MAGI over 75,000 / 150,000; Social Security bases 25,000 / 32,000 and 34,000 / 44,000 (zero for MFS living with the spouse); 1040-SR eligibility is "born before 2 January 1961"; income tax from the 2025 rate schedules.

## 4. Known gaps

- Docker is included and untested locally: no Docker daemon on the development machine.
- The form renders to HTML (and a text-positioned PDF) rather than onto the official IRS PDF; the line numbering follows the 2025 1040-SR.
- The `chat` dashboard page is a plain form, not a voice or large-type interface. The intake copy was written for reading aloud, but no usability test was run with older taxpayers.
- Vision extraction is tested on synthetic images only. Real scans will need the confidence threshold tuned and probably a second pass on low-confidence fields.
- Eval cases are the four fixtures plus the batch set. A wider set of edge cases (MFS, dependents with SSNs, partial-year Social Security) is the obvious next step.
