# Orchestration: who acts next, and when it stops

Role: Task division and management. Author: Mahika Arora.

This note explains the Orchestrator: how it picks the next agent, where a return
enters the graph, how the review cycle and the loop guard are bounded, and which
test covers each rule. The code is `src/silverfile/agents/orchestrator.py` (the
decision) and `src/silverfile/graph.py` (the wiring). The README has the overview
and the topology picture (`docs/graph.png`); this is the detail behind it.

## 1. What the Orchestrator is, and is not

- It is the only node that decides where a return goes next. Every other agent
  hands the return back to it (`START -> orchestrator`, and every spoke has an
  edge back to `orchestrator`). The one exception is `extract_document`, which
  joins at `aggregate_extraction` first. There are seven worker nodes plus `finalize`:
  intake, extraction, eligibility, computation, review, escalation and
  `resolution_check` (the code-only checker for a person's decisions, section 7).
- It is **deterministic**: no language model, no tools, no arithmetic. Its
  decision, `decide_next(state)`, is a pure function of the return's state. The
  same state always gives the same answer, and it does not change the state.
- It never extracts, computes or speaks to the taxpayer. It also owns
  termination: a return ends only when the Orchestrator routes to `finalize`.

Why a hub and not a fixed pipeline: the control flow is full of "if this, go back
there" (a review finding goes back to Computation; a resolved escalation re-runs
Eligibility). In a pipeline each of those is its own edge. In a hub they are rows
in one table, and every handoff passes through one place, which is also where the
loop guard has to sit.

## 2. Entry points: where a return enters, and when each agent is engaged

There is one graph entry point: `START -> orchestrator`. What makes the entry
point flexible is that the Orchestrator reads the state before choosing, so a
return **starts wherever its state says it should**:

| Return starts with | First stop | Why |
| --- | --- | --- |
| Nothing confirmed (the live chat, `run.py chat`) | Intake | `intake_complete` is false |
| A confirmed intake plus uploaded documents (`run.py demo`, tests, seeded returns) | Extraction | Intake is already done, documents are not read |
| A return resumed after a person's decision | `resolution_check` first, then whatever the check allows | The decision is recorded as a `resolution_request`, which the Orchestrator sends to the checker before anything else (section 7) |
| A return where a person finished editing facts | Eligibility, then Computation, then Review | `recompute_stage` is set, and each step names the next |

Each agent is engaged only when its precondition is missing, in this order:

| Agent | Engaged when | Rule in `decide_next` |
| --- | --- | --- |
| Intake | the taxpayer's basics are not confirmed | `not intake_complete` |
| Extraction | documents are not read, or Review asked for a re-read | `not extraction_done`, or `revise_target == "extraction"`, or an open work order for it |
| Eligibility | there is no eligibility record, or Review/a resolution asked for a re-check | `not eligibility` or `revise_target == "eligibility"` |
| Computation | there are no lines yet, or Review asked for a recompute | `not lines` or `revise_target == "computation"` |
| Review | the draft exists and has not passed, and the round bound is not reached | the final fall-through |
| Escalation | any escalation is open | `open escalations` (checked before the working steps) |
| Resolution check | a person has submitted a decision (Continue, Resolve review) | `resolution_request` is set |
| Finalize | review passed, a person halted it, the return is out of scope, or the loop guard tripped | several rows, see below |

Escalation is checked **before** the working steps on purpose: if a person is
needed, nothing else should run on the return until they decide.

## 3. The routing table

`decide_next` is an ordered list. The first row that matches wins, so order is
the priority. Rows in the order the code checks them:

