# Blueprint result for the SilverFile brief (2026-10-25)

Run: local dev server, AI on (Haiku first, Sonnet on a failed check). Short brief (994 chars), kept as one job.

## What Blueprint recommended

- AI decision: **a workflow with AI steps, no agent, main tool n8n**, cloud, autonomy level 2 (approve every output), Haiku to sort documents, Sonnet to read them, Opus as checker. Running cost $27–$110/month; build 1–4 weeks.
- Rules' version: a workflow with AI steps in **the Claude app with a Skill** (7 generic steps: start, sort by type, two AI steps, unclear items to a person, approve, done).
- Shortlist: n8n (recommended) vs Claude app. Neither option is LangGraph or a team of agents.
- The decision's own reason: "The steps are fixed and known in advance (sort, extract, assemble, check, approve), and the only judgement is reading the documents. A workflow with AI steps gives predictable, testable runs at lower cost than an agent."

## The AI design (16 steps)

start (preparer starts a draft) -> check uploads & tax year -> sort by form type (Haiku) -> three parallel reading steps (W-2 / 1099 / SSA-1099 & 1099-R, Sonnet) + unclear-items person -> deterministic merge -> spouse died? -> spouse-death sign-off (person) -> apply current-year tax rules (decision table) -> checker (Opus) <-> look up prior-year return (tool) -> preparer approves each draft (person; "sent back" returns to the tax step) -> save draft with source log -> end.

Design draft 1 was rejected once (a decision step lacked a knowledge citation) and repaired. Kit text: four "briefs" were dropped because the design has no agents; one build-notes item was too long and the section was dropped.

## What it got right (useful for the build)

- Every figure tied to its source record and box; "insufficient evidence" instead of guesses; checker flags but never edits; no batch approval; filing stays with a person.
- Parallel extraction per form type with a deterministic merge that flags duplicates and conflicts.
- A spouse-death pause for a trained employee, as a fixed rule (not an AI judgement).
- The checker is a different model from the worker.
- Sensible gotchas: logs leak PII; a retried save duplicates drafts; the merge can mix taxpayers; a checker with no external reference is weak.
- Owner questions worth answering in the README (how the system knows about a spouse's death; what happens to insufficient-evidence items).

## What it missed against the brief

1. **The architecture mandate.** The brief said "multi-agent system", "orchestrator", "LangGraph", "six agents is a firm rule". Blueprint has no input for a business-mandated architecture or platform, so both the rules and the AI chose what the answers imply (a workflow) and n8n/Claude. The decision prompt even argues against agents. LangGraph exists in the catalog but is only reached through agent approaches.
2. **No intake conversation.** The design starts with "Preparer starts a return draft" after images are uploaded. The one-question-at-a-time, resumable taxpayer dialogue (a large part of the brief and of the grade) does not appear anywhere; the interview answered "how does the work start" as "I'll start it myself".
3. **No eligibility step, no scope gate.** 1040-SR eligibility, filing-status validation and the out-of-scope list (Schedule C/E/D, 1099-NEC, foreign, digital assets, 1099-R box 2b) have no step; only the spouse-death pause survived as an escalation. Advice requests and elder-exploitation signals are absent.
4. **No deterministic tool boundary for arithmetic.** "Apply current-year tax rules" is a decision table, which is close, but the ordering dependency (taxable Social Security needs the rest of income first and feeds AGI) and the "every number from an MCP tool call" rule are not expressed.
5. **No review-and-revise cycle with a bound.** The checker flags; only the human "sent back" edge loops to the tax step. No loop guard.
6. **Invented a prior-year lookup tool** although the answers said "current tax year only", and assumed "the firm's tax software" as the save target.
7. **Context-window strategy, provenance UI, dashboard, resumability, test SSNs**: outside what Blueprint designs (it designs the flow, not the product surface).
8. **The split.** Both the long and the short brief were split into four jobs mapped onto the agents (intake, extraction, compute, review), with an invented handoff store. Eligibility and the orchestrator never appeared as jobs. "Keep it as one job" on the long brief would have designed from a 1,000-character slice of job 1 (see 01-split.md).

## Learnings to take back (general, not tax-specific)

- A way to state a **given architecture or platform** ("the business has decided on X agents / on LangGraph") so the rules and the AI design within it, and say what they would have chosen otherwise.
- The interview's **"how does the work start" / conversational intake** gap: a task whose input is a dialogue with a person (one question at a time, resumable) is not a "manual start"; it is a conversational intake step that the design should contain.
- **Scope and escalation lists in the brief** should become decision steps (a scope gate) rather than vanish; the design keeps only the escalations the limits force it to keep.
- **Review loops**: when the brief says "checker sends work back", the design should carry a bounded loop, not a one-way checker plus a human send-back.
- **Keep-as-one-job truncation** and the stuck "reading" overlay after keep-as-one are bugs.
- A brief longer than 1,000 characters cannot be designed as one job; the split is forced.
