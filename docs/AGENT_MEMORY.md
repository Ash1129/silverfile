# Agent memory

*Agent memory: Daniel Akinwale.*

Code: `src/silverfile/memory.py`, used by `agents/intake.py`, `agents/escalation.py`, `service.py`, `dashboard/app.py` and `run.py chat`. Tests: `tests/test_memory.py`. Measurement: `docs/metrics.md`. Dashboard: the **Changed answers** card on each return, and the recap at the top of the taxpayer's page after a break.

SilverFile remembers four things, each kept for a different reader:

| Memory | Kept in | For | How long |
| --- | --- | --- | --- |
| Session state: every answer, document, line, escalation and message | LangGraph checkpoints in SQLite, one thread per return, encrypted at rest | The graph itself: resuming at the same step | Until the return is deleted; an abandoned return is purged after 30 days (`SILVERFILE_RETENTION_DAYS`) |
| Working summary | Computed from the state when asked (`session_summary`); a copy in the dashboard's index, encrypted | The taxpayer coming back, and staff scanning the list | Recomputed on every change; nothing to go stale |
| Answer history | `intake["_answer_history"]`, inside the checkpoint | The preparer: what the taxpayer said before they changed it | With the return; the newest 200 changes |
| Model context | Built per call (`ContextWindow`); never saved | The model reading the next document | One call |

## 1. Session state and resuming

Every graph step is checkpointed (`get_checkpointer` in `graph.py`), and every intake question is a LangGraph `interrupt`. A session is therefore never "in memory" in the process sense: closing the browser, stopping the server or restarting the container loses nothing, and the next visit opens at the question that was waiting. `test_a_session_left_on_tuesday_resumes_on_friday_in_a_new_process` answers four questions, drops the graph, compiles a new one on the same SQLite file and checks that the same question is waiting, the recap names what was covered, and the next answer carries on from there.

**The recap.** The brief asks that a session resumed on Friday continue "with a summary of what was already covered". The waiting question was written on Tuesday, before anyone knew there would be a break, and it is stored in the checkpoint as it was asked. So the recap cannot be part of the question. It is added when the question is *shown*: `with_resume_recap` puts "Welcome back. Here's where we were: …" in front of it if the last answer is more than 30 minutes old (`RESUME_AFTER_MINUTES`). The taxpayer's page (`service.pending_for_taxpayer`) and `run.py chat --resume` both use it. The stored question is not changed, so a reload shows the recap once, not twice, and the interrupt id the form carries still matches.

Two places get no recap on purpose. Staff pages show the question as it was asked, because "Welcome back" there would read as if the system were talking to the employee. The first question ("Are you the taxpayer?") gets none, because the person coming back may not be the person who started, and that question is how the system finds out.

Before this change the recap was written only by the intake node running again, which happens after the waiting question is answered. The taxpayer saw the bare question and the recap went into the transcript afterwards, where nobody needed it (`docs/report.md` §5 recorded this as an open gap).

## 2. Answer history: earlier answers are never lost

The brief's dignity rules say the taxpayer may "re-answer, contradict themselves, wander, or ask you to go back" and that losing their previous answers is a failure. The intake already kept every *other* answer when one was corrected. But the corrected answer itself was simply overwritten. Going back ("No, my date of birth is wrong") deleted the old value so the question could be asked again. That works for the conversation, but the preparer could not see that the taxpayer had said something else first. For an older taxpayer, a contradiction is often exactly what a preparer should ask about.

`record_answer_changes(before, after, slot=..., at=...)` compares the facts before and after each turn and appends one entry per answer that changed:

```json
{"at": "2026-10-30T15:00:00+00:00", "slot": "confirm", "field": "taxpayer.dob", "old": "1950-03-14", "new": null, "how": "went_back"}
```

`how` is `answered` (it had no value), `corrected` (a different value replaced one), `went_back` (cleared to be asked again) or `kushim` (a Kushim employee changed it while resolving an escalation, in `agents/escalation.py`). The facts still hold only the newest value, and every agent downstream reads only that. The history is a record for people, not a second source of truth for the return.

Design choices:

- **A diff, not a log of parses.** Recording what changed in the facts, rather than what the parser returned, covers every path the same way: a direct answer, a correction volunteered in another answer, going back, the closing summary, and an employee's edit. A path added later is covered without anyone remembering to log it.
- **No SSNs.** An SSN field is recorded as changed but its value is replaced with a placeholder, and dependents are copied without theirs. The brief's PII rule applies to the history as much as to the transcript: a second copy of a number is a second thing to protect. `test_the_history_never_holds_a_social_security_number` searches the history for both test SSNs after a correction.
- **Bookkeeping is not an answer.** Keys starting with `_` (turn counter, last activity, glosses given), noted questions and security flags are left out, so the history is only what the taxpayer said.
- **Bounded.** The newest 200 changes are kept. The bound exists because the history lives in every checkpoint; a scripted loop of corrections could otherwise grow the state without limit. A real conversation is far below it.

The dashboard's **Changed answers** card lists every entry with an earlier value (first answers are left out as noise), and the working summary says how many answers changed.

## 3. Model context (§4.5)

`ContextWindow` builds the messages for each model call in the ten-document batch (`run.py batch`). With the strategy on it uses **selective recall**: one line summarising the decisions so far, plus the current document. Once there are more than 12 decisions the oldest fold into a single "earlier: N items processed" line (rolling summarisation), so the summary itself cannot grow without limit. With `--no-context-strategy` it replays every earlier document and reply, images included, which is what a naive single thread does.

Measured in `docs/metrics.md`: input tokens for the tenth document are 24,814 without the strategy and 3,190 with it, and 136,234 against 28,195 over the batch (79% fewer). The tests check the same shape offline: prompt size strictly grows without the strategy, stays within 1.5 times its first size with it, folds old decisions, and keeps an image item's image while adding the recap as text.

Selective recall was chosen over trimming the oldest messages because transcribing one W-2 does not need the earlier images at all, only the one-line decision recorded for each. Trimming would still replay the most recent images, which cost the most tokens and say nothing about the current document.

## Failure modes and limits

- **A clock change or a wrong server clock** can show the recap too early or not at all. Either way the cost is one sentence, never a lost answer.
- **The 30-minute threshold is a guess**, not a measured value. A taxpayer who steps away for 20 minutes gets no recap; the question on screen is still the one they left.
- **The history records what changed, not why.** "Corrected" does not say whether the taxpayer misspoke or the parser misread them; the transcript beside it does.
- **The recap is a summary of facts**, not of the conversation: it says what was answered, not which questions were declined or noted for the preparer beyond a count.
- **Model context is not saved.** The batch keeps its window in the process, so a batch interrupted halfway starts again from the first document. It is a measurement run, not a taxpayer's session, so nothing a person typed is lost.
- **The decision line is written by the model.** With selective recall the model sees earlier documents only through its own one-line decisions, so a wrong decision is carried forward. It cannot change a number on the return (every value is checked and computed elsewhere), but it can mislead the next transcription.

## Tests

`tests/test_memory.py`: resuming in a new process; recap present only after a break, shown once, absent for staff, escalations and the first question; history entries for going back, correcting and an employee's edit; no SSN in the history; bookkeeping left out; the bound; the summary's count; the dashboard showing the recap to the taxpayer and the changed answer to staff; and the context window's growth, flatness, folding and image handling.
