"""Isolated, CPU-bounded checking of patched imports and selected tests.

The parent supplies baseline-derived task coverage. Unaffected optimizer tasks
are skipped using unittest subtests; executor tasks use their existing None path.
Unknown tasks always execute. A timeout or uncertain result never removes a test.
"""

from __future__ import annotations
import concurrent.futures
import contextlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import pickle
import signal
import sys
import unittest


class Discard:
    encoding = "utf-8"

    def write(self, value):
        return len(value)

    def flush(self):
        pass

    def isatty(self):
        return False


def main():
    request = json.load(sys.stdin)
    root = Path(request["source"]).resolve()
    overrides = request["overrides"]
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            return compile(
                overrides[Path(self.path).relative_to(root).as_posix()],
                self.path,
                "exec",
                dont_inherit=True,
            )

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if (
                spec
                and spec.origin
                and Path(spec.origin).is_relative_to(root)
                and Path(spec.origin).relative_to(root).as_posix() in overrides
            ):
                spec.loader = Loader(fullname, spec.origin)
                return spec
            return None

    sys.meta_path.insert(0, Finder())
    active = [None]
    ordinals = {}
    plans = request.get("plans", {})

    class Pool:
        def __init__(self, *args, initializer=None, initargs=(), **kwargs):
            self.initializer = initializer
            self.initargs = initargs
            self.initialized = False

        def submit(self, fn, /, *args, **kwargs):
            if not self.initialized:
                self.initialized = True
                if self.initializer:
                    self.initializer(*self.initargs)
            test = active[0]
            ordinal = ordinals.get(test, 0)
            ordinals[test] = ordinal + 1
            plan = plans.get(test)
            known = plan is not None and ordinal < plan["count"]
            skip = known and ordinal not in plan["run"]
            module = getattr(fn, "__module__", "")
            name = getattr(fn, "__name__", "")
            protocol = (
                "skip"
                if module == "tests.test_optimizer" and name == "parse_and_optimize"
                else "none"
                if module == "tests.test_executor" and name == "mp_execute"
                else None
            )
            future = concurrent.futures.Future()
            future.set_result(None)  # wait()/as_completed() see ready, lazy results.
            evaluated = [False]
            result = [None]
            error = [None]

            def resolve(timeout=None):
                if not evaluated[0]:
                    evaluated[0] = True
                    try:
                        if skip and protocol:
                            if protocol == "skip":
                                raise unittest.SkipTest("Unchanged baseline task")
                            result[0] = None
                        else:
                            callable_, values, options = pickle.loads(
                                pickle.dumps((fn, args, kwargs))
                            )
                            result[0] = pickle.loads(
                                pickle.dumps(callable_(*values, **options))
                            )
                    except BaseException as caught:
                        error[0] = caught
                if error[0] is not None:
                    raise error[0]
                return result[0]

            future.result = resolve
            return future

        def shutdown(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    concurrent.futures.ProcessPoolExecutor = Pool
    import pytest

    failed = set()
    call_passed = set()
    completed = set()
    collection_errors = set()
    collection_ok = not request.get("collect", False)
    aborted = [False]

    class Plugin:
        def pytest_collectreport(self, report):
            if report.failed:
                collection_errors.add(report.nodeid)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            active[0] = item.nodeid
            yield
            active[0] = None

        def pytest_runtest_logreport(self, report):
            if report.failed:
                failed.add(report.nodeid)
                completed.discard(report.nodeid)
                if report.when == "teardown":
                    completed.clear()
                if hasattr(report, "context"):
                    # Stop at the first failing subtest instead of evaluating all
                    # SQL cases. This is a confirmed failure, not a passing probe.
                    pytest.exit("A target test failed", returncode=1)
            elif (
                report.when == "call"
                and report.passed
                and not hasattr(report, "context")
            ):
                call_passed.add(report.nodeid)
            elif (
                report.when == "teardown"
                and report.passed
                and report.nodeid in call_passed
                and report.nodeid not in failed
            ):
                completed.add(report.nodeid)

    plugin = Plugin()
    output = sys.stdout
    common = [
        "-q",
        "-s",
        "--tb=no",
        "--assert=plain",
        "--disable-warnings",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
    ]
    with contextlib.redirect_stdout(Discard()), contextlib.redirect_stderr(Discard()):
        if request.get("collect"):
            code = pytest.main(
                ["tests", "--collect-only", "--continue-on-collection-errors", *common],
                plugins=[plugin],
            )
            collection_ok = code == 0 and not collection_errors
        if collection_ok and request.get("tests"):

            def expire(signum, frame):
                aborted[0] = True
                raise KeyboardInterrupt("Probe CPU budget exhausted")

            signal.signal(signal.SIGPROF, expire)
            signal.setitimer(signal.ITIMER_PROF, request.get("cpu_budget", 1.0))
            try:
                pytest.main(
                    [*request["tests"], "--maxfail=1", *common], plugins=[plugin]
                )
            finally:
                signal.setitimer(signal.ITIMER_PROF, 0)
    json.dump(
        {
            "passed": sorted(completed - failed),
            "failed": sorted(failed),
            "collection_ok": collection_ok,
            "collection_errors": sorted(collection_errors),
            "aborted": aborted[0],
        },
        output,
        separators=(",", ":"),
    )
    output.write("\n")


if __name__ == "__main__":
    main()
