# SilverFile threat model

SilverFile prepares draft Form 1040-SR returns for older taxpayers. The proof of concept runs on synthetic data: every SSN is in the 900 test series and every document says "synthetic test document". The controls below are built as they would need to be with real taxpayers, because the system will hold exactly the data identity thieves want: names, dates of birth, SSNs, income and a refund destination. Elderly filers are also a group targeted by financial exploitation.

Each threat is mapped to the code that answers it and to a test that would fail if that answer stopped working. All tests are in `tests/security/` and run offline with `pytest -q tests/security`. Section 8 of `docs/report.md` covers the residual risks.

## 1. Assets

| Asset | Why it matters |
| --- | --- |
| Taxpayer PII: name, date of birth, test SSN, dependents, refund account description | Identity theft, refund fraud |
| Uploaded documents (W-2, SSA-1099, 1099s) | Hold all of the above, and sometimes photo GPS metadata |
| The draft's numbers and their provenance | A changed number is a wrong return; an unsourced number is the failure the brief forbids |
| Escalation decisions (who decided, what, when) | The human-in-the-loop record is how Kushim answers "why did this happen?" |
| Staff credentials and sessions, taxpayer access codes | Each one opens returns |
| `ANTHROPIC_API_KEY`, `SILVERFILE_DATA_KEY` | Billing, and the keys to every encrypted store |
| Model budget | Prompt or upload abuse can be used to run up cost |

## 2. Where data is stored

Everything sits in one data directory (`SILVERFILE_DATA_DIR`). The directory is created with mode 0700 and each database file with mode 0600, and nothing in it is served over HTTP.

| Store | Holds | Protection | Retention |
| --- | --- | --- | --- |
| `checkpoints.sqlite` | Every graph step's full state: facts, transcript, documents read, lines, provenance, escalations | Every blob is encrypted with Fernet (AES-128-CBC with HMAC-SHA256). Unencrypted or altered blobs are refused (`StrictEncryptedSerializer`). Loaded state is checked against the schema (`validate.check_state`). | Abandoned returns are deleted after `SILVERFILE_RETENTION_DAYS` (30). Finished drafts are deleted by an admin. |
| `uploads/<return>/<random>.enc` | Uploaded images and PDFs after cleaning | Fernet-encrypted under a random 128-bit name. The original name is kept only as a cleaned label. | Same as the return |
| `returns.sqlite` | The list index: status, flags, taxpayer name, one-line summary | Name and summary are encrypted. No SSN is ever written here. | Same as the return |
| `auth.sqlite` | Staff accounts, sessions, taxpayer access codes | scrypt password hashes. Sessions and codes are stored only as keyed hashes (HMAC-SHA256). | Sessions expire after 30 min idle / 12 h for staff and 60 min / 24 h for taxpayers. Codes are deleted with the return. |
| `security.sqlite` | Security event log, model spend per return | Append-only HMAC hash chain. Details are redacted (no SSNs, no keys). | Kept |
| Process memory | Visitor API keys entered in the header control or the optional gate (`gate.py`) | Never written to disk or logs; 12 h sliding expiry | Session |
| `.env` (local) / host secret store | API key, data key, first admin password | Gitignored and dockerignored. A warning is printed if the file is readable by others. Production refuses to start without the data key. | n/a |

One key, `SILVERFILE_DATA_KEY`, is turned into two keys with HKDF: an encryption key and an integrity (HMAC) key. To rotate it, put the new key first in a comma-separated list. A development checkout without the variable generates a key into the data directory. That is convenient but protects nothing, because the key then sits next to the data, so `SILVERFILE_ENV=production` (set in the Dockerfile) turns that fallback off.

## 3. Actors and trust boundaries

