# Blueprint answers for the SilverFile brief (short brief, kept as one job)

Title Blueprint gave the task: "Prepare draft Form 1040-SR returns with source provenance for human review". 9 of 12 answered from the description.

| Question | Answer | Read from the description |
|---|---|---|
| Same steps every time, or reading and judgement? | The same steps, but some need reading or judgement | Fixed stages but requires reading W-2 and 1099 images and flagging document conflicts |
| Distinct types needing different handling? | Yes, a few clear types | Separate cases (document conflicts, advice requests, elder abuse signs) that each pause or route differently |
| How do the parts build on each other? | Each step builds on the last | Stages in order, only W-2/1099 extraction in parallel |
| Could you write down what a good result looks like? | Yes, as a clear checklist | No amount without a source, deterministic tax maths, cited eligibility check |
| What should it draw on every time? | House rules/playbook, Reference material | Cited corpus for eligibility; fixed intake and escalation rules |
| Work with other systems? | It needs to look things up | Reads images and checks a corpus; never files |
| How does the work start? | I'll start it myself when I need it | The taxpayer starts and resumes the intake |
| Volume | Dozens of times a day | (asked) |
| Which are true? | Customers/partners see the output; handles personal or confidential information | SSNs and income documents; never files, so irreversible harm not implied |
| Limits on location/providers? | No special limits | (asked) |
| Organisation | Larger organisation with developers or IT | (asked) |
| Essential requirements? | A person must approve every result before it is released or acted on | Pauses go to a Kushim employee; human reviews the draft |

Task-specific questions the AI added:
- How will W-2 and 1099 images reach the system? -> The taxpayer uploads them through the intake
- Who signs off on a pause involving a spouse who died in the year? -> Any trained Kushim employee
- Which tax years must a draft cover? -> Current tax year only

Observations:
- "It decides or recommends something about a person" was not ticked. A draft tax return is prepared for a human; the system doesn't decide an outcome, so this reads right, but a tax position affects a person's money and the gap is worth noting.
- UI: after "Keep it as one job" the reading overlay never cleared even though /api/interview returned 200 (one duplicate request was aborted). A reload fixed it; the adaptive wording survived for some questions (location had taxpayer-data wording) but not all (volume showed the standard wording).
