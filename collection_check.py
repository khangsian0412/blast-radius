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

from selector import apply_change, iter_bits, parse_patch


def overlay(source, destination, changes, prefix=""):
    destination.mkdir()
    for entry in source.iterdir():
        if entry.name in {".git", ".pytest_cache"}:
            continue
        name = prefix + entry.name
        target = destination / entry.name
        if entry.name == "__pycache__":
            changed_stems = {
                Path(changed).stem for changed in changes
                if changed.endswith(".py")
                and ("" if Path(changed).parent == Path(".") else Path(changed).parent.as_posix()) == prefix.rstrip("/")
            }
            if changed_stems:
                target.mkdir()
                for cached in entry.iterdir():
                    if not any(cached.name.startswith(stem + ".") for stem in changed_stems):
                        (target / cached.name).symlink_to(cached)
            else:
                target.symlink_to(entry, target_is_directory=True)
        elif name in changes:
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
        return self.run(patch) == {"collection_ok": True}

    def check_tests(self, patch, selected):
        targets = [self.selector.tests[i] for i in iter_bits(selected)]
        if not targets or len(targets) > 50 or sum(test["seconds"] for test in targets) > 1.0:
            return selected
        if len(targets) == 1 and getattr(self.selector, "execution_changed", True):
            return selected
        # Resource-reader tests often launch a large worker pool. Their recorded
        # duration omits the cold verification startup, so keep them directly.
        try:
            if any(not (change.old or "").endswith(".py") for change in parse_patch(patch)):
                return selected
        except ValueError:
            return selected
        names = {test["id"] for test in targets}
        if any(item["test"] in names for item in self.selector.map.get("task_ids", ())):
            return selected
        report = self.run(patch, [test["id"] for test in targets])
        if not isinstance(report, dict) or report.get("collection_ok") is not True:
            return selected
        passed, failed = report.get("passed"), report.get("failed")
        if not isinstance(passed, list) or not isinstance(failed, list) or not all(isinstance(node, str) for node in passed + failed):
            return selected
        passed = set(passed) - set(failed)
        for i in list(iter_bits(selected)):
            if self.selector.tests[i]["id"] in passed:
                selected &= ~(1 << i)
        return selected

    def run(self, patch, tests=None):
        if self.used >= self.budget or time.monotonic() >= self.deadline:
            return None
        started = time.process_time()
        import resource
        previous = resource.getrusage(resource.RUSAGE_CHILDREN)
        process = None
        try:
            changes = {}
            for change in parse_patch(patch):
                if change.old != change.new or not change.old:
                    return None
                if tests is None and (not change.old.startswith("sqlglot/") or not change.old.endswith(".py")):
                    return None
                changes[change.old] = apply_change(self.selector.read(change.old), change)[0]
            with tempfile.TemporaryDirectory(prefix="blast-collection-") as directory:
                root = Path(directory) / "source"
                overlay(self.selector.source.resolve(), root, changes)
                request = {"source": str(root), "modules": [name.removesuffix(".py").replace("/", ".").removesuffix(".__init__") for name in changes if name.endswith(".py")]}
                if tests is not None:
                    request["tests"] = tests
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root), "SKIP_INTEGRATION": "0", "TMPDIR": directory},
                )
                stdout, _ = process.communicate(json.dumps(request).encode(), timeout=min(8, max(0.1, self.deadline - time.monotonic())))
                if process.returncode or len(stdout) > 100_000:
                    return None
                return json.loads(stdout)
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return None
        finally:
            following = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.used += time.process_time() - started + following.ru_utime + following.ru_stime - previous.ru_utime - previous.ru_stime


def worker():
    request = json.load(sys.stdin)
    root = Path(request["source"]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    output = sys.stdout
    failures = []
    failed, call_passed, completed = set(), set(), set()
    unsafe = [False]
    result = 3
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            if "tests" not in request:
                for module in request["modules"]:
                    importlib.import_module(module)
            import pytest

            class Plugin:
                def pytest_collectreport(self, report):
                    if report.failed:
                        failures.append(report.nodeid)

                def pytest_collection_modifyitems(self, config, items):
                    if "tests" in request:
                        targets = set(request["tests"])
                        removed = [item for item in items if item.nodeid not in targets]
                        items[:] = [item for item in items if item.nodeid in targets]
                        config.hook.pytest_deselected(items=removed)

                def pytest_runtest_logreport(self, report):
                    if report.failed:
                        failed.add(report.nodeid)
                        completed.discard(report.nodeid)
                        if report.when == "teardown":
                            unsafe[0] = True
                    elif report.when == "call" and report.passed and not hasattr(report, "context"):
                        call_passed.add(report.nodeid)
                    elif report.when == "teardown" and report.passed and report.nodeid in call_passed and report.nodeid not in failed:
                        completed.add(report.nodeid)

            options = ["--collect-only", "--continue-on-collection-errors"] if "tests" not in request else ["--maxfail=1"]
            result = pytest.main(["tests", *options, "-q", "--tb=no", "-o", "addopts=", "-p", "no:cacheprovider"], plugins=[Plugin()])
            passed = result == 0 and not failures
        except BaseException:
            passed = False
    if "tests" in request:
        json.dump({"collection_ok": not failures, "passed": sorted(completed - failed) if not unsafe[0] and result in (0, 1) else [], "failed": sorted(failed)}, output)
    else:
        json.dump({"collection_ok": passed}, output)


if __name__ == "__main__":
    worker()
