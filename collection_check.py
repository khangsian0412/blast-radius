"""Check real patched files in isolated processes and retain uncertain tests."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import time

from selector import apply_change, iter_bits, parse_patch


class CheckServer:
    """Share external dependency imports, never patched modules or test state.

    Each request forks a fresh child of an untouched parent. SQLGlot and tests
    are imported only in that child, from its actual patched directory.
    """

    def __init__(self):
        self.process = None
        self.disabled = False
        self.cpu = 0.0

    def receive(self, timeout):
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise subprocess.TimeoutExpired("check server", timeout)
        line = self.process.stdout.readline(100_001)
        if not line.endswith(b"\n") or len(line) > 100_000:
            raise ValueError("incomplete check server response")
        return json.loads(line)

    def run(self, request, timeout):
        if self.disabled:
            return None, 0.0
        try:
            if self.process is None:
                self.process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--server"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                    cwd=Path(__file__).resolve().parent,
                    env={**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1",
                         "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
                )
                if self.receive(timeout) != {"ready": True}:
                    raise ValueError("check server did not initialize")
            self.process.stdin.write(json.dumps(request).encode() + b"\n")
            self.process.stdin.flush()
            envelope = self.receive(timeout)
            cpu = envelope["cpu_seconds"]
            if type(cpu) not in (int, float) or cpu < self.cpu:
                raise ValueError("invalid check CPU accounting")
            elapsed, self.cpu = cpu - self.cpu, cpu
            return envelope["result"], elapsed
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            self.disabled = True
            self.close()
            return None, 0.0

    def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            process.stdout.close()


def server():
    import gc
    import resource

    output = sys.stdout
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        for module in ("pytest", "numpy", "pandas", "pandas.testing", "duckdb", "setuptools.command.build_ext", "unittest.mock", "doctest"):
            importlib.import_module(module)
        from _pytest.config import default_plugins
        for plugin in default_plugins:
            importlib.import_module("_pytest." + plugin)
        # Import the installed external plugin modules, without registering
        # hooks or creating a pytest session in this parent.
        from importlib.metadata import entry_points
        for plugin in entry_points(group="pytest11"):
            importlib.import_module(plugin.module)
    if any(name == "sqlglot" or name.startswith(("sqlglot.", "tests.")) or name == "tests" for name in sys.modules):
        raise RuntimeError("check parent imported application code")
    # Age the shared external dependency graph once. Child collections then
    # start with empty young generations. Do not freeze objects or disable GC:
    # heap-inspection and reference-leak tests must retain their normal view.
    gc.collect()
    print(json.dumps({"ready": True}), file=output, flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                # Native libraries and spawned programs must not write to the
                # server protocol. The report has its own close-on-exec pipe.
                with open(os.devnull, "wb") as sink:
                    os.dup2(sink.fileno(), 1)
                    os.dup2(sink.fileno(), 2)
                os.environ.update(PYTHONPATH=request["source"], TMPDIR=request["temporary"])
                tempfile.tempdir = None
                with os.fdopen(write_fd, "w") as result:
                    worker(request, result)
            finally:
                os._exit(0)
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as result:
            payload = result.read(100_001)
        _, status = os.waitpid(pid, 0)
        report = json.loads(payload) if status == 0 and len(payload) <= 100_000 else None
        own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime
        print(json.dumps({"result": report, "cpu_seconds": cpu}), file=output, flush=True)


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
    def __init__(self, selector, budget=20, backend=None, allow_tests=True):
        self.selector = selector
        self.used = 0.0
        self.budget = budget
        self.deadline = time.monotonic() + 120
        self.backend = backend
        self.cached_check = None
        self.allow_tests = allow_tests

    def __call__(self, patch):
        return self.run(patch) == {"collection_ok": True}

    def eligible_tests(self, patch, selected):
        targets = [self.selector.tests[i] for i in iter_bits(selected)]
        if not targets or len(targets) > 50 or sum(test["seconds"] for test in targets) > 1.0:
            return None
        if len(targets) == 1 and getattr(self.selector, "execution_changed", True):
            return None
        # Resource-reader tests often launch a large worker pool. Their recorded
        # duration omits the cold verification startup, so keep them directly.
        try:
            if any(not (change.old or "").endswith(".py") for change in parse_patch(patch)):
                return None
        except ValueError:
            return None
        names = {test["id"] for test in targets}
        if any(item["test"] in names for item in self.selector.map.get("task_ids", ())):
            return None
        return [test["id"] for test in targets]

    def validate_selection(self, patch, selected):
        targets = self.eligible_tests(patch, selected) if self.allow_tests else None
        if targets is None:
            return self(patch)
        report = self.run(patch, targets, require_imports=True)
        self.cached_check = (patch, tuple(targets), report) if isinstance(report, dict) and not report.get("late_import") else None
        return isinstance(report, dict) and report.get("collection_ok") is True

    def check_tests(self, patch, selected):
        targets = self.eligible_tests(patch, selected)
        if targets is None:
            return selected
        cached, self.cached_check = self.cached_check, None
        report = cached[2] if cached is not None and cached[:2] == (patch, tuple(targets)) else self.run(patch, targets)
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

    def run(self, patch, tests=None, require_imports=False):
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
                    request["require_imports"] = require_imports
                if self.backend is not None:
                    request["temporary"] = directory
                    report, cpu = self.backend.run(request, min(8, max(0.1, self.deadline - time.monotonic())))
                    self.used += cpu
                    return report
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


def worker(request=None, output=None):
    request = json.load(sys.stdin) if request is None else request
    root = Path(request["source"]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    output = sys.stdout if output is None else output
    failures = []
    failed, call_passed, completed = set(), set(), set()
    unsafe = [False]
    imported_late = [False]
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
                        if request.get("require_imports"):
                            # Normally full collection has already imported the
                            # changed module. A late forced import may change
                            # test initialization, so it can validate collection
                            # but cannot certify passing tests for removal.
                            for module in request["modules"]:
                                if module not in sys.modules:
                                    imported_late[0] = True
                                    importlib.import_module(module)
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
        report = {"collection_ok": not failures and result in (0, 1), "passed": sorted(completed - failed) if not unsafe[0] and not imported_late[0] and result in (0, 1) else [], "failed": sorted(failed)}
        if imported_late[0]:
            report["late_import"] = True
        json.dump(report, output)
    else:
        json.dump({"collection_ok": passed}, output)


if __name__ == "__main__":
    server() if "--server" in sys.argv else worker()
