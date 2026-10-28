#!/usr/bin/env python
"""SilverFile command line.

  python run.py serve [--port 8000]        the Kushim dashboard (and the taxpayer chat)
  python run.py demo case1_clean           run a fixture case end to end in the terminal (prefilled intake)
  python run.py chat [--case case1_clean]  hold the intake conversation in the terminal, answers typed by you
  python run.py batch [--no-context-strategy] [--docs 10]   the section-4.5 measurement; writes docs/metrics.md
  python run.py graph                      print the Mermaid diagram of the graph (and write docs/graph.mmd)
  python run.py seed [--force]             add the demo returns to the dashboard's data directory (what a shared copy starts with)
  python run.py staff add NAME --role R    create a staff account (roles: clerk, preparer, admin); the password is prompted for
  python run.py staff list | passwd NAME   list accounts, or set a new password (ends that person's sessions)
  python run.py purge [--days N]           delete abandoned returns (unfinished, idle N days; default SILVERFILE_RETENTION_DAYS)
  python run.py audit                      verify the security log's hash chain and print the latest events
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def _docs_for_case(case: str) -> list[dict]:
    from silverfile.provenance import now_iso

    d = ROOT / "data/fixtures" / case
    docs = []
    for p in sorted(d.glob("*.json")):
        if p.stem in ("intake", "answers"):
            continue
        kind = json.loads(p.read_text()).get("kind", "unknown")
        docs.append({"id": p.stem, "kind": kind, "path": str(p), "declared_by": "fixture", "uploaded_at": now_iso()})
    if case == "case1_clean":
        docs.insert(0, {"id": "w2-image-1", "kind": "W-2", "path": str(ROOT / "demo/w2_image_1.jpg"), "declared_by": "fixture", "uploaded_at": now_iso()})
    return docs


def _pending(g, cfg):
    return [i.value for t in g.get_state(cfg).tasks for i in t.interrupts]


def cmd_demo(args: argparse.Namespace) -> int:
    from langgraph.types import Command

    from silverfile.forms.render import render_html
    from silverfile.graph import compile_graph, get_checkpointer, new_return_state

    case = args.case
    intake = json.loads((ROOT / "data/fixtures" / case / "intake.json").read_text())
    g = compile_graph(get_checkpointer(":memory:"))
    rid = f"demo-{uuid.uuid4().hex[:6]}"
    cfg = {"configurable": {"thread_id": rid}}
    out = g.invoke(new_return_state(rid, _docs_for_case(case), intake=intake), cfg)
    while True:
        pend = _pending(g, cfg)
        if not pend:
            break
        iv = pend[0]
        if iv["type"] != "escalation":
            print("unexpected question in a prefilled demo:", iv["text"])
            return 1
        e = iv["escalation"]
        print(f"\nPAUSED for a Kushim employee: [{e['kind']}] {e['reason']}")
        if args.resolve:
            res = json.loads(args.resolve)
        else:
            print('Resolve with a JSON resolution, e.g. {"action":"continue","facts_patch":{"filing_status":"mfj"}} or {"action":"halt"}')
            res = json.loads(input("resolution> ") or '{"action":"halt"}')
        res["resolved_by"] = "cli"  # the person at this terminal; the dashboard records the logged-in preparer
        out = g.invoke(Command(resume=res), cfg)
    print("\nEVENTS")
    for ev in out.get("events", []):
        print("  -", ev)
    print("\nSTATUS:", out.get("status"))
    if out.get("lines"):
        for k in ("1z", "2b", "3b", "4b", "5b", "6a", "6b", "9", "11a", "12d", "12e", "13b", "14", "15", "16", "24", "25d", "26", "33", "34", "37"):
            p = out["provenance"].get(k, {})
            print(f"  line {k:>4}: {out['lines'].get(k, 0):>12,.2f}   <- {p.get('method', '?')} {p.get('sources', [])}")
        outdir = ROOT / "outputs"
        outdir.mkdir(exist_ok=True)
        (outdir / f"{rid}.html").write_text(render_html(out))
        print("\nform written to", outdir / f"{rid}.html")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from langgraph.types import Command

    from silverfile.graph import compile_graph, get_checkpointer, new_return_state

    g = compile_graph(get_checkpointer(":memory:" if args.ephemeral else None))
    rid = args.resume or f"chat-{uuid.uuid4().hex[:6]}"
    cfg = {"configurable": {"thread_id": rid}}
    if not args.resume:
        g.invoke(new_return_state(rid, []), cfg)
    print(f"(return {rid}; type 'quit' to stop and resume later with --resume {rid})\n")
    while True:
        pend = _pending(g, cfg)
        if not pend:
            break
        iv = pend[0]
        if iv["type"] == "escalation":
            e = iv["escalation"]
            print(f"\n[paused for a Kushim employee: {e['kind']}] {e['reason']}")
            res = json.loads(input('resolution JSON (e.g. {"action":"continue"})> ') or '{"action":"halt"}')
            res.setdefault("resolved_by", "cli")
            g.invoke(Command(resume=res), cfg)
            continue
        print("SilverFile:", iv["text"])
        if iv["type"] == "upload":
            ans = input("files (comma-separated paths, or a fixture case name, or 'done')> ").strip()
            if ans and (ROOT / "data/fixtures" / ans).exists():
                g.invoke(Command(resume={"text": "done", "documents": _docs_for_case(ans)}), cfg)
            else:
                from silverfile.provenance import now_iso

                docs = [{"id": Path(p.strip()).stem, "kind": "unknown", "path": str(Path(p.strip()).resolve()), "declared_by": "taxpayer", "uploaded_at": now_iso()} for p in ans.split(",") if p.strip() and p.strip() != "done"]
                g.invoke(Command(resume={"text": "done", "documents": docs}), cfg)
            continue
        ans = input("you> ").strip()
        if ans.lower() in ("quit", "exit"):
            print("Saved. Resume with: python run.py chat --resume", rid)
            return 0
        out = g.invoke(Command(resume=ans), cfg)
        msgs = out.get("messages", [])
        if msgs and msgs[-1].type == "ai" and msgs[-1].content and msgs[-1].content != iv["text"]:
            print("SilverFile:", msgs[-1].content)
    out = g.get_state(cfg).values
    print("\nSTATUS:", out.get("status"))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    from silverfile.batch import run_batch

    return run_batch(docs=args.docs, strategy="none" if args.no_context_strategy else "selective", write_metrics=not args.no_write)


def cmd_graph(args: argparse.Namespace) -> int:
    from silverfile.graph import build_graph

    mmd = build_graph().compile().get_graph().draw_mermaid()
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/graph.mmd").write_text(mmd)
    print(mmd)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from silverfile.seed import seed_demo_returns

    ids = seed_demo_returns(force=args.force)
    print(f"seeded {len(ids)} demo return(s)" if ids else "index not empty; nothing seeded (use --force to add anyway)")
    return 0


def cmd_staff(args: argparse.Namespace) -> int:
    import getpass

    from silverfile.security import auth

    if args.action == "list":
        for r in auth.list_staff():
            print(f"{r['username']:<24} {r['role']:<10} {'disabled' if r['disabled'] else 'active'}")
        return 0
    if not args.name:
        print("a username is required")
        return 2
    pw = getpass.getpass("Password (12+ characters): ")
    if pw != getpass.getpass("Again: "):
        print("the passwords differ")
        return 1
    try:
        if args.action == "add":
            auth.create_staff(args.name, pw, args.role)
        else:
            auth.set_password(args.name, pw)
    except ValueError as e:
        print(e)
        return 1
    print(f"{args.action}: {args.name} ok")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from silverfile.security.retention import purge_abandoned

    gone = purge_abandoned(args.days, include_finished=args.include_finished)
    print(f"deleted {len(gone)} return(s)" + (": " + ", ".join(gone) if gone else ""))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from silverfile.security import audit

    ok, bad, n = audit.verify_chain()
    print(f"security log: {n} events, chain {'intact' if ok else f'BROKEN at event {bad}'}")
    for e in reversed(audit.recent(args.last)):
        print(f"  {e['seq']:>5} {e['at']} {e['kind']:<26} {e['actor'] or '-':<28} {e['return_id'] or '-'}")
    return 0 if ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("silverfile.dashboard.app:create_app", factory=True, host=args.host, port=args.port, reload=False)
    return 0


def main() -> int:
    from silverfile.security.pii import install_log_redaction

    install_log_redaction()  # no SSN or API key reaches a log line, whichever library logs it
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed"); s.add_argument("--force", action="store_true", help="seed even when returns already exist"); s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8000); s.add_argument("--host", default="127.0.0.1"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("demo"); s.add_argument("case", nargs="?", default="case1_clean"); s.add_argument("--resolve", help="JSON resolution to apply to any escalation"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("chat"); s.add_argument("--resume"); s.add_argument("--ephemeral", action="store_true"); s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("batch"); s.add_argument("--docs", type=int, default=10); s.add_argument("--no-context-strategy", action="store_true"); s.add_argument("--no-write", action="store_true"); s.set_defaults(fn=cmd_batch)
    s = sub.add_parser("graph"); s.set_defaults(fn=cmd_graph)
    s = sub.add_parser("staff"); s.add_argument("action", choices=["add", "list", "passwd"]); s.add_argument("name", nargs="?"); s.add_argument("--role", default="preparer", choices=["clerk", "preparer", "admin"]); s.set_defaults(fn=cmd_staff)
    s = sub.add_parser("purge"); s.add_argument("--days", type=int); s.add_argument("--include-finished", action="store_true"); s.set_defaults(fn=cmd_purge)
    s = sub.add_parser("audit"); s.add_argument("--last", type=int, default=20); s.set_defaults(fn=cmd_audit)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
