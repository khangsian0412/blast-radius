"""Check patched collection against real files; never certify passing tests.

Only import/collection success can enable a measured configuration dependency
reduction. Every error, timeout, or incomplete response keeps the full suite.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from selector import apply_change, parse_patch


def overlay(source, destination, changes, prefix=""):
    destination.mkdir()
    for entry in source.iterdir():
        if entry.name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        name = prefix + entry.name
        target = destination / entry.name
        if name in changes:
            target.write_text(changes[name], encoding="utf-8")
        elif any(changed.startswith(name + "/") for changed in changes):
            overlay(entry, target, changes, name + "/")
        else:
            target.symlink_to(entry, target_is_directory=entry.is_dir())


class CollectionCheck:
    def __init__(self, selector, budget=20):
        self.selector = selector
        self.used = 0.0
        self.budget = budget
        self.deadline = time.monotonic() + 120

    def __call__(self, patch):
        if self.used >= self.budget or time.monotonic() >= self.deadline:
            return False
        started = time.process_time()
        import resource
        previous = resource.getrusage(resource.RUSAGE_CHILDREN)
        process = None
        try:
            changes = {}
            for change in parse_patch(patch):
                if change.old != change.new or not change.old.startswith("sqlglot/") or not change.old.endswith(".py"):
                    return False
                changes[change.old] = apply_change(self.selector.read(change.old), change)[0]
            with tempfile.TemporaryDirectory(prefix="blast-collection-") as directory:
                root = Path(directory) / "source"
                overlay(self.selector.source.resolve(), root, changes)
                request = {"source": str(root), "modules": [name.removesuffix(".py").replace("/", ".").removesuffix(".__init__") for name in changes]}
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root), "SKIP_INTEGRATION": "0", "TMPDIR": directory},
                )
                stdout, _ = process.communicate(json.dumps(request).encode(), timeout=min(8, max(0.1, self.deadline - time.monotonic())))
                if process.returncode or len(stdout) > 1000:
                    return False
                return json.loads(stdout) == {"collection_ok": True}
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return False
        finally:
            following = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.used += time.process_time() - started + following.ru_utime + following.ru_stime - previous.ru_utime - previous.ru_stime


def worker():
    request = json.load(sys.stdin)
    root = Path(request["source"]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    output = sys.stdout
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            for module in request["modules"]:
                importlib.import_module(module)
            import pytest
            failures = []

            class Plugin:
                def pytest_collectreport(self, report):
                    if report.failed:
                        failures.append(report.nodeid)

            result = pytest.main(["tests", "--collect-only", "--continue-on-collection-errors", "-q", "--tb=no", "-o", "addopts=", "-p", "no:cacheprovider"], plugins=[Plugin()])
            passed = result == 0 and not failures
        except BaseException:
            passed = False
    json.dump({"collection_ok": passed}, output)


if __name__ == "__main__":
    worker()