```
 taxpayer (browser) ──sf_taxpayer──▶ /start /resume /t/*  ─┐
                                                           │  service.py: authorize actor, build the resume value,
 Kushim staff (browser) ──sf_staff──▶ /  /returns/* /audit ─┼─▶ check the interrupt id, diff for security events
                                                           │
 CLI / seed / retention (system principal) ────────────────┘
                                       │
                                       ▼
            LangGraph (orchestrator + six agents) ──▶ checkpoints (encrypted)
                 │ text only, no tools                │ tax_math via MCP stdio, per-agent tool set
                 ▼                                    ▼
          Anthropic API                       tax_math server (local subprocess)
```

The boundaries:

1. **Browser to server.** Only an authenticated session crosses, with a CSRF token on every form.
2. **Taxpayer input to graph.** The only crossing is the text of an answer, plus checked uploads, at a question Intake asked.
3. **Graph to model.** Untrusted text goes out and untrusted text comes back. Replies are parsed into a fixed schema.
4. **Agent to tool.** Each agent gets only its own allow-listed functions.
5. **Process to disk.** Everything is encrypted and authenticated.

## 4. Roles

| Permission | taxpayer | clerk | preparer | admin |
| --- | :-: | :-: | :-: | :-: |
| Answer *their own* return's current question, upload at the upload step | ✓ | | | |
| List returns (name and status) | | ✓ | ✓ | ✓ |
| Start a return, issue an access code | | ✓ | ✓ | ✓ |
| Open a return, read the draft, state.json, PDF | | | ✓ | ✓ |
| Resolve an escalation | | | ✓ | ✓ |
| Advance a stalled run | | | ✓ | ✓ |
| Read the security log, delete a return | | | | ✓ |

Staff never answer for a taxpayer: a preparer answering is a third party answering. The permission to resolve an escalation is given only to roles that may also read the draft. A test checks this for every role, so the person a case is escalated to can always see the filing they are deciding on. The permissions are in `security/auth.py`.

## 5. STRIDE

