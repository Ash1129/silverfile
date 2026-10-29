# Intake dignity and safety (Guardrails B)

Owner: Gabriel Maurad (Guardrails B: dignity and intake safety). The Intake agent itself was built by Ashwin; the SSN validator and redaction (`security/pii.py`) are Sasha's. This note covers the dignity rules of brief §4.3 as implemented in `src/silverfile/agents/intake.py`, why each one fits the people SilverFile serves, and the test that proves it. The tests are in `tests/test_intake_dignity.py`: scripted "messy" taxpayers who repeat themselves, wander, change their minds, mention their family, say "I don't know", ask for advice, or let someone else take over the keyboard.

The person on the other end may be tired, hard of hearing, using a screen reader, anxious about money or embarrassed not to understand taxes. Every rule below is meant to make the conversation easier for that person to follow. None of them exists to make the system look careful.

## 1. One question at a time

**Choice.** Each turn is a single LangGraph interrupt carrying one question, and every question has exactly one question mark. Two questions failed that rule and were rewritten:
- estimated payments ("Did you send…? If so, roughly how much?") became "In total, how much did you send the IRS in estimated tax payments during 2025? Many people sent none, and 'none' is a fine answer."
- dependents ("Will you list any…? Say none, or tell me the first one's name and…") became "Who is the first dependent you'll list?" with an example.

**Why.** A screen reader reads a compound question as one long sentence. A tired or anxious person answers the first half and forgets the second. The parser also then has to guess which half the answer belongs to.

**Test.** `test_every_question_is_one_question_in_plain_language` walks a full single and a full married conversation and checks every question. The old suite allowed up to two question marks per question.

## 2. Plain language

**Choice.** A glossary (`GLOSSES`) adds a one-line explanation right after the first sentence that uses a tax term: filing status, standard deduction, dependent, estimated tax payment, adjustments to income, legally blind and withholding. Each term is explained once per session, and the terms already explained are kept in the session's facts (`_glossed`), so they survive a resume. A list of jargon (`JARGON`: MAGI, AGI, provisional income, MFJ, QSS and the like) must never appear in a question.

**Why.** The brief's example is "modified adjusted gross income": gloss it the first time, and ideally never say it. Repeating an explanation every time is patronising and makes a screen-reader user listen to the same sentence again. Explaining it only once, at the first use, avoids that.

**Test.** `test_tax_terms_are_explained_once_the_first_time_they_appear`, and the jargon check in test 1.

## 3. Tolerance of repetition and drift

**Choice.** Answers are kept per field. A correction clears only the field it names. An answer that doesn't fit the question ("oh, sometime after the war") gets a gentle re-ask with an example, and nothing already given is touched. "No, my date of birth is wrong" at the final summary reopens only the date of birth.

**Why.** Losing an earlier answer is the failure the brief names. Making an older person start again because they wandered is exactly the experience SilverFile is supposed to replace.

**Tests.** `test_a_correction_replaces_only_that_answer`, `test_an_answer_that_does_not_fit_is_asked_again_without_losing_anything`, `test_a_contradiction_at_the_summary_reopens_only_that_answer`.

## 4. No unnecessary PII, and volunteered SSNs

**Choice: what is asked.** SilverFile asks only for the SSNs the form prints:
- the taxpayer's;
- the spouse's, only on a joint or separate return or after a death in the year (this question was missing before, so a joint return printed a blank);
- each dependent's.

A single filer is asked for exactly one. Only 900-series test numbers are accepted. A real-looking number is refused with a plain explanation and is not kept, not even in the transcript.

**Choice: what happens to an unrequested SSN.** When someone types an SSN where none was asked for (for example "No. Here's my social, ...", using a synthetic value from the test inputs):
1. The number is removed from the answer before anything is parsed or saved. It never reaches a model, the facts, the transcript, a log or a trace.
2. The rest of the answer still counts ("No" is still no).
3. The person is told, in one plain sentence: "I noticed a Social Security number in your answer. I didn't need it here, so I haven't kept it."
4. An event recording *that* a number was volunteered (not the number) is kept, so a preparer can see that it happened.

**Why this choice.** Volunteering an SSN is a sign of trust, or of habit from years of being asked for one. Ignoring it silently would leave the person wondering where their number went. Storing it "just in case" would create exactly the data an identity thief wants. Rejecting the whole answer would punish them for being helpful. Saying plainly that it wasn't kept respects the person and protects them.

**Tests.** `test_a_single_filer_is_asked_for_exactly_one_ssn`, `test_the_spouse_ssn_is_asked_only_for_a_joint_return_and_must_be_a_test_number`, `test_an_unrequested_ssn_is_not_kept_and_the_person_is_told`, and Sasha's `tests/security/test_sec_ssn.py`.

## 5. Honest uncertainty

**Choice.** "I'm not sure" is an answer, not a failure. On filing status it is noted for the preparer and raises a filing-status escalation. On anything the return cannot do without, the agent says "I'm not sure either, so I'll flag it for a volunteer preparer rather than guess." An advice question is declined kindly, noted for the preparer word for word, escalated, and then the *same* question is asked again, so the conversation picks up where it was. Advice detection now also catches "Would it help to file separately?" and "Which is better…?". Naming a status ("married filing separately") is not mistaken for asking about one.

