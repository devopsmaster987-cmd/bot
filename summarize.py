#!/usr/bin/env python3
"""
summarize.py - render the newest browser test result per test as a markdown
table, print it to stdout, append it to $GITHUB_STEP_SUMMARY, and exit non-zero
if anything failed.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="reports/browser_results.csv")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    args = ap.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"No results file at {path}; nothing to summarise.")
        return 0

    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        latest[row["test_name"]] = row  # file is append-ordered: last wins

    lines = ["## Browser test results", "", "| Test | Result | Time | Notes |", "| --- | --- | --- | --- |"]
    for row in latest.values():
        note = (row.get("error") or "").replace("|", "\\|")[:120]
        result = "PASS" if row.get("passed") == "1" else "FAIL"
        lines.append(f"| {row['test_name']} | {result} | {row.get('duration_s', '')}s | {note} |")
    table = "\n".join(lines)
    print(table)

    if args.summary and Path(args.summary).parent.exists():
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(table + "\n")

    failures = [r for r in latest.values() if r.get("passed") != "1"]
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())