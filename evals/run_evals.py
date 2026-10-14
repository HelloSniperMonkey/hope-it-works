"""Run your eval cases (evals/cases/*.yaml) through your harness and score them. Behind `make eval`.

    python evals/run_evals.py [--harness harness/harness.py] [--cases evals/cases] [--out evals/results.json] [-k name]

Each case uses grader/run_grader.py's replay: restricted environment, empty cwd, fresh processes, and a report before
feedback after every batch. `by_batch` optionally specifies intermediate dates and expectations. A final empty batch
allows single-batch feedback cases to be checked after the decision. Every report's schema, keys and quotes are checked.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "grader"))
sys.path.insert(0, str(ROOT / "tools"))
import score as S  # noqa: E402
import run_grader as R  # noqa: E402
from run_mailbox import harness_cmd, batch_dates  # noqa: E402

SCHEMA = json.loads((ROOT / "grader" / "schema" / "report.schema.json").read_text())


def find_item(report: dict, key: dict) -> dict | None:
    want = S.key_tuple(key)
    for it in S.report_items(report):
        if S.key_tuple(it) == want:
            return it
    return None


def check(case: dict, item: dict | None, previous: dict | None = None) -> list[str]:
    """-> what differs (empty when the case passes)."""
    exp = case["expect"]
    v = exp.get("verdict", "finding")
    if v == "no_finding":
        return [] if item is None or item.get("verdict") == "no_finding" else \
            [f"expected no finding, got {item.get('verdict')} {item.get('amount_usd') or ''}".strip()]
    if item is None:
        return ["key missing from the report"]
    bad = []
    if item.get("verdict") != v:
        bad.append(f"verdict {item.get('verdict')}, expected {v}")
    if "amount_usd" in exp and exp["amount_usd"] is not None and not S.amount_ok(item.get("amount_usd"), exp["amount_usd"]):
        bad.append(f"amount {item.get('amount_usd')}, expected {exp['amount_usd']}")
    if exp.get("amount_range") and not S.range_ok(item.get("amount_range"), exp["amount_range"]):
        bad.append(f"range {item.get('amount_range')}, expected {exp['amount_range']}")
    if exp.get("action") and str(item.get("action")) != exp["action"]:
        bad.append(f"action {item.get('action')}, expected {exp['action']}")
    if exp.get("deadline") and str(item.get("deadline")) != str(exp["deadline"]):
        bad.append(f"deadline {item.get('deadline')}, expected {exp['deadline']}")
    want_doc = (exp.get("missing_doc") or {}).get("name") if isinstance(exp.get("missing_doc"), dict) else exp.get("missing_doc")
    if want_doc:
        got = ((item.get("missing_doc") or {}).get("name") or "").lower()
        if not all(w in got for w in str(want_doc).lower().split()):
            bad.append(f"missing_doc {got or 'none'}, expected {want_doc}")
    holder = (exp.get("missing_doc") or {}).get("holder") if isinstance(exp.get("missing_doc"), dict) else None
    if holder and not (S._holder_names(holder) & S._holder_names((item.get("missing_doc") or {}).get("holder"))):
        bad.append("missing-document holder does not match")
    if "retro" in exp and item.get("retro", False) != exp["retro"]:
        bad.append(f"retro {item.get('retro', False)}, expected {exp['retro']}")
    if exp.get("learned_at_required") and not item.get("learned_at"):
        bad.append("learned_at missing")
    if exp.get("learned_at") and item.get("learned_at") != exp["learned_at"]:
        bad.append(f"learned_at {item.get('learned_at')}, expected {exp['learned_at']}")
    if exp.get("supersedes_required") and (not previous or previous["id"] not in item.get("supersedes", []) or
                                         previous["id"] == item["id"]):
        bad.append("supersedes must name the preceding report's item with a new id")
    for doc in exp.get("evidence_docs") or []:
        if not any(S.same_doc(doc, e["doc"]) for e in item.get("evidence") or []):
            bad.append(f"evidence missing {doc}")
    return bad


def received_date(batch: Path, fallback: str) -> str:
    dates = []
    for f in batch.glob("*.eml"):
        msg = BytesParser(policy=policy.default).parsebytes(f.read_bytes(), headersonly=True)
        dates.append(parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc).date().isoformat())
    return max(dates, default=fallback)


def run_case(harness: str, mailbox: Path, case: dict, timeout: float) -> tuple[list[dict], list[str]]:
    """Use the actual grader stepper, preserving mailbox refs while keeping fixtures and keys outside its cwd."""
    batches = sorted(p for p in mailbox.glob("batch_*") if p.is_dir())
    if not batches:
        return [], [f"no batch_* folders in {mailbox}"]
    dates = batch_dates(mailbox)
    checks = {b["after_batch"]: b for b in case.get("by_batch") or []}
    fb = case.get("feedback") or {}
    schedule = [(b.name, str(checks.get(b.name, {}).get("as_of") or dates.get(b.name) or
                              min(str(case["as_of"]), received_date(b, str(case["as_of"]))))) for b in batches]
    if batches[-1].name not in fb:
        schedule[-1] = (batches[-1].name, str(case["as_of"]))
    with tempfile.TemporaryDirectory(prefix="eval-") as tmp:
        data = Path(tmp) / "data"
        data.mkdir()
        for b in batches:
            shutil.copytree(b, data / b.name)
        for batch, path in fb.items():
            dest = data / "feedback" / f"EVAL_{batch}.json"
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / path, dest)
        if batches[-1].name in fb:
            name = f"batch_{max(int(b.name.split('_')[-1]) for b in batches) + 1:02d}"
            (data / name).mkdir()
            schedule.append((name, str(case["as_of"])))
        key = {"worlds": {"EVAL": schedule}, "scenarios": []}
        mode = case.get("mode", "supervised")
        runs, _ = R.replay(harness_cmd(harness), data, key, [mode], timeout, log=lambda *_: None)
        records = runs[mode]["EVAL"]
        bad = []
        mb = S.WorldMailbox(data)
        previous = None
        for r in records:
            if r.get("error"):
                bad.append(f"{r['batch']}: {r['error']}")
                continue
            report = r["report"]
            schema_errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(report))
            for e in schema_errors:
                bad.append(f"{r['batch']}: report schema: {e.message}")
            if schema_errors:
                continue
            if report["as_of"] != r["as_of"] or report["mode"] != mode:
                bad.append(f"{r['batch']}: report date/mode does not match the invocation")
            if (report.get("diagnostics") or {}).get("unreadable_documents"):
                bad.append(f"{r['batch']}: unreadable documents were silently omitted")
            if r is records[-1]:
                for field, expected in (case.get("observed") or {}).items():
                    got = (report.get("diagnostics") or {}).get(field)
                    matches = sorted(got or []) == sorted(expected) if isinstance(expected, list) else got == expected
                    if not matches:
                        bad.append(f"extraction coverage {field}: {got}, expected {expected}")
            seen = set()
            for it in S.report_items(report):
                k = S.key_tuple(it)
                if k in seen:
                    bad.append(f"{r['batch']}: duplicate key {S.key_str(k)}")
                seen.add(k)
                for ev in it.get("evidence") or []:
                    if len("".join(ev["quote"].split())) < S.MIN_QUOTE:
                        bad.append(f"{r['batch']}: evidence quote shorter than {S.MIN_QUOTE} non-space characters")
                    err = mb.check(ev["doc"], ev["page"], ev["quote"])
                    if err:
                        bad.append(f"{r['batch']}: invalid evidence: {err}")
            outbound = json.dumps([[it.get(f) for f in S.OUTBOUND_FIELDS] for it in S.report_items(report)]).lower()
            for text in case.get("forbidden") or []:
                if text.lower() in outbound:
                    bad.append(f"{r['batch']}: forbidden outbound text {text}")
            it = find_item(report, case["expect"]["key"])
            if r["batch"] in checks:
                exp = {**case["expect"], **checks[r["batch"]]["expect"]}
                bad += [f"{r['batch']}: {d}" for d in check({"expect": exp}, it, previous)]
            if r is records[-1]:
                bad += check(case, it, previous)
            previous = it
        return records, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_evals.py", description="score your eval cases")
    ap.add_argument("--harness", default="harness/harness.py")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "results.json"))
    ap.add_argument("-k", help="only cases whose file name contains this")
    ap.add_argument("--timeout", type=float, default=600.0)
    a = ap.parse_args(argv)
    files = sorted(Path(a.cases).glob("*.yaml"))
    if a.k:
        files = [f for f in files if a.k in f.name]
    if not files:
        print(f"no cases in {a.cases}", file=sys.stderr)
        return 2
    results = []
    for f in files:
        case = yaml.safe_load(f.read_text())
        mailbox = (ROOT / case["mailbox"]) if not Path(case["mailbox"]).is_absolute() else Path(case["mailbox"])
        records, diff = run_case(a.harness, mailbox, case, a.timeout)
        ok = not diff
        results.append({"case": f.name, "name": case.get("name"), "pass": ok, "diff": diff,
                        "reports_checked": len(records)})
        print(f"{'PASS' if ok else 'FAIL'}  {f.name}: {case.get('name', '')}" + ("" if ok else f"\n      {'; '.join(diff)[:400]}"))
    n = sum(r["pass"] for r in results)
    ours = sum(1 for r in results if r["case"].startswith("sample_"))
    summary = {"harness": a.harness, "passed": n, "total": len(results), "your_cases": len(results) - ours,
               "score": round(n / len(results), 4), "cases": results}
    if not a.k:                                  # a filtered run never overwrites the full results
        Path(a.out).write_text(json.dumps(summary, indent=1) + "\n")
    print(f"\n{n} of {len(results)} cases pass ({len(results) - ours} of them yours)" + ("" if a.k else f"; wrote {a.out}"))
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