| # | If the state says | Next | Test |
| --- | --- | --- | --- |
| 1 | status is `complete` or `halted` | end | `test_finished_returns_end`, `test_a_finished_return_ends_even_mid_recompute` |
| 2 | a recompute stage is set (a person finished editing facts) | that stage: eligibility, computation or review | `test_a_recompute_stage_routes_to_that_agent` |
| 3 | a person's decision is waiting to be checked | resolution check | `test_a_pending_resolution_goes_to_the_resolution_check` |
| 4 | the handoff log shows a loop and it has not been handled | loop guard | `test_loop_guard.py` |
| 5 | the loop guard already tripped | escalation if one is open, else finalize | `test_decide_next_routes_to_loop_guard_then_escalation_then_finalize` |
| 6 | an escalation is open | escalation | `test_open_escalation_goes_to_a_human`, `test_escalation_beats_every_later_step` |
| 7 | a halt reason is set | finalize | `test_halted_reason_goes_to_finalize` |
| 8 | intake is not complete | intake | `test_intake_first_when_the_basics_are_incomplete` |
| 9 | documents are not extracted | extraction | `test_extraction_when_documents_are_not_read` |
| 10 | no eligibility record, or Review asked for it | eligibility | `test_eligibility_when_it_has_not_run`, `test_eligibility_again_when_review_asks_for_it` |
| 11 | not eligible, out of scope, or the chart does not apply | finalize | `test_not_eligible_or_out_of_scope_goes_to_finalize` |
| 12 | Review asked for a re-read | extraction | `test_extraction_again_when_review_asks_for_it` |
| 13 | no lines yet, or Review asked for a recompute | computation | `test_computation_when_no_lines_exist_yet`, `test_computation_again_when_review_asks_for_it` |
| 14 | review passed | finalize | `test_review_passed_goes_to_finalize` |
| 15 | a finding needs a person, or the round bound is reached | review unresolved (escalation) | `test_a_finding_that_needs_a_person_goes_to_review_unresolved`, `test_the_review_bound_ends_the_cycle` |
| 16 | a work order for extraction/eligibility/computation is open | that agent | `test_open_work_orders_route_each_addressee_before_review_again` (test_comms.py) |
| 17 | none of the above | review | `test_review_runs_when_the_draft_is_assembled_and_unreviewed` |

Rows 2 and 3 were added with the human-review change: a person's edit or
decision is carried through before anything else is routed, even before an open
escalation or the loop guard. `test_human_review_steps_beat_escalation_halt_and_review_findings`
covers that.

Priority is itself tested, since "an earlier row beats a later one" is where
routing bugs hide: `test_escalation_beats_a_halt`, `test_halt_beats_intake`,
`test_intake_beats_extraction`, `test_extraction_beats_eligibility`,
`test_the_scope_exit_beats_a_computation_request`, and
`test_review_passing_beats_the_round_bound`. Purity is tested by
`test_decide_next_does_not_change_the_state`.

`orchestrator_node` then acts on the decision: it numbers each Computation
attempt, sets the status for the dashboard, raises the `review_unresolved` or
`loop_guard` escalation itself, clears `revise_target` once it has acted on it,
and posts the handoff `request` on the message bus. Those behaviors are covered
by the four `test_node_*` tests in `tests/test_routing_table.py`.

## 4. The parallel branch

When the next stop is Extraction, `route()` returns one `Send("extract_document", ...)`
per uploaded document. The branches run in parallel and join at
`aggregate_extraction`, which then returns to the Orchestrator.

The risk with parallel writes is that one branch overwrites another, or that the
result depends on which branch finishes first. The state key that collects the
results uses a reducer, `merge_documents`: records are keyed by document id, a
higher version replaces a lower one, and the same version twice is kept and
flagged as a duplicate. `tests/test_merge_order.py` checks the reducer in
isolation and checks through the graph that reversing the document order gives
the same final state. The aggregation step is recorded as **one** handoff, not
one per document, so parallel branches never trip the loop guard.

## 5. The review cycle (Reflection) and its bound

Review recomputes the totals independently, checks that every amount has a
provenance record, and (online) asks a different Claude model to read the draft
adversarially. It **cannot change a number**. It can only report findings, each
with a `send_to` (computation, extraction, eligibility, intake, or human), and
the Orchestrator routes the return back to that agent.

The cycle is bounded by `MAX_REVIEW_ROUNDS = 3` in `state.py`. When the round
counter reaches the bound without a pass, or a finding needs a person, the
Orchestrator raises a `review_unresolved` escalation and stops the cycle. Tests
in `tests/test_review_cycle.py`:

- `test_missing_provenance_is_caught_once_and_the_recompute_passes`: one fault,
  fixed on round 2, return completes.
- `test_persistent_corruption_hits_the_review_bound_then_escalates`: a fault that
  never goes away stops after exactly 3 rounds and reaches a person.
- `test_accepting_an_unresolved_review_cannot_complete_an_unsourced_amount` and
  `test_accepting_a_sourced_but_unresolved_review_does_not_complete_the_draft`: Accept on an
  unresolved review is refused (the return stays `waiting_human` with the blocking event
  open and the required actions listed), so a failing draft cannot be pushed through.
