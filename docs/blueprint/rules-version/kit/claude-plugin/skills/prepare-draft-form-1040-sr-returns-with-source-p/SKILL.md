---
name: prepare-draft-form-1040-sr-returns-with-source-p
description: >
  This skill should be used when the user asks to "prepare draft form 1040-sr returns with source provenance for human review", or hands over work of that kind.
  It follows the business's playbook and stops to ask a person when unsure.
metadata:
  version: "0.1.0"
---

# Prepare draft Form 1040-SR returns with source provenance for human review

## Steps

1. **Sort by type.** A quick AI step reads each item and decides which type it is, so it can go down the right path.
2. **Most common type.** This path has its own instructions and tools for the most frequent type of request. It follows the steps in your Skill. It draws on your reference library rather than guessing. (Checked against checklist)
3. **Other known types.** Each other known type gets its own path like this one, with instructions tuned to it. It draws on your reference library rather than guessing. (Checked against checklist)
4. **Unclear items.** Anything the sorting step isn't confident about goes straight to a person instead of being guessed.
5. **You approve each result.** A named person reviews every result with the checklist beside the source material and any item marked insufficient evidence, one case at a time, and approves it, changes it or sends it back. No batch approval.

## Tools

Use the connected tools to look things up. Do not change anything.

## House rules

[Fill in: the steps, templates and rules a good employee follows for this task. Be specific, and include one example of a great result.]

## Stop and ask a person when

- After a set number of failed attempts (start with 3), stop and hand the case to a person.
- Customer-facing output is reviewed by a person until it has a proven track record.
- Agree in advance what track record earns the next level, for example four weeks without a serious error.
- A named person approves every result before release or action; reject, timeout and missing approval stop the run.
- The reviewer sees the checklist or requirements beside the source material and any item marked insufficient evidence, and approves, changes or rejects one case at a time. No batch approval.
- Track the override rate and the time per review. Overrides near zero, or reviews that take seconds, mean the review has become a rubber stamp.
- Seed known-wrong cases into the queue, so catching them is measured.