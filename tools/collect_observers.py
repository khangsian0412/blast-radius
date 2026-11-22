"""Measure source/code observers on the unmodified baseline, in pytest scopes.

This is separate from execution coverage: a test can inspect a cached function
without ever calling it. CPython audits access to function code and frame code.
Unknown child programs and shared fixture contexts are preserved explicitly.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    import pytest

    active = [None]
    scopes, class_tags, module_tags = {}, {}, {}
    metadata, initialization, source_reads = {}, set(), {}
    children = set()
    failures = []
    prefix = str(root) + "/"

    def record(table, name):
        if active[0] is not None:
            table.setdefault(name, set()).add(active[0])

    observing = [False]

    def audit(event, values):
        if observing[0]:
            return
        observing[0] = True
        try:
            observe(event, values)
        finally:
            observing[0] = False

    def observe(event, values):
        if event == "object.__getattr__":
            obj, field = values
            name = None
            if field == "__code__" and isinstance(obj, types.FunctionType):
                if obj.__module__ and obj.__module__.startswith("sqlglot"):
                    name = obj.__code__.co_filename
            elif field == "f_code" and isinstance(obj, types.FrameType):
                name = obj.f_code.co_filename
            elif field == "tb_frame" and isinstance(obj, types.TracebackType):
                name = obj.tb_frame.f_code.co_filename
            if name and name.startswith(prefix + "sqlglot/"):
                if active[0] is None:
                    initialization.add(name[len(prefix):])
                else:
                    record(metadata, name[len(prefix):])
        elif event == "open" and active[0] is not None and isinstance(values[0], str):
            name = os.path.abspath(values[0])
            if name.startswith(prefix) and name.endswith(".py"):
                record(source_reads, name[len(prefix):])
        elif event == "subprocess.Popen" and active[0] is not None:
            children.add(active[0])

    sys.addaudithook(audit)

    @contextmanager
    def context(tag):
        previous = active[0]
        active[0] = tag
        try:
            yield
        finally:
            active[0] = previous

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            scopes["@session"] = {item.nodeid for item in items}
            for item in items:
                scopes[item.nodeid] = {item.nodeid}
                file = item.nodeid.split("::")[0]
                tag = module_tags.setdefault(file, "@module:" + file)
                scopes.setdefault(tag, set()).add(item.nodeid)
                if getattr(item, "cls", None) is not None:
                    tag = class_tags.setdefault(item.cls, "@class:" + item.nodeid.rsplit("::", 1)[0])
                    scopes.setdefault(tag, set()).add(item.nodeid)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            with context(item.nodeid):
                yield

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if fixturedef.scope == "function":
                yield
                return
            if fixturedef.scope == "class" and request.cls in class_tags:
                tag = class_tags[request.cls]
            elif fixturedef.scope == "module":
                tag = module_tags[request.node.nodeid]
            else:
                tag = "@session"
            first = len(fixturedef._finalizers)
            with context(tag):
                yield

            def wrap(finalizer):
                def finish():
                    with context(tag):
                        return finalizer()
                return finish
            fixturedef._finalizers[first:] = [wrap(f) for f in fixturedef._finalizers[first:]]

        def pytest_runtest_logreport(self, report):
            if report.failed:
                failures.append(report.nodeid)

    result = pytest.main(["tests", "-q", "--disable-warnings", "-o", "addopts=", "--tb=short", "-p", "no:cacheprovider"], plugins=[Plugin()])
    if result or failures:
        raise SystemExit(f"Observer baseline failed: {result}, {failures}")

    def members(contexts):
        return sorted({node for tag in contexts for node in scopes.get(tag, ())})

    payload = {
        "metadata_observers": {name: members(tags) for name, tags in sorted(metadata.items())},
        "metadata_initialization": sorted(initialization),
        "observer_source_reads": {name: members(tags) for name, tags in sorted(source_reads.items())},
        "observer_children": members(children),
        "observer_collection": {"python": sys.version.split()[0], "platform": sys.platform, "tests": len(scopes["@session"]), "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(json.dumps({"metadata_files": len(metadata), "metadata_initialization": sorted(initialization), "child_tests": len(payload["observer_children"])}))


if __name__ == "__main__":
    main()
