"""Replay source-backed layouts with golden reading, or opt in to live extraction with --live.

Offline results test document rendering, classification safeguards, persisted facts, rules and actions; they do not
measure the model's ability to read an unseen layout. --live exercises that reader and requires an Anthropic key.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from run_evals import run_case

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    if args.live and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Live extraction was not run: ANTHROPIC_API_KEY is required.", file=sys.stderr)
        return 2
    cases = json.loads((ROOT / "evals/web_mailboxes/cases.json").read_text())
    results = []
    for case in cases:
        mailbox = ROOT / case["mailbox"]
        harness = str(ROOT / "harness/harness.py") if args.live else shlex.join(
            [sys.executable, str(ROOT / "evals/fixture_harness.py"), str(mailbox / "golden.json")])
        for mode in ("supervised", "autonomous"):
            c = {**case, "mode": mode, "expect": dict(case["expect"])}
            if c["expect"]["verdict"] == "finding":
                c["expect"]["action"] = "draft:dispute" if mode == "supervised" else "execute:dispute"
            records, bad = run_case(harness, mailbox, c, 600)
            results.append({"case": case["name"], "mode": mode, "pass": not bad, "diff": bad,
                            "reports_checked": len(records)})
            print(f"{'FAIL' if bad else 'PASS'} {mode:10} {case['name']}" + (f": {'; '.join(bad)}" if bad else ""))
    out = {"reader": "live API" if args.live else "authored golden facts", "passed": sum(r["pass"] for r in results),
           "total": len(results), "cases": results}
    (ROOT / "evals" / ("web_live_results.json" if args.live else "web_results.json")).write_text(json.dumps(out, indent=1) + "\n")
    print(f"{out['passed']}/{out['total']} pass ({out['reader']})")
    return 0 if out["passed"] == out["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
