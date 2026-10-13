#!/usr/bin/env python3
"""Run the typed-answer grader over the whole active bank (local content DB or a JSONL dump).

    python scripts/open_answer_bank_regression.py                 # docker exec algo-content-db ... (read-only select)
    python scripts/open_answer_bank_regression.py --jsonl bank.jsonl

Prints only the counts and the suspicious tasks.  Exit code 1 if a distractor is accepted that is not a known bank
defect, or if a key typed without `$` is rejected.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.open_answer_bank import BANK_DEFECTS, regression  # noqa: E402

QUERY = (
    "select json_build_object('id',id,'correct_answer',correct_answer,'distractor_meta',distractor_meta) "
    "from tasks_master where is_active"
)


def load(jsonl: str | None) -> list[dict]:
    if jsonl:
        with open(jsonl, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    out = subprocess.run(
        ["docker", "exec", "algo-content-db", "psql", "-U", "algo", "-d", "algo_content", "-tA", "-c", QUERY],
        check=True, capture_output=True, text=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl")
    args = parser.parse_args()
    stats = regression(load(args.jsonl))
    for name, value in stats.items():
        if not isinstance(value, list):
            print(f"{name}: {value}")
    unexpected = [v for v in stats["violations"] if v[0] not in BANK_DEFECTS]
    for task_id, key, bad in stats["violations"]:
        tag = "KNOWN" if task_id in BANK_DEFECTS else "NEW"
        print(f"[{tag}] {task_id} key={key[:50]!r} distractors={[b[:40] for b in bad]}")
    for task_id, key in stats["self_failures"][:20]:
        print(f"[SELF-FAIL] {task_id} key={key[:60]!r}")
    return 1 if unexpected or stats["self_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
