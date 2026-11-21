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
    parser.add_argument("--raw", type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    raw = args.raw.resolve() if args.raw else None
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
    class_objects = {}
    untraced = set()
    class_contexts = {}
    attribute_reads = {}
    attribute_init = set()
    task_counts = {}
    task_contexts = set()
    class_seen = {}
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
        source=[str(root / "sqlglot"), str(root / "tests")],
        data_file=str(raw) if raw else None,
    )
    cov.start()

    from instrument import install, attribute_owner

    def record_class(obj):
        context = active[0]
        if not context:
            return
        cls = obj if isinstance(obj, type) else type(obj)
        seen = class_seen.setdefault(context, set())
        cls_id = id(cls)
        if cls_id in seen:
            return
        seen.add(cls_id)
        if cls_id not in class_keys:
            class_objects[cls_id] = cls  # Keep identity cache keys alive.
            class_keys[cls_id] = tuple(
                f"{parent.__module__}.{parent.__qualname__}"
                for parent in cls.__mro__
                if parent.__module__.startswith("sqlglot")
            )
        class_reads.setdefault(context, set()).update(class_keys[cls_id])

    def record_attribute(obj, name):
        key = attribute_owner(obj, name)
        if key:
            context = active[0]
            if context:
                attribute_reads.setdefault(key, set()).add(context)
            if not context or context.startswith("@class:"):
                attribute_init.add(key)

    install(root, record_class, record_attribute)

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
                previous = active[0]
                root_context = previous or "@collection"
                ordinal = task_counts.get(root_context, 0)
                task_counts[root_context] = ordinal + 1
                tag = f"{root_context}@@task:{ordinal}"
                task_contexts.add(tag)
                active[0] = tag
                cov.switch_context(tag)
                try:
                    fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                    value = fn(*args, **kwargs)
                    future.set_result(pickle.loads(pickle.dumps(value)))
                finally:
                    active[0] = previous
                    cov.switch_context(previous or "")
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
    if raw:
        cov.save()
    data = cov.get_data()
    ids = sorted(set(collected))
    index = {name: i for i, name in enumerate(ids)}
    pool = [0]
    pool_index = {0: 0}

    def intern(contexts):
        bits = 0
        for context in contexts:
            context = context.split("@@task:", 1)[0]
            for name in class_contexts.get(context, [context]):
                if name in index:
                    bits |= 1 << index[name]
        if bits not in pool_index:
            pool_index[bits] = len(pool)
            pool.append(bits)
        return pool_index[bits]

    task_ids = sorted(
        task_contexts,
        key=lambda tag: (tag.split("@@task:")[0], int(tag.split("@@task:")[1])),
    )
    task_index = {name: i for i, name in enumerate(task_ids)}
    task_pool = [0]
    task_pool_index = {0: 0}

    def intern_tasks(contexts):
        bits = 0
        for context in contexts:
            if context in task_index:
                bits |= 1 << task_index[context]
        if bits not in task_pool_index:
            task_pool_index[bits] = len(task_pool)
            task_pool.append(bits)
        return task_pool_index[bits]

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
        task_lines = {
            str(line): intern_tasks(contexts)
            for line, contexts in data.contexts_by_lineno(name).items()
        }
        files[rel] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "lines": lines,
            "tasks": {line: value for line, value in task_lines.items() if value},
            "outside": {
                str(line): intern(
                    [context for context in contexts if "@@task:" not in context]
                )
                for line, contexts in data.contexts_by_lineno(name).items()
                if any(context and "@@task:" not in context for context in contexts)
            },
            "init": [
                line
                for line, contexts in data.contexts_by_lineno(name).items()
                if "" in contexts
            ],
        }
    fixture_reads = {name: intern(contexts) for name, contexts in sorted(reads.items())}
    uses = {}
    for context, keys in class_reads.items():
        for key in keys:
            uses.setdefault(key, set()).add(context)
    classes = {key: intern(contexts) for key, contexts in sorted(uses.items())}
    attributes = {
        key: intern(contexts) for key, contexts in sorted(attribute_reads.items())
    }
    attribute_tasks = {
        key: intern_tasks(contexts) for key, contexts in sorted(attribute_reads.items())
    }
    class_tasks = {
        key: intern_tasks(contexts) for key, contexts in sorted(uses.items())
    }
    always = intern(untraced | {name for name in ids if outcomes.get(name) != "passed"})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema": 2,
                "outside_complete": True,
                "baseline": BASELINE,
                "tests": ids,
                "pool": [format(bits, "x") for bits in pool],
                "files": files,
                "fixtures": fixture_reads,
                "classes": classes,
                "always": always,
                "attributes": attributes,
                "attribute_tasks": attribute_tasks,
                "attribute_init": sorted(attribute_init),
                "class_tasks": class_tasks,
                "attributes_outside": {
                    key: intern(
                        [context for context in contexts if "@@task:" not in context]
                    )
                    for key, contexts in sorted(attribute_reads.items())
                },
                "classes_outside": {
                    key: intern(
                        [context for context in contexts if "@@task:" not in context]
                    )
                    for key, contexts in sorted(uses.items())
                },
                "task_ids": [
                    {
                        "test": tag.split("@@task:")[0],
                        "ordinal": int(tag.split("@@task:")[1]),
                    }
                    for tag in task_ids
                ],
                "task_pool": [format(bits, "x") for bits in task_pool],
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
