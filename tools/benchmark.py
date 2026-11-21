"""Run three offline Linux container checks and record sample cost/resources.

The measurement wrapper adds a small amount of CPU to the normal entry point.
These are local validation measurements, not an official leaderboard score.
"""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

from score import evaluate_run

MEASURE = """
import json, resource, runpy, sys
runpy.run_path('/app/selector.py', run_name='__main__')
a = resource.getrusage(resource.RUSAGE_SELF)
b = resource.getrusage(resource.RUSAGE_CHILDREN)
print('BLAST_METRICS ' + json.dumps({'cpu_seconds': a.ru_utime+a.ru_stime+b.ru_utime+b.ru_stime, 'max_rss_kib': a.ru_maxrss}), file=sys.stderr)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="blast-radius")
    parser.add_argument("--data", type=Path, default=Path("sample"))
    args = parser.parse_args()
    data = args.data.resolve()
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",
        "--user",
        "65534:65534",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{data / 'commits'}:/data/commits:ro",
        "-v",
        f"{data / 'tests.json'}:/data/tests.json:ro",
        "--entrypoint",
        "python",
        args.image,
        "-c",
        MEASURE,
    ]
    runs = []
    first = None
    for number in range(1, 4):
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, timeout=180, check=True)
        elapsed = time.monotonic() - started
        Path(f"output-run{number}.jsonl").write_bytes(result.stdout)
        Path(f"output-run{number}.log").write_bytes(result.stderr)
        if len(result.stdout) > 2 * 1024 * 1024:
            raise SystemExit("Output exceeds 2 MB")
        evaluation = evaluate_run(result.stdout, tests, answers)
        if not evaluation["ok"] or evaluation["missed_commits"]:
            raise SystemExit(f"Sample validation failed: {evaluation}")
        if first is not None and first != result.stdout:
            raise SystemExit("Repeated runs produced different output")
        first = result.stdout
        metrics = next(
            json.loads(line.removeprefix("BLAST_METRICS "))
            for line in result.stderr.decode().splitlines()
            if line.startswith("BLAST_METRICS ")
        )
        runs.append(
            {
                "run": number,
                "simulated_seconds": evaluation["simulated_milliseconds"] / 1000,
                "wall_seconds": elapsed,
                "output_bytes": len(result.stdout),
                "misses": 0,
                **metrics,
            }
        )
    report = {
        "runs": runs,
        "median_simulated_plus_measured_cpu_seconds": statistics.median(
            run["simulated_seconds"] + run["cpu_seconds"] for run in runs
        ),
        "note": "Sample validation, not an official score. CPU includes the measurement wrapper.",
    }
    Path("benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    sys.exit(main())