| # | Asset | Threat (STRIDE) | Mitigation | Test |
| --- | --- | --- | --- | --- |
| S1 | Returns | **Spoofing:** opening someone else's return by typing their SSN, name or return id | No login by SSN or name. A taxpayer session comes only from a ~118-bit access code shown once and stored as a keyed hash. It is bound to one return, and taxpayer routes take no return id. | `test_sec_dashboard::test_an_ssn_a_name_or_an_id_opens_nothing`, `::test_taxpayers_only_ever_see_their_own_return` |
| S2 | Access codes | **Spoofing:** guessing a code | ~118 bits of entropy; 10 guesses per 15 min per address; failures logged | `test_sec_dashboard::test_guessing_access_codes_is_rate_limited`, `::test_ids_codes_and_tokens_are_unguessable_and_not_stored_raw` |
| S3 | Staff accounts | **Spoofing:** password guessing, session theft or fixation | scrypt hashes; lockout after 5 failures; per-address limit; a new token on every login; HttpOnly, SameSite=Strict and Secure cookies; idle and absolute expiry; logout ends the session on the server | `::test_logins_lock_after_repeated_failures_and_logout_ends_the_session`, `::test_sessions_expire_when_idle` |
| S4 | Escalations | **Spoofing:** recording a different resolver | The resolver and role come from the session, never from a form field | `::test_the_resolver_is_the_logged_in_preparer_not_a_form_field` |
| T1 | Draft numbers | **Tampering:** a number changed after review | `finalize` seals every line and record with a signed SHA-256 chain. The dashboard and the draft verify it, and the first changed line is named. | `test_sec_integrity::test_changing_a_number_after_review_is_detected`, `::test_a_seal_forged_without_the_key_does_not_verify` |
| T2 | Checkpoints | **Tampering:** editing or replaying saved state | Authenticated encryption; unencrypted blobs refused; schema check on load (ids, statuses, lines, SSNs, the fault hook) | `::test_an_edited_checkpoint_is_refused`, `::test_an_unencrypted_checkpoint_is_refused`, `::test_state_that_does_not_match_the_schema_is_refused` |
| T3 | Answers and resolutions | **Tampering:** replaying or double-submitting a form | Each form carries the interrupt id it answers, and a stale id is refused | `test_sec_dashboard::test_taxpayers_only_ever_see_their_own_return`, `::test_the_resolver_is_the_logged_in_preparer_not_a_form_field` |
| T4 | Draft numbers | **Tampering via prompt injection:** a document or answer that makes a model emit a number or a flag | Model output is reduced to a per-form or per-slot schema; extra keys and private flags are dropped; amounts are typed and range-checked; every amount on the draft must have a valid provenance record (Review fails otherwise, and `finalize` refuses to complete) | `test_sec_injection::test_injected_w2_image_is_contained_by_the_schema`, `::test_injected_w2_cannot_reach_a_completed_draft`, `::test_a_fooled_parser_cannot_set_private_flags_or_silence_a_trigger` |
| T5 | Staff forms | **Tampering:** CSRF | Session-bound token on every form (double-submit before login); cross-site Origin refused | `test_sec_dashboard::test_forms_without_a_valid_csrf_token_or_from_another_site_are_refused` |
| T6 | Dashboard | **Tampering:** XSS through extracted text, names or provenance cards | Autoescape forced on for every template; CSP `script-src 'nonce-…'` with no inline script allowed | `::test_displayed_values_are_escaped_and_scripts_need_the_nonce` |
| R1 | HITL decisions | **Repudiation:** "I never approved that" | Security log: each event is HMAC-chained to the previous one; escalation raised and resolved events carry actor, decision, note and fields changed | `test_sec_hitl::test_escalations_are_security_events_in_an_intact_chain`, `test_sec_integrity::test_an_edited_or_deleted_security_event_breaks_the_chain` |
| I1 | SSNs | **Information disclosure:** a real SSN entered by mistake | Only the 900–999 series is accepted, at every entry point: conversation, dependents, staff correction, loaded state. A refused number is not stored, and the event is logged without it. | `test_sec_ssn` (all) |
| I2 | SSNs | **Information disclosure:** an SSN volunteered where none was asked | It is removed before the checkpoint is written, the taxpayer is told, and the event is recorded without the number | `test_sec_pii::test_an_ssn_volunteered_at_another_question_is_removed_and_not_stored` |
| I3 | PII in logs and traces | **Information disclosure** | A log-record factory redacts SSNs and API keys from every record; the security log is redacted; the dashboard masks SSNs to the last four digits | `test_sec_traces` (all), `test_sec_pii::test_every_log_record_is_redacted` |
| I4 | PII sent to the model | **Information disclosure:** sending more than the model needs | The SSN answer is parsed locally; the facts sent to the intake model leave out SSNs, names, dependents and the refund account | `test_sec_ssn::test_the_ssn_answer_is_never_sent_to_a_model` |
| I5 | Data at rest | **Information disclosure:** stolen disk or backup | Checkpoints, uploads and the index's name and summary are encrypted; files are mode 0600; abandoned returns are deleted | `test_sec_traces::test_stores_on_disk_hold_no_pii_in_the_clear`, `test_sec_budget::test_abandoned_returns_are_deleted_with_everything_they_hold` |
| I6 | Drafts | **Information disclosure:** the wrong staff member reads a draft | Role permissions; a clerk sees names and statuses only | `test_sec_dashboard::test_roles_decide_what_staff_see` |
| I7 | Photos | **Information disclosure:** GPS and device data in EXIF | Images are re-encoded from pixels, so no metadata survives | `test_sec_uploads::test_exif_and_gps_are_stripped` |
| I8 | Keys | **Information disclosure:** a key committed to git or baked into the image | `.gitignore` and `.dockerignore` cover them; a test scans tracked files for key patterns; the Dockerfile sets no secret | `test_sec_structure::test_no_secret_is_tracked_in_git`, `::test_ignore_files_cover_secrets_and_runtime_data`, `::test_the_container_runs_unprivileged_with_no_baked_in_secret` |
| D1 | Server | **Denial of service:** huge or crafted uploads | Size cap per file and per request (counted as the body streams); a pixel cap against decompression bombs; a page cap on PDFs | `test_sec_uploads::test_size_limit`, `::test_decompression_bomb_is_refused` |
| D2 | Model budget | **Denial of service or cost:** a looping or abusive session running up tokens | Per-return model calls per minute, a global rate and a persisted token cap; the agent falls back or hands the item to a person; the event is logged | `test_sec_budget` (all) |
| E1 | Agents | **Elevation:** the taxpayer talking to the orchestrator, or to anything but Intake | The service builds the resume value itself, as text plus server-built documents, and only when a question is pending. No taxpayer route can resolve, advance or patch state. | `test_sec_hitl::test_a_third_party_cannot_talk_their_way_out_of_the_escalation`, `test_sec_uploads::test_documents_must_come_from_an_upload_or_a_fixture` |
| E2 | Escalation triggers | **Elevation:** silencing an escalation through user input | Escalation-trigger slots cannot be changed by a model's correction field; "third party" is sticky; only a staff resolution can resolve an escalation (any other path is an invariant violation that stops the request); the flag is checkpointed | `test_sec_hitl` (all), `test_sec_injection::test_a_fooled_parser_cannot_set_private_flags_or_silence_a_trigger` |
| E3 | Uploads | **Elevation:** an executable, a script or an active PDF | Allowlist by magic bytes, and the extension must agree; PDFs with JavaScript, actions, forms or attachments are refused; uploads are never served or executed | `test_sec_uploads::test_wrong_types_and_lying_names_are_refused`, `::test_active_pdfs_are_refused`, `::test_a_polyglot_trailer_does_not_survive`, `::test_stored_under_a_random_name_encrypted_and_never_served` |
| E4 | Tools | **Elevation:** an agent calling a tool outside its job | Per-agent allowlists; the MCP server started for Computation registers only Computation's tools; models get no tools at all | `test_sec_structure::test_the_tool_allowance_matches_what_each_agent_calls`, `::test_an_agent_cannot_reach_a_tool_outside_its_allowance`, `::test_the_model_door_takes_no_tools` |
| E5 | Filing | **Elevation:** anything transmitting a return | There is no filing path: no network module except the Anthropic client and the local MCP pipe, and no filing status, function or route | `test_sec_structure::test_no_module_opens_a_network_connection_of_its_own`, `::test_nothing_in_the_code_is_about_filing` |
| E6 | Container | **Elevation:** a compromise of the web process | The process runs as non-root uid 10001 on a read-only root filesystem, with all capabilities dropped, no-new-privileges, and the port bound to loopback in compose | `test_sec_structure::test_the_container_runs_unprivileged_with_no_baked_in_secret` |

