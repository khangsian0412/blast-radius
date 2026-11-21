"""Offline evaluation only. This file and sample answers are not in the image."""

import argparse
import json
from pathlib import Path
import sys

from score import evaluate_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--require-no-misses", action="store_true")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    answers = json.loads((args.data / "answers.json").read_text())["commits"]
    patches = {path.stem for path in (args.data / "commits").glob("*.patch")}
    if patches != set(answers):
        raise SystemExit("Sample patches and answer IDs differ")
    stdout = args.output.read_bytes()
    if len(stdout) > 2 * 1024 * 1024:
        raise SystemExit("Output exceeds 2 MB limit")
    result = evaluate_run(stdout, tests, answers)
    Path("validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["ok"] or (args.require_no_misses and result["missed_commits"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
