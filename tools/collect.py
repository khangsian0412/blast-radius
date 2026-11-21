"""Collect patch-independent coverage and fixture dependencies from the baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    import coverage
    import pytest

    active = [None]
    reads = {}
    collected = []
    outcomes = {}
    class_reads = {}
    class_keys = {}
    untraced = set()
    class_contexts = {}
    prefix = str(root) + "/"

    def audit(event, values):
        if event == "subprocess.Popen" and active[0]:
            untraced.add(active[0])
        if event != "open" or not active[0]:
            return
        name = values[0]
        if not isinstance(name, str):
            return
        if not name.startswith("/"):
            name = str(root / name)
        if name.startswith(prefix + "tests/fixtures/"):
            reads.setdefault(name[len(prefix) :], set()).add(active[0])

    sys.addaudithook(audit)
    cov = coverage.Coverage(
        source=[str(root / "sqlglot"), str(root / "tests")], data_file=None
    )
    cov.start()

    # Preserve task argument/result serialization while running pool tasks in the
    # calling thread. Otherwise spawn workers lose the active test's coverage.
    # This is baseline instrumentation only, never used on submitted patches.
    import concurrent.futures
    import pickle

    class TracedPool:
        def __init__(self, *args, initializer=None, initargs=(), **kwargs):
            self.initializer = initializer
            self.initargs = initargs
            self.initialized = False

        def submit(self, fn, /, *args, **kwargs):
            future = concurrent.futures.Future()
            try:
                if not self.initialized:
                    self.initialized = True
                    if self.initializer:
                        self.initializer(*self.initargs)
                fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                value = fn(*args, **kwargs)
                future.set_result(pickle.loads(pickle.dumps(value)))
            except BaseException as error:
                future.set_exception(error)
            return future

        def shutdown(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.shutdown()

    concurrent.futures.ProcessPoolExecutor = TracedPool

    # Capture dependencies on class constants which execute only at import time.
    from sqlglot.expressions.core import Expr
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import Tokenizer
    import functools

    def instrument_init(base):
        original = base.__init__

        @functools.wraps(original)
        def traced(self, *args, **kwargs):
            context = active[0]
            if context:
                cls = type(self)
                if cls not in class_keys:
                    class_keys[cls] = tuple(
                        f"{parent.__module__}.{parent.__qualname__}"
                        for parent in cls.__mro__
                        if parent.__module__.startswith("sqlglot")
                    )
                class_reads.setdefault(context, set()).update(class_keys[cls])
            original(self, *args, **kwargs)

        base.__init__ = traced

    for base in (Expr, Dialect, Parser, Generator, Tokenizer):
        instrument_init(base)

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            collected.extend(item.nodeid for item in items)
            classes = {item.cls for item in items if getattr(item, "cls", None)}
            for cls in classes:
                members = [
                    item.nodeid for item in items if getattr(item, "cls", None) is cls
                ]
                tag = "@class:" + members[0].rsplit("::", 1)[0]
                class_contexts[tag] = members
                for method in ("setUpClass", "tearDownClass"):
                    original = getattr(cls, method, None)
                    if original is None:
                        continue

                    def wrap(original, tag):
                        def run(_cls):
                            previous = active[0]
                            active[0] = tag
                            cov.switch_context(tag)
                            try:
                                return original()
                            finally:
                                active[0] = previous
                                cov.switch_context(previous or "")

                        return classmethod(run)

                    setattr(cls, method, wrap(original, tag))

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            active[0] = item.nodeid
            cov.switch_context(item.nodeid)
            yield
            cov.switch_context("")
            active[0] = None

        def pytest_runtest_logreport(self, report):
            if report.when == "call":
                outcomes[report.nodeid] = report.outcome

    result = pytest.main(
        ["tests", "-q", "--disable-warnings", "-o", "addopts=", "--tb=short"],
        plugins=[Plugin()],
    )
    cov.stop()
    if result != 0:
        raise SystemExit(
            f"Baseline test run failed (pytest exit {result}); refusing to publish coverage"
        )
    data = cov.get_data()
    ids = sorted(set(collected))
    index = {name: i for i, name in enumerate(ids)}
    pool = [0]
    pool_index = {0: 0}

    def intern(contexts):
        bits = 0
        for context in contexts:
            for name in class_contexts.get(context, [context]):
                if name in index:
                    bits |= 1 << index[name]
        if bits not in pool_index:
            pool_index[bits] = len(pool)
            pool.append(bits)
        return pool_index[bits]

    files = {}
    for name in sorted(data.measured_files()):
        if not name.startswith(prefix):
            continue
        path = Path(name)
        rel = name[len(prefix) :]
        lines = {
            str(line): intern(contexts)
            for line, contexts in data.contexts_by_lineno(name).items()
        }
        lines = {k: v for k, v in lines.items() if v}
        files[rel] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "lines": lines,
        }
    fixture_reads = {name: intern(contexts) for name, contexts in sorted(reads.items())}
    uses = {}
    for context, keys in class_reads.items():
        for key in keys:
            uses.setdefault(key, set()).add(context)
    classes = {key: intern(contexts) for key, contexts in sorted(uses.items())}
    always = intern(untraced | {name for name in ids if outcomes.get(name) != "passed"})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema": 1,
                "baseline": BASELINE,
                "tests": ids,
                "pool": [format(bits, "x") for bits in pool],
                "files": files,
                "fixtures": fixture_reads,
                "classes": classes,
                "always": always,
                "collection": {
                    "exit_code": int(result),
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "collector_sha256": hashlib.sha256(
                        Path(__file__).read_bytes()
                    ).hexdigest(),
                    "outcomes": {
                        value: sum(v == value for v in outcomes.values())
                        for value in sorted(set(outcomes.values()))
                    },
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        f"Wrote {len(files)} files, {len(pool)} coverage sets, {len(fixture_reads)} fixtures, {len(ids)} tests to {output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