## 6. Prompt injection, specifically

There are two sources: the taxpayer's own answers, and text printed on, or hidden in, an uploaded document. The tests assume the model is fooled. They replace it with one that obeys the injected text, then check that the damage stops at the schema:

- **The models have no tools.** A model can only return text. Code parses that text into fixed fields; anything else is dropped, and private control flags (`_confirmed`, `_uploaded`) cannot be set.
- **Triggers are code.** The escalation triggers, the review rules and the provenance guard are deterministic and run after the model. The model reviewer can add findings but cannot remove a rule finding.
- **Numbers come from tools.** Every amount on the draft comes from a `tax_math` call with a provenance record that points to a document field or a line. An injected document can still lie about its own boxes; that is the residual risk in section 8 of the report.
- **Tripwires.** Instruction-like text, a full SSN in a text field, or an implausible amount becomes a security flag on the document or answer. Review sends a flagged document to a person, and the event goes to the security log.

## 7. Out of scope for this proof of concept

- Multi-factor authentication for staff.
- A shared rate-limit store for more than one server instance.
- Malware scanning beyond type checking and re-encoding.
- TLS termination: the host or a proxy provides it, and cookies are marked Secure behind HTTPS.
- Egress firewalling of the container.
- Hardware-backed key storage.
- Formal penetration testing.

Each of these is listed in the report as work to do before real taxpayer data.