- `test_review_never_changes_a_figure`.

## 6. The loop guard

The review cycle has its own bound; the loop guard catches everything else. It
has two detectors in `detect_loop`:

- **Ceiling:** more than `MAX_HANDOFFS = 40` handoffs among the working agents.
- **Pattern:** a 2- or 3-agent sequence repeated `LOOP_PATTERN_REPEATS = 3` times
  (for example eligibility, computation, eligibility, computation...).

Two exemptions keep it from firing on legitimate behavior: Intake turns do not
count (a long conversation is normal), and the computation/review pair is
exempt because that cycle already has `MAX_REVIEW_ROUNDS`. When the guard fires
it writes a `loop_guard` escalation and the return pauses for a person. After the
person resolves it (through `resolution_check`), a `reset` marker in the handoff log
makes the guard judge only what happens afterwards.

Tests: `tests/test_loop_guard.py` covers the detector cases (clean run, the
pattern at exactly 3 repeats and not at 2, the exemptions, the ceiling, the
reset marker) and runs a fault-injected graph in which Eligibility and
Computation hand the return back and forth. It asserts the guard trips after
exactly three repeats, before Extraction or Review ever run, and that a person's
halt ends the return.

## 7. How a person re-enters the loop (HITL)

`escalation_node` calls LangGraph `interrupt()`, which checkpoints the thread and
waits. The payload shows the open item, the actions still required, and the last
check result. A person acts through the dashboard; the action is one of `continue`,
`halt`, `accept`, `start_edit`, `save_edit`, `complete_edit`, `resolve_review`, or
saving an intervention note. What happens next:

1. **`halt`** is always allowed: the return ends at `finalize` with the reason.
2. **`continue` and `resolve_review`** do not take effect immediately. The escalation node
   records a `resolution_request`, the Orchestrator routes it to **`resolution_check`**
   (row 3), and that node, which is plain code with no model, decides **yes or no**
   from deterministic rules (eligibility and scope still hold, no unconfirmed required fact,
   no unresolved document conflict or box 2b, and so on). On "no" the escalation stays open and the
   unmet requirements are listed for the person. On "yes" the escalation resolves.
3. **Editing facts** (`start_edit`, `save_edit`, `complete_edit`): completing an edit
   clears the old lines and eligibility and sets `recompute_stage`. The Orchestrator
   then runs Eligibility, Computation and Review in that order (row 2), each step
   naming the next, so a person's correction is always recomputed and rechecked.
4. **`review_unresolved`** opens a **blocking review event** with required actions.
   `accept` is refused until the actions pass, a person explicitly resolves the
   event, and a saved explanation is on the log. Even then `finalize` still refuses
   an unsourced amount and refuses to finish while a human review is incomplete.

Tests: `tests/test_escalations.py` and `tests/security/test_sec_hitl.py`.

## 8. Mapping to the four design patterns

| Pattern | Where it lives |
| --- | --- |
| Reflection | Review's independent recompute and provenance check, and the bounded review -> revise cycle (section 5) |
| Tool use | Computation and Eligibility call the `tax_math` MCP server; each agent can call only its own functions; no language model has tools |
| ReAct (reasoning, then action) | The Orchestrator's loop: read the state (reasoning), pick and run one agent (action), read the new state, repeat. It is rule-based, not a model reasoning in free text |
| Human in the loop | `interrupt()` in `escalation_node`, the deterministic `resolution_check`, the blocking review event and the recompute path in section 7 |

## 9. Known gaps (found while writing this note)

This is an open question for the team, not something this note changes.

**A Review finding aimed at Intake goes to Computation.** In `decide_next` row
13, `revise_target == "intake"` routes to computation. Intake is never re-run,
because it is the only agent that talks to the taxpayer. The README and the
report say a document that conflicts with an answer goes back to Intake.
Today that case is handled by an **escalation to a person**, not by re-running
Intake. Either the wording or the routing should change. The test
`test_a_review_finding_aimed_at_intake_currently_goes_to_computation` records
the current behavior, so whoever changes it will see this test fail on purpose.

## 10. Running the tests for this area

```
pytest -q tests/test_routing_table.py tests/test_loop_guard.py tests/test_review_cycle.py tests/test_merge_order.py tests/test_escalations.py tests/security/test_sec_hitl.py tests/test_orchestration_flow.py
```
