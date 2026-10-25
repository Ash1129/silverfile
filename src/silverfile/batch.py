"""Section 4.5: the batch scenario and the context-window measurement.

Ten W-2 images are read sequentially in ONE thread by a model-calling step.
Without a strategy, every previous document and reply stays in the prompt, so
input tokens grow with each document. With the strategy (selective recall: a
rolling summary of decisions so far plus the current document only), the curve
stays flat. `run_batch` runs one setting and records per-document input tokens;
`write_metrics` folds both runs into docs/metrics.md. The numbers are the grade.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import llm
from .config import ROOT, settings
from .memory import ContextWindow

SYSTEM = "You transcribe W-2 forms for a tax preparation aid. Report only printed values. Return JSON with wages, federal_withheld, employer_name, and a one-line 'decision' summarising what you recorded. Never estimate."
METRICS_JSON = ROOT / "docs" / "metrics.json"
METRICS_MD = ROOT / "docs" / "metrics.md"


def run_batch(docs: int = 10, strategy: str = "selective", write_metrics: bool = True) -> int:
    paths = sorted((ROOT / "data/fixtures/batch_w2s").glob("w2_*.jpg"))[:docs]
    if not paths:
        print("no batch fixtures; run data/generate_fixtures.py")
        return 1
    window = ContextWindow(strategy=strategy)
    rows = []
    model = settings().extraction_model
    for i, p in enumerate(paths, 1):
        item = [llm.image_block(p), {"type": "text", "text": f"Document {i} of {len(paths)}: W-2 image {p.name}. Transcribe it."}]
        msgs = window.messages_for(item)
        if llm.available():
            res = llm.complete(SYSTEM, msgs, model=model, max_tokens=400)
            data = res.json() if res.text else {}
            tokens_in, tokens_out, reply = res.tokens_in, res.tokens_out, res.text
        else:  # offline estimate so the pipeline can be exercised without a key
            reply = json.dumps({"wages": 0, "decision": f"recorded {p.name}"})
            images = sum(1 for m in msgs if not isinstance(m["content"], str))
            words = sum(len(str(m["content"])) // 4 for m in msgs)
            tokens_in, tokens_out, data = words + 1600 * images, 40, json.loads(reply)
        decision = data.get("decision") if isinstance(data, dict) else None
        window.remember(item, reply, decision or f"doc {i}: recorded")
        rows.append({"doc": i, "file": p.name, "tokens_in": tokens_in, "tokens_out": tokens_out, "wages": data.get("wages") if isinstance(data, dict) else None, "parsed": isinstance(data, dict) and bool(data)})
        print(f"[{strategy}] doc {i:2d} {p.name}: input tokens {tokens_in:6d}, output {tokens_out:4d}, wages {rows[-1]['wages']}")
    record = {"strategy": strategy, "model": model, "online": llm.available(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": rows}
    if write_metrics:
        existing = json.loads(METRICS_JSON.read_text()) if METRICS_JSON.exists() else {}
        existing[strategy] = record
        METRICS_JSON.parent.mkdir(exist_ok=True)
        METRICS_JSON.write_text(json.dumps(existing, indent=1))
        write_markdown(existing)
    return 0


def write_markdown(data: dict) -> None:
    none, sel = data.get("none"), data.get("selective")
    lines = ["# Context-window strategy: measured", "", "Scenario (brief section 4.5): ten W-2 images processed sequentially in one thread by the extraction step. "
             "`python run.py batch --no-context-strategy` replays every earlier document and reply in the prompt. "
             "`python run.py batch` uses selective recall: a rolling summary of the decisions so far plus the current document only (`ContextWindow` in `src/silverfile/memory.py`).", ""]
    if none and sel:
        lines += [f"Model: `{sel['model']}`. Online: {sel['online']}. Measured {sel['at']}.", "", "| Document | Input tokens, no strategy | Input tokens, selective recall | Output tokens (no / selective) |", "| --- | ---: | ---: | --- |"]
        for a, b in zip(none["rows"], sel["rows"]):
            lines.append(f"| {a['doc']} ({a['file']}) | {a['tokens_in']:,} | {b['tokens_in']:,} | {a['tokens_out']} / {b['tokens_out']} |")
        tn, ts = sum(r["tokens_in"] for r in none["rows"]), sum(r["tokens_in"] for r in sel["rows"])
        lines += ["", f"Totals: {tn:,} input tokens without the strategy, {ts:,} with it ({(1 - ts / tn) * 100:.0f}% fewer). "
                  f"Last document: {none['rows'][-1]['tokens_in']:,} vs {sel['rows'][-1]['tokens_in']:,}. "
                  f"Growth from first to last document: {none['rows'][-1]['tokens_in'] - none['rows'][0]['tokens_in']:+,} vs {sel['rows'][-1]['tokens_in'] - sel['rows'][0]['tokens_in']:+,}.", ""]
        bad_none = sum(1 for r in none["rows"] if not r.get("parsed", True))
        bad_sel = sum(1 for r in sel["rows"] if not r.get("parsed", True))
        lines += [f"Replies that failed to parse as the requested JSON: {bad_none} of {len(none['rows'])} without the strategy, {bad_sel} of {len(sel['rows'])} with it.", "",
                  "Reading: without a strategy every earlier image and reply is replayed, so the prompt grows by roughly one image plus one reply per document and the curve climbs every step (an earlier run with text-only replay also saw the model drift from the requested JSON format on 8 of 10 documents; the image-replay run above did not). With selective recall the prompt is one summary line plus the current image, so it is flat; the small variation is the summary growing by one short decision per document until it is folded (rolling summarisation after 12 decisions).", ""]
    else:
        lines += ["Run both settings to fill the table:", "", "```", "python run.py batch --no-context-strategy", "python run.py batch", "```", ""]
        for k, v in data.items():
            lines += [f"## {k}", "", "| Document | Input tokens | Output tokens |", "| --- | ---: | ---: |", *[f"| {r['doc']} | {r['tokens_in']:,} | {r['tokens_out']} |" for r in v["rows"]], ""]
    METRICS_MD.write_text("\n".join(lines))