**Why.** People who are embarrassed not to understand taxes will guess rather than admit it, unless "I don't know" is plainly welcome. A guessed filing status is worse than a blank one.

**Tests.** `test_im_not_sure_is_an_answer_that_reaches_a_person`, `test_an_advice_question_is_noted_and_the_same_question_asked_again` (four phrasings), `test_naming_a_filing_status_is_not_mistaken_for_advice`.

## 6. Resumability

Built by Daniel (agent memory) and Ashwin: every answer is a checkpoint, so nothing is lost when a session is abandoned, and the taxpayer page shows the conversation so far when they come back. When a question is asked more than 30 minutes after the last answer, it opens with "Welcome back. Here's where we were…" and a plain-language summary. Guardrails B's part is that wording, which uses no field names, and the explanations already given, which carry over the gap so they aren't repeated.

**Open issue (flagged to Daniel).** The question waiting when a person leaves was written before they left, so someone returning on Friday first sees Tuesday's question without the "Welcome back" summary. The summary is only generated when that question is answered. Showing the summary on return (from the interrupt's `summary` field, in `run.py chat --resume` and on the taxpayer page) would close this gap.

## 7. Noticing a third party (brief §4.4, condition 6)

**Choice.** The trigger is someone *acting for* the taxpayer, not a relative being *mentioned*.
- These **stop the return** for a Kushim employee: "my daughter is typing this for me", "I'm answering for my mother", "this is her caregiver", "it's my mother's return".
- These **do not**: "My wife is Mary Whitfield", "my wife and I support ourselves", "my husband handled the taxes", "Yes, my son is helping me today".
- A helper who states an authority (a power of attorney, a guardian) is noted for the preparer to check the paperwork. The return isn't paused for them.

**Why.** Before this change, a married taxpayer answering "My wife is Mary Whitfield" at the spouse-name question was flagged as possible elder financial exploitation and the return paused. Every joint return would have stalled, and an honest couple would have been treated as suspects. Condition 6 is "not decoration", so it has to fire when it should, and it also has to stay quiet when it shouldn't, or staff learn to ignore it. The system notices and stops. Deciding whether abuse is happening is a person's job.

**Tests.** `test_a_married_couple_mentioning_each_other_is_not_an_exploitation_case`, `test_someone_taking_over_mid_conversation_stops_the_return` (three phrasings), `test_a_helper_who_states_authority_is_noted_for_the_preparer_not_stopped`, `test_who_is_answering_at_the_first_question` (five answers).

## Failure modes we know about

- **Detection is pattern-based.** A third party who never says they are one ("Yes, this is me") is not caught by the wording. The refund-account question is the second net: a refund going to an account the taxpayer can't describe as their own also stops the return. A determined abuser can still lie to both questions. The system lowers that risk, but it can't remove it.
- **Rewording can slip past advice detection.** Someone can phrase an advice question in a way the patterns miss. The Eligibility and Review agents never give advice either, so a missed question goes unanswered rather than wrongly answered, but it also isn't noted for the preparer.
- **The model parser can phrase things the tests don't see.** With an API key, answers are parsed by a model. The question text itself is always fixed, so the one-question rule holds either way. Only the parsing of answers changes.

## What to show in the demo video (the brief requires these working live)

Run `python run.py chat` and follow the case 1 answers in `demo/eval_case_1.md`, with these moments swapped in, in this order:

1. **Question 3 (SSN):** use the deliberately rejected synthetic value from the intake dignity test inputs (never a real person's SSN). It is refused, not kept, and asked again. Then use the allowed 900-series test value from `data/fixtures/case1_clean/intake.json`. Full numbers belong in fixture inputs, not in this document or traces.
2. **Question 5 (legally blind):** point out the one-line explanations of "legally blind" and "standard deduction". At question 7, "filing status" gets one too, and none is ever repeated.
3. **Question 7 (filing status):** type `Would it help to file separately?`. It is declined kindly and noted for the preparer, and the return pauses for a Kushim employee. Type `{"action":"continue"}` at the prompt, and the same question is asked again. Then type `Single`.
4. **Question 9 (someone else claim you):** answer `No. Here's my social, ...`, replacing the ellipsis with the same rejected synthetic test value from step 1. "No" counts, the number is not kept, and the person is told.
5. **Question 11 (other income):** type `Actually, can we go back to my date of birth?`. Only the date is re-asked. Type `March 14, 1954`, then `No` to the other-income question.
6. **Any later question:** type `quit`, then `python run.py chat --resume <id>` (the id is printed at the top). Every answer was kept, and the conversation carries on. On the web, use "Save and leave" and come back with the access code, and the conversation so far is still there.
7. **Upload step:** type `case1_clean`, then `Yes` at the summary, and the draft is produced.
8. **Optional, a new chat:** answer the first question with `I'm her daughter, I'm typing this for her` to show the exploitation stop.
