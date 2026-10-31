# SilverFile

A multi-agent preparation aid that turns a short conversation and a handful of uploaded tax documents into a **draft Form 1040-SR (tax year 2025) with a provenance record for every number**. Built for the Kushim proof of concept (Cornell ENMGT 5405). It prepares drafts for a human preparer; it never files, never advises, and stops for a person whenever the brief says it must.

> Software status: a proof of concept on synthetic data. The test SSNs are all in the 900 series and the W-2 images say "synthetic test document" on their face.

## Quick start (local demo)

Follow these steps in order. The examples put the repository in your home folder; any writable folder works. All commands after step 2 run inside **`silverfile/`, the folder containing `run.py`, `README.md` and `.env.example`**.

### 1. Install Git and uv

Install [Git](https://git-scm.com/downloads) using the instructions for your operating system. Then open **PowerShell** on Windows or **Terminal** on macOS/Linux and install [uv](https://docs.astral.sh/uv/getting-started/installation/), which manages Python and dependencies:

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS/Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen the terminal, then check that both `git --version` and `uv --version` work. You do not need to install Python separately.

### 2. Download the repository and enter it

Run in either terminal:

```text
cd ~
git clone https://github.com/Ash1129/silverfile.git
cd silverfile
```

If you already have the repository, skip cloning and use `cd "FULL/PATH/TO/silverfile"` with its actual location. Keep quotes around paths containing spaces.

### 3. Create the Python environment and install dependencies

Run in either terminal:

```text
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt -e .
```

uv downloads Python 3.13 if needed. This gives everyone the same Python version regardless of the system installation. The commands below call this environment's Python directly, so no activation step is needed. Demo fixtures and W-2 images are already included in the repository.

### 4. Put your API key in `.env`

On the first run, copy the template and open it in a text editor. If `.env` already exists, edit that file instead of copying over it.

**Windows (PowerShell):**

```powershell
Copy-Item .env.example .env
notepad .env
```

**macOS/Linux:**

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

In nano, save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**. Use another text editor if nano is unavailable. Save the file as exactly `.env` beside `run.py` (not `.env.txt`). It is gitignored; keep your real key out of committed files.

Choose **one** provider below and replace the matching settings in `.env`. Use your own API key with available API credit; live calls are billed to that account. To try the app offline, leave `ANTHROPIC_API_KEY` blank and continue to step 5.

**Claude / Anthropic** — get a key from [Anthropic](https://platform.claude.com/settings/keys):

```dotenv
ANTHROPIC_API_KEY=YOUR_ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL=https://api.anthropic.com
SILVERFILE_INTAKE_MODEL=claude-haiku-5-5
SILVERFILE_EXTRACTION_MODEL=claude-sonnet-5-5
SILVERFILE_REVIEW_MODEL=claude-opus-5-5
```

**DeepSeek** — get a key from [DeepSeek](https://platform.deepseek.com/api_keys):

```dotenv
ANTHROPIC_API_KEY=YOUR_DEEPSEEK_API_KEY
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
SILVERFILE_INTAKE_MODEL=deepseek-flash
SILVERFILE_EXTRACTION_MODEL=deepseek-flash
SILVERFILE_REVIEW_MODEL=deepseek-v4-pro
```

DeepSeek uses the existing Anthropic SDK through its [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api/); no application code changes are needed. Keep the review model different from the extraction model. `deepseek-flash` supports image extraction; `deepseek-v4-pro` does not. For DeepSeek uploads, use JPEG/PNG: this endpoint does not support PDF document blocks.

### 5. Start SilverFile and log in

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe run.py try
```

**macOS/Linux:**

```bash
./.venv/bin/python run.py try
```

Keep the terminal open. The command starts the local server and opens [http://localhost:8000](http://localhost:8000); open that address yourself if the browser does not appear. Log in as **`pat`**, password **`silverfile-demo-2025`**. Other demo users are `ada` (admin) and `cleo` (clerk). A fresh data directory is seeded with six demo returns. Seeding follows the configured mode and can make billed model calls when an API key is set; the browser may open before seeding finishes, so refresh after the terminal reports `Application startup complete`.

Stop with **Ctrl+C** in the terminal. To restart later, enter the same repository folder and repeat the start command. Restart after changing `.env`.

### 6. Confirm that the API is actually being called

The terminal should show `Mode: live models` when a key is set. This confirms that the key was loaded; it does **not** test authentication or model access. Opening an existing demo return makes no model calls.

1. As `pat`, click **Start return** with **Start from a fixture** set to **none** and **Skip the conversation** unchecked. Copy the access code.
2. Open [http://localhost:8000/start](http://localhost:8000/start) in a private/incognito window and enter the code. Answer as the taxpayer using synthetic data (only 900-series test SSNs are accepted). When prompted for documents, upload **`demo/w2_image_1.jpg`** from your checkout and finish the upload step.
3. Back in the staff dashboard, open this new return. **Token log** should show the configured models and nonzero token usage. In **Draft form**, the W-2 extraction should say **`vision:<your extraction model>`** and Box 1 wages should be **18,400**. You can also inspect `extracted` and `token_log` through the **state.json** link. Review calls happen only after the rule checks pass.

### Troubleshooting

| Symptom | What to check |
| --- | --- |
| `git` or `uv` is not recognized | Finish installation and reopen the terminal before step 2. |
| `run.py` cannot be found | Enter the repository folder containing `run.py`; the examples use `~/silverfile`. |
| `Mode: offline`, or the wrong key/provider/model is used | Check `.env` spelling and values, then restart. Existing shell variables override `.env`, even when empty; remove stale `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` or `SILVERFILE_*_MODEL` settings from that terminal. |
| Live mode, but no model activity | Test a **new** conversation and image upload. Seeded returns are offline; JSON fixtures bypass image extraction. Check **Token log** for successful usage. |
| API failure or unexpected output | Inspect **Agent trace**, **Draft form** review findings and **state.json**. Intake logs `model parse failed` and uses its scripted parser; review records `claude:unavailable` and uses rules only. Image API errors can surface as request errors; they do not all fall back to sidecar JSON. Check the key, API balance, model access and document type. |
| Port 8000 is already in use | Add `--port 8001` to the start command and open `http://localhost:8001` (use the same port for `/start`). |

### Configuration and other commands

All model calls go through `src/silverfile/llm.py` using the Anthropic SDK. `config.py` loads `.env` from the repository root at startup; values exported in the shell also work. The SDK reads `ANTHROPIC_BASE_URL`, and the three `SILVERFILE_*_MODEL` variables select the agents' models. Some labels still say "Claude" or "key in .env" even when another provider or shell environment is used.

For **Anthropic keys only**, `python run.py setup` offers hidden key input and saves `.env`; `python run.py try --ask-key` accepts a key in the browser and keeps it in memory for that browser session (12 hours or until **Forget my key**). Both validate Anthropic key format. **For DeepSeek, use the manual `.env` configuration above.**

Local runtime data defaults to `data/runtime/`, which is gitignored. A development encryption key is generated there when `SILVERFILE_DATA_KEY` is blank. `setup` can generate an explicit data key in `.env`; it is required for Docker. Preserve that key with any saved data you want to keep.

In the table below, `python` means `.\.venv\Scripts\python.exe` on Windows or `./.venv/bin/python` on macOS/Linux. For tests, first install the extra dependencies with `uv pip install --python .venv -r requirements-dev.txt`; run pytest as `python -m pytest -q` using that same interpreter.

Other entry points:

| Command | What it does |
| --- | --- |
| `python run.py demo case1_clean` | Clean run in the terminal with a prefilled intake; writes `outputs/<id>.html` |
| `python run.py demo case2_hitl` | HITL run; pauses for the filing-status decision, resolve it on the command line or with `--resolve '{...}'` |
| `python run.py chat` | Hold the intake conversation yourself, one question at a time; `--resume <id>` continues a session |
| `python run.py batch [--no-context-strategy]` | The ten-document context-window measurement (writes `docs/metrics.md`) |
| `python run.py graph` | Prints the Mermaid diagram of the compiled graph and writes `docs/graph.mmd` |
| `pytest -q` | The test suite (offline; tax math runs in-process); `pytest -q tests/security` for the security tests alone |
| `python run.py staff add NAME --role clerk\|preparer\|admin` | Create a staff account (`staff list`, `staff passwd NAME` too) |
| `python run.py setup` | Put your Anthropic key (hidden input, checked) and a generated data key into `.env`, readable by you only |
| `python run.py try [--ask-key]` | One step to click through it locally: demo staff accounts (ada admin, pat preparer, cleo clerk; password `silverfile-demo-2025`), demo returns, server and browser. `--ask-key` asks for the Anthropic key in the browser and never saves it |
| `python run.py purge [--days N]` | Delete abandoned returns past retention (also runs at start-up) |
| `python run.py audit` | Verify the security log's hash chain and print recent events |
| `python evals/run_evals.py` | The eval cases with expected line values and expected escalations |
| `docker compose up --build` | The same dashboard on port 8000 with sessions in a named volume. Run `python run.py setup` first: the container needs `SILVERFILE_DATA_KEY` (and `SILVERFILE_ADMIN_USER` / `SILVERFILE_ADMIN_PASSWORD` in `.env` for the first login) |

**Offline mode.** Without an API key the system still runs end to end with the included fixtures: intake answers are parsed by a scripted parser, fixture images are read from their sidecar JSON, and review uses the rule checks and the independent recomputation only. A new image without a sidecar needs a live vision model. Every number still comes from the MCP tool server. Online, the configured intake model parses answers, the extraction model reads documents, and the review model audits the draft. Keep review and extraction models distinct: if they match, the code substitutes a Claude model name, which may not suit a third-party endpoint.

Docker could not be run on the development machine (no Docker daemon). The Dockerfile and compose file are included and were written against the same requirements file the local virtual environment was built from.

### Sharing a demo copy

`render.yaml` deploys the Dockerfile to Render's free tier with no API key of its own. Your `ANTHROPIC_API_KEY` stays in the local `.env`, which is gitignored and never copied into the image. With no server key, every page has an **API key** control in the header: a visitor can paste their own Anthropic key to run the live models for their browser session only. The key is checked with one `models.list` call, kept in the server's memory under a random session id (the cookie carries only the id, HttpOnly, 12-hour sliding expiry, *Forget my key* in the header drops it), used through the `llm.request_key` context variable for that visitor's requests only, never written to disk or logs and never shown on a page; each visitor's runs are billed to their account. Until a key is entered the app runs in **offline mode** (scripted intake, the demo documents and rule-based review; the numbers are identical), so a clean run works with no key, but a new document image can only be read by a live model. Set `SILVERFILE_REQUIRE_KEY=1` to make a key mandatory before any page is shown (the earlier full-page gate). Vercel is not a fit: its Python runtime is serverless with no writable disk, and the dashboard keeps sessions and uploads in SQLite and on disk.

The shared copy also needs a staff login: `render.yaml` has Render generate `SILVERFILE_DATA_KEY` and the first admin's password (user `admin`), which the owner reads in Render's dashboard and uses to add accounts for teammates.

A fresh data directory would show an empty list, so with `SILVERFILE_SEED_DEMO=1` (set in the Dockerfile and `render.yaml`) the dashboard seeds six demo returns on its first start: two complete, one halted, two waiting on a person and one mid-conversation. They are real offline runs of the fixtures through the same service layer, with transcripts, traces and draft forms. `python run.py seed` does the same by hand (`--force` adds them even when returns exist), and the seed is a no-op once the index has rows.

## Topology

![graph](docs/graph.png)

A **hub-and-spoke graph with one conditional router**. The Orchestrator is the only node that decides where the return goes next; every worker returns to it. Three things in the picture are not simple spokes:

- **Document extraction fans out** (`Send` per document) into `extract_document` and joins in `aggregate_extraction`, whose state key uses a custom reducer (`merge_documents`) so parallel branches never overwrite one another, and so a re-extraction of one document replaces only that document's record. A test checks that the final state is identical whatever order the branches finish in.
- **Review → revise is a bounded cycle.** Review can send the return back to Computation, Extraction, Eligibility or Intake with findings, at most `MAX_REVIEW_ROUNDS = 3` times. Review cannot change a number; it says what is wrong and who should look. After the third failed round the return goes to a person.
- **Escalation is an interrupt**, not a node that waits. `escalation` calls LangGraph `interrupt()` with the open item; the thread is checkpointed and the dashboard resumes it with `Command(resume=...)` when a Kushim employee decides.

Why a hub and not a pipeline: the brief's control flow is full of "if this, go back there" (a document conflicts with an answer → Intake; a review finding about a transcription → Extraction; a resolved escalation → re-check Eligibility). A linear pipeline would need every one of those paths drawn as its own edge. A hub keeps one decision table (`decide_next` in `agents/orchestrator.py`), which the tests cover row by row, and it makes the loop guard trivial to place: the Orchestrator sees every handoff.

### Roles

| Agent | Does | Does not | Model |
| --- | --- | --- | --- |
| Orchestrator | Reads the state, picks the next agent, counts handoffs, trips the loop guard, records the handoff log | Touch any number or talk to anyone | none (deterministic) |
| Intake | Asks one plain-language question at a time, parses answers into slots, notes "I'm not sure", collects uploads, raises conversation-level escalations | Interpret tax rules, do arithmetic, ask for PII that is not needed | Claude Haiku (parser only) / scripted |
| Document Extraction | Reads each uploaded document into typed fields with a confidence, checks it against what the taxpayer said it was | Decide what a field means for the return | Claude Sonnet (vision) / sidecar JSON |
| Eligibility & Rules | 1040-SR eligibility, filing status validity, line 12d boxes, Standard Deduction Chart applicability, scope check, document/answer conflicts, 1099-R box 2b | Compute a line | none; calls `tax_math` |
| Computation | Builds every line from the documents and the eligibility record, in dependency order | Arithmetic of any kind: every value is a `tax_math` tool result, and a test asserts the module contains no arithmetic | none; calls `tax_math` over MCP stdio |
| Review / Audit | Rule checks, an independent recomputation from the leaf lines, then an adversarial model read; emits findings with a target agent | Fix anything | Claude Opus / rules only |
| `tax_math` MCP server | 12 pure functions (standard deduction, senior deduction, Social Security worksheet, 2025 rate schedule, sums, settle) | Hold state | none |

### The loop guard

`detect_loop` in `agents/orchestrator.py` has two detectors: an absolute ceiling (`MAX_HANDOFFS = 40` handoffs among the working agents) and a repeated short pattern (a 2- or 3-agent cycle repeated `LOOP_PATTERN_REPEATS = 3` times without reaching review). When it fires the Orchestrator writes a `loop_guard` escalation, the return pauses for a person, and whatever the person decides, the handoff log is marked `reset` so the guard judges only what happens afterwards. `tests/test_loop_guard.py` turns on a fault-injection flag that makes Eligibility and Computation hand the return back and forth, and asserts the guard trips, the reason names the pattern, and the return reaches a person rather than spinning.

The routing table row by row (with the test that covers each row), the entry points, the review bound, the human-review path and the known gaps are in [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md).

## Inter-agent communication

Agents talk through a typed **message bus** (`mailbox` in graph state, `src/silverfile/comms/`) rather than loose state fields. Every handoff is a `request` from the Orchestrator, and every result is an `inform` reply on the same thread. Review's findings become `revise` **work orders** addressed to the agent that owns each fix, and that agent replies `done`. Escalations appear as `escalate`, and a person's decision as `resolve`. Three things hold this together:

- **Channel policy** (`protocol.CHANNELS`): who may send which speech act to whom. Only the Orchestrator hands off, only Intake reaches the taxpayer, only Review issues work orders, and only a person resolves. Anything else is refused at construction.
- **Write contracts** (`contracts.WRITES`): the state keys each agent may write, enforced on every node by the hub. Review physically cannot write `lines`. An illegal write is dropped and logged as a `violation`.
- **An order-independent reducer** (`merge_messages`): content-addressed ids and a logical clock, so the parallel extraction branches post at once and the log is identical whichever branch finishes first. Redelivery is a no-op.

Open work orders also steer routing. If Review finds problems for both Extraction and Computation, each runs before review comes round again. The **Agent messages** card on each return in the dashboard shows the log (`/returns/<id>/messages.json` for the raw envelopes). Full design, sequence diagram and tests: [docs/INTER_AGENT_COMMUNICATION.md](docs/INTER_AGENT_COMMUNICATION.md).

## Escalation conditions

Every one of these produces an `Escalation` record with a reason and a rulebook citation, pauses the thread, and shows up in the dashboard queue:

| Kind | Raised when | Raised by |
| --- | --- | --- |
| `out_of_scope` | A declared or uploaded document type, or an income source, is outside W-2, SSA-1099, 1099-R, 1099-INT, 1099-DIV | Eligibility |
| `eligibility` | Nobody on the return was born before 2 January 1961 | Eligibility |
| `chart_not_applicable` | The Standard Deduction Chart does not apply (dependent of another, MFS with itemising spouse, dual-status) | Eligibility |
| `uncertain_fact` | A required fact is unconfirmed, including dual-status residency or an MFS spouse's itemised deductions | Intake, Eligibility, Computation |
| `conflict` | A document disagrees with what the taxpayer said (a form not declared, a declared form missing, a name mismatch), or the taxpayer cannot answer something the return needs | Eligibility, Intake |
| `r_box2b` | A 1099-R has box 2b "taxable amount not determined" checked | Eligibility |
| `advice` | The taxpayer asks what they *should* do | Intake |
| `filing_status` | A spouse died during the year, or the taxpayer is unsure of their status | Intake, Eligibility |
| `exploitation` | A third party answers without stated authority, or the refund account is not described as the taxpayer's own | Intake, Eligibility |
| `review_unresolved` | Review failed `MAX_REVIEW_ROUNDS` times, or a finding needs a person | Orchestrator |
| `loop_guard` | See above | Orchestrator |

A staff action is submitted through the dashboard; the resolver and role come from the logged-in session and fact patches are schema-checked. Every `continue` runs a deterministic `resolution_check` that records `yes` or `no` with structured required actions. A failed check retains the open escalation. `start_edit` and `save_edit` keep a checkpointed human edit in progress; `complete_edit` (or Continue with a patch) completes it, invalidates old numbers and provenance, and queues eligibility, one computation and review when the required intake and documents are available. `halt` hands the return to a preparer.

Source-linked monetary claims are reconciled with extracted boxes before calculation. A preparer can inspect the original source and submit an evidence-backed, version-checked transcription correction in the dashboard. Extraction applies the work order, preserving the original upload and audit history, before eligibility, recomputation and review. Identical-source duplicates require explicit disposition; unrelated documents are not merged. Details, integration boundaries and acceptance steps are in [docs/EVIDENCE_REVIEW.md](docs/EVIDENCE_REVIEW.md).

Deduction-chart facts require explicit confirmation: `dual_status_alien` for every return and `mfs_spouse_itemizes` only for married filing separately. Missing or null answers block calculation; they are never defaulted to false. Active intake questions for these two facts are pending integration. Until then, a new conversation pauses for staff confirmation after intake, while the prefilled demo fixtures contain explicit synthetic answers. The field contract and staff resolution example are in `docs/INTAKE_DIGNITY.md`.

An unresolved review creates a separate **blocking review event**. Accept stays blocked until the listed corrections and recheck pass, a staff member explicitly selects **Resolve blocking review event**, and a nonempty intervention explanation is saved to the structured log with **Save intervention explanation**. The explanation has no application character limit; long text is saved in ordered, bounded chunks while the existing HTTP body cap and staff rate limit remain in force. Changing the explanation requires saving it again. Only then is **Accept** enabled. Starting another edit reopens the event and invalidates the saved explanation. The dashboard exposes the workflow and `/returns/<id>/hitl.json`; the same records are mirrored to the tamper-evident security log. Unknown required amounts and checkbox values remain unknown instead of becoming zero or false, and are sent to a person when necessary.

## Accessibility and intake design

The Intake agent is the only one that speaks to the taxpayer, and the choices there are the product:

- **One question per turn**, in plain words, with a short recap of what has been covered on every resume. The question text is the interrupt payload, so the chat page and the terminal show exactly the same thing.
- **Drift is normal.** Answers are parsed into *any* slot, not only the one asked, so a correction three questions later still lands ("actually my wife passed in May"). Stories are acknowledged, not cut off.
- **No arithmetic, no interpretation.** Intake asks which filing status the taxpayer *expects* to use. It never derives one. "I'm not sure" is recorded for the preparer, and escalated only where the answer changes the return (filing status after a death).
- **Minimal PII.** Only the SSNs the form needs (taxpayer, spouse on a joint return, dependents), accepted only in the 900 test series in this proof of concept. Extra identifiers the taxpayer volunteers are noted as events, not stored as facts.
- **Resumable by design.** Every answer is a LangGraph checkpoint in SQLite. A session can be abandoned at any question and resumed days later with the recap, with the taxpayer's access code (never by SSN or return id).
- **Honest about uncertainty.** When an answer cannot be parsed the agent says so and asks again differently; it never guesses a value and never fills a slot from a model's assumption.

## Security

`docs/THREAT_MODEL.md` has the asset list, where each kind of data is stored and how, the roles, and a STRIDE table that maps every threat to its mitigation and to the test in `tests/security/` that would fail without it. In short: staff log in with roles (clerk, preparer, admin); taxpayers get a one-return access code, never a login by SSN; only 900-series SSNs are accepted anywhere and volunteered ones are removed before anything is saved; checkpoints, uploads and the index are encrypted at rest with an authenticated cipher; uploads are allow-listed by magic bytes, re-encoded (EXIF gone) and stored under random names; every form has a CSRF token and names the interrupt it answers; model output is reduced to a schema and models have no tools; each agent gets only its own `tax_math` functions; a completed draft is sealed with a signed hash chain over its provenance; escalations can only be resolved by a role that may see the filing, and every one is logged in an HMAC-chained security log; model calls are rate-limited and capped per return; and there is no filing path in the code at all. Section 8 of `docs/report.md` is honest about what this does not stop.

## Layout

```
src/silverfile/
  graph.py            StateGraph, checkpointer, finalize
  state.py            ReturnState, reducers, limits
  comms/              inter-agent message protocol, channel policy, write contracts, bus wrapper
  agents/             orchestrator, intake, extraction, eligibility, computation, review, escalation
  tax_math/           rules.py (pure functions), server.py (MCP), client.py (stdio client + call log)
  provenance.py       the per-line record
  memory.py           session summary, resume recap, answer history; ContextWindow (section 4.5)
  batch.py            the ten-document measurement
  service.py          session store and the dashboard's operations (authorization, security diff)
  security/           pii, crypto, uploads, validate, injection, integrity, audit, auth, ratelimit, retention, capabilities
  dashboard/          FastAPI app (staff and taxpayer doors), web.py (headers, CSRF, cookies), templates
  forms/render.py     interactive HTML review and official 1040-SR draft PDF
  forms/official.py   verified 2025 IRS field map and linked PDF provenance
data/fixtures/        synthetic cases and the batch W-2 images (generated)
demo/                 the two walkthroughs and the synthetic W-2 images
docs/                 graph.png, metrics.md, AGENT_MEMORY.md, THREAT_MODEL.md, IMPLEMENTATION_NOTES.md, AI_DISCLOSURE.md, blueprint/ (the design-stage run)
evals/                cases.json + run_evals.py
tests/                pytest suite; tests/security/ the security tests
traces/               sample runs (events, provenance, token log)
```

## Team and roles

Seven people, one focus area each. Each role has a design note or section in `docs/`, and the commit history shows who built what.

| Person | Role | Where to look |
| --- | --- | --- |
| Ashwin (Ash) | MLOps / DevOps: the first working graph, deployment, evals and metrics | `src/silverfile/`, `Dockerfile`, `render.yaml`, `docs/metrics.md` |
| Sasha | Security | `docs/THREAT_MODEL.md`, `src/silverfile/security/`, `src/silverfile/keysetup.py`, `tests/security/` |
| Ishaan | Inter-agent communication | `src/silverfile/comms/`, `docs/INTER_AGENT_COMMUNICATION.md`, `tests/test_comms.py` |
| Daniel | Agent memory: resume recap, answer history, context window | `docs/AGENT_MEMORY.md`, `src/silverfile/memory.py`, `tests/test_memory.py`, the `--no-context-strategy` measurement |
| Tony (Guardrails A) | Guardrails and ethics: escalations and human review | `src/silverfile/agents/escalation.py`, `tests/test_escalations.py`, `tests/security/test_sec_hitl.py` |
| Gabriel (Guardrails B) | Guardrails and ethics: intake dignity | `docs/INTAKE_DIGNITY.md`, `tests/test_intake_dignity.py`, `demo/eval_case_1.md` |
| Mahika | Task division and management: the Orchestrator, routing, parallel branch, review bound, loop guard, graph diagram | `docs/ORCHESTRATION.md`, `docs/graph.mmd`, `tests/test_routing_table.py`, `tests/test_orchestration_flow.py` |
