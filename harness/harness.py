"""Your harness. The grader calls exactly these three commands, in a fresh process each time:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside) and store what you learn under --state-dir. The process
  is restarted between batches, so everything you need later must be on disk.
- report: print one report.json (grader/schema/report.schema.json) to stdout. Only documents received on or before
  --as-of count. --mode defaults to supervised.
- feedback: take in a person's decisions on what you escalated (feedback.json) and let them change later reports.

The environment gives ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL. Keep the argument names as they are: grader/validate.py
checks them. Replace the NotImplementedError bodies with your code.

How it works: ingest asks Claude to read each document into typed facts (llm.py), each fact pointing at the lines it
came from; report applies the glossary's rules to those facts in plain Python (engine.py). The model never decides a
verdict, an amount or an action.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs as DOCS  # noqa: E402

DEFAULT_STATE = ".state"
WORKERS = 12


def _resolve(facts, pages: list[str]):
    """Turn every '<x>src' line reference into the exact page text, so quotes are the grader's own text."""
    if isinstance(facts, dict):
        for k in list(facts):
            v = facts[k]
            if k.endswith("src") and isinstance(v, str):
                q = DOCS.quote_of(pages, v)
                facts[k + "_q"] = {"page": q[0], "quote": q[1]} if q else None
            else:
                _resolve(v, pages)
    elif isinstance(facts, list):
        for x in facts:
            _resolve(x, pages)
    return facts


_SNIFF = [("credit_note", r"credit note"), ("invoice", r"invoice|statement of account|monthly statement|billing "
          r"document|charges advice|statement"), ("air_waybill", r"air waybill"),
          ("dg_declaration", r"declaration"), ("contract", r"master .*agreement")]


def _sniff(kind: str, d) -> str:
    """When the email-level reader couldn't place an attachment, let its own opening lines say what it is."""
    import re
    if kind != "other" or not d.pages:
        return kind
    head = " ".join(d.pages[0].split("\n")[:6]).lower()
    return next((k for k, pat in _SNIFF if re.search(pat, head)), kind)


def ingest(batch_dir: str, state_dir: str) -> None:
    import llm
    state = Path(state_dir)
    (state / "docs").mkdir(parents=True, exist_ok=True)
    batch = Path(batch_dir).name
    groups = [DOCS.read_email(f, batch) for f in sorted(Path(batch_dir).glob("*.eml"))]

    def guarded(fn, *a):
        try:
            return fn(*a)
        except Exception as e:              # noqa: BLE001 - one unreadable document must not sink the batch
            llm.log(f"ingest: {a[0] if a else ''}: {type(e).__name__}: {e}")
            return None

    scans = [d for g in groups for d in g if d.is_scan]
    with ThreadPoolExecutor(WORKERS) as ex:
        for d, pages in zip(scans, ex.map(lambda d: guarded(llm.transcribe, state, d.sha, d.scan_pngs), scans)):
            if pages:
                d.pages = pages

    def read_email(g):
        email, atts = g[0], g[1:]
        heads = "\n\n".join(f"<attachment name=\"{a.ref.split('#', 1)[1]}\">\n" +
                            "\n".join(a.pages[0].split("\n")[:25]) + "\n</attachment>" for a in atts)
        return llm.extract(state, "email", email.sha, DOCS.numbered(email.pages),
                           f"Opening lines of the attachments, for classifying them:\n{heads}" if atts else "")

    with ThreadPoolExecutor(WORKERS) as ex:
        email_facts = list(ex.map(lambda g: guarded(read_email, g), groups))

    jobs = []
    for g, ef in zip(groups, email_facts):
        kinds = {k["name"]: k["kind"] for k in (ef or {}).get("attachment_kinds") or []}
        for d in g[1:]:
            jobs.append((d, _sniff(kinds.get(d.ref.split("#", 1)[1], "other"), d)))
    with ThreadPoolExecutor(WORKERS) as ex:
        att_facts = list(ex.map(lambda j: guarded(lambda d, k: llm.extract(state, k, d.sha, DOCS.numbered(d.pages)),
                                                  *j), jobs))
    by_doc = {d.ref: (k, f) for (d, k), f in zip(jobs, att_facts)}
    for g, ef in zip(groups, email_facts):
        email = g[0]
        rec = {"meta": email.meta, "docs": [{"ref": email.ref, "kind": "email", "scan": False, "pages": email.pages,
                                             "facts": _resolve(ef, email.pages) if ef else None}]}
        for d in g[1:]:
            k, f = by_doc[d.ref]
            rec["docs"].append({"ref": d.ref, "kind": k if f else "unreadable", "scan": d.is_scan,
                                "facts": _resolve(f, d.pages) if f else None})
        name = email.ref.replace("/", "_").replace(".eml", "")
        (state / "docs" / f"{name}.json").write_text(json.dumps(rec, indent=1))
    (state / "batches.json").write_text(json.dumps(sorted(set(_read(state / "batches.json", []) + [batch]))))


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def report(as_of: str, mode: str, state_dir: str) -> dict:
    import engine
    state = Path(state_dir)
    fb = _read(state / "feedback.json", [])
    hist = _read(state / f"reports_{mode}.json", [])
    prev = {}
    for h in sorted(hist, key=lambda h: h["as_of"]):
        if h["as_of"] < as_of:
            prev = h["items"]
    rep = engine.build(state, date.fromisoformat(as_of), mode, fb, prev)
    items = rep["findings"] + [x for v in rep["portfolio"].values() for x in v]
    snap = {engine.key_of(i): {"id": i["id"], "sig": [i["verdict"], i["amount_usd"], list(i["amount_range"] or [])]}
            for i in items if i["verdict"] != "no_finding"}
    hist = [h for h in hist if h["as_of"] != as_of] + [{"as_of": as_of, "items": snap}]
    if state.is_dir():
        (state / f"reports_{mode}.json").write_text(json.dumps(hist, indent=1))
    return rep


def feedback(feedback_file: str, state_dir: str) -> None:
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    fb = json.loads(Path(feedback_file).read_text())
    stored = _read(state / "feedback.json", [])
    have = {x.get("item_id") for x in stored}
    for it in fb.get("items") or []:
        if it.get("item_id") in have:
            continue
        stored.append({**it, "_batch": fb.get("after_batch"), "_decided_at": fb.get("decided_at"),
                       "_decided_by": fb.get("decided_by"), "_file": Path(feedback_file).name})
    (state / "feedback.json").write_text(json.dumps(stored, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
