# Blueprint split (AI) for the SilverFile brief

"This is four jobs."

1. Hold a plain-language intake conversation to collect the filing details a 1040-SR draft needs
   - Hands over through SilverFile session store when the taxpayer confirms the document inventory and the documents are supplied.
   - Hands over to job 3 through SilverFile session store when the intake answers for filing status, age, blindness, dependents, adjustments and estimated payments are confirmed.
2. Extract typed, sourced figures from the taxpayer's W-2, SSA-1099, 1099-R, 1099-INT and 1099-DIV documents
   - Hands over through SilverFile session store when all supplied documents are extracted and aggregated into a sourced record set.
3. Assemble the 1040-SR draft with a provenance record for every dollar amount
   - Hands over through SilverFile session store when the draft return is fully populated with a provenance record for every amount.
4. Independently check each draft return against its provenance and route held returns to a Kushim employee
   - Hands over to job 3 through SilverFile session store when the review finds a contradiction, a missing provenance record or a failed total, and sends it back for revision.

Choice made: "Keep it as one job". The brief describes one orchestrated multi-agent system
(one graph, one orchestrator, a review cycle back into computation). The split mapped the
agents inside that system onto separate jobs with an invented handoff system.
Eligibility & Rules and the Orchestrator do not appear as jobs.

## Finding: "Keep it as one job" truncates

`onKeepOne` in `src/App.tsx` uses the original text only when it is at most
`MAX_DESCRIPTION` (1,000 characters, `src/lib/interview.ts`). Otherwise it joins
the split's job descriptions and cuts them at 1,000 characters, so an 8,500-character
brief kept as one job was about to be designed from the first job's description
("Talk with the taxpayer to collect filing status...") alone: intake only, no
extraction, computation, review or escalation. The page showed that text under
"Your problem, in your words", which is not the owner's words.

Workaround used for this run: a description under 1,000 characters (brief-short.txt).
