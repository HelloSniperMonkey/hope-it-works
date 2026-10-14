"""Offline extraction adapter ONLY for authored tests. The production harness never imports this module.

Replace the reader with explicit golden facts, then run the real ingest/report/feedback CLI in fresh processes.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
import harness as H
import llm


def main():
    golden = json.loads(Path(sys.argv[1]).read_text())
    errors = []

    def extract(state, kind, sha, *args):
        f = golden.get(sha)
        if not f or f["kind"] != kind:
            errors.append(f"golden facts missing for {kind}/{sha}")
            raise ValueError(errors[-1])
        return copy.deepcopy(f["facts"])

    def transcribe(state, sha, pngs):
        f = golden.get(sha) or {}
        if not f.get("scan_pages"):
            errors.append(f"golden scan transcription missing for {sha}")
            raise ValueError(errors[-1])
        return f["scan_pages"]

    llm.extract, llm.transcribe = extract, transcribe
    result = H.main(sys.argv[2:])
    return 1 if errors else result


if __name__ == "__main__":
    sys.exit(main())
