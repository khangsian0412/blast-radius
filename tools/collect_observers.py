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
import tempfile
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
    source_initialization = set()
    metadata_sites, initialization_sites = {}, set()
    children = set()
    expected_children, child_starts = {}, {}
    failures = []
    prefix = str(root) + "/"
    parent_pid = os.getpid()
    child_output = tempfile.TemporaryDirectory(prefix="blast-observers-")
    hook_dir = Path(child_output.name)
    (hook_dir / "sitecustomize.py").write_text(
        "from collect_observers import child_hook\nchild_hook()\n"
    )
    os.environ.update(
        BLAST_OBSERVER_ROOT=str(root), BLAST_OBSERVER_OUTPUT=str(hook_dir),
        PYTHONPATH=os.pathsep.join([str(hook_dir), str(Path(__file__).resolve().parent), str(root), os.environ.get("PYTHONPATH", "")]),
    )
    child_seen = set()

    def record(table, name):
        if active[0] is not None:
            if os.getpid() == parent_pid:
                table.setdefault(name, set()).add(active[0])
            else:
                kind = "code" if table is metadata_sites else "metadata" if table is metadata else "source"
                entry = (kind, name, active[0])
                if entry not in child_seen:
                    child_seen.add(entry)
                    with (hook_dir / f"{os.getpid()}.jsonl").open("a") as stream:
                        stream.write(json.dumps(entry) + "\n")

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
            name, code = None, None
            if field == "__code__" and isinstance(obj, types.FunctionType):
                if obj.__module__ and obj.__module__.startswith("sqlglot"):
                    code = obj.__code__
            elif field == "f_code" and isinstance(obj, types.FrameType):
                code = obj.f_code
            elif field == "tb_frame" and isinstance(obj, types.TracebackType):
                code = obj.tb_frame.f_code
            if code is not None:
                name = code.co_filename
            if name and name.startswith(prefix + "sqlglot/"):
                site = f"{name[len(prefix):]}:{code.co_firstlineno}"
                if active[0] is None:
                    initialization.add(name[len(prefix):])
                    initialization_sites.add(site)
                else:
                    record(metadata, name[len(prefix):])
                    record(metadata_sites, site)
        elif event == "open" and isinstance(values[0], str):
            name = os.path.abspath(values[0])
            if name.startswith(prefix) and name.endswith(".py") and not import_read():
                if active[0] is None:
                    source_initialization.add(name[len(prefix):])
                else:
                    record(source_reads, name[len(prefix):])
        elif event == "subprocess.Popen" and active[0] is not None:
            children.add(active[0])
            expected_children[active[0]] = expected_children.get(active[0], 0) + 1

    sys.addaudithook(audit)

    @contextmanager
    def context(tag):
        previous = active[0]
        active[0] = tag
        os.environ["BLAST_OBSERVER_CONTEXT"] = tag
        try:
            yield
        finally:
            active[0] = previous
            os.environ["BLAST_OBSERVER_CONTEXT"] = previous or ""

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
    for file in hook_dir.glob("*.jsonl"):
        for line in file.read_text().splitlines():
            kind, name, tag = json.loads(line)
            if tag not in scopes:
                raise SystemExit("Child observer lost its pytest scope")
            if kind == "start":
                child_starts.setdefault(tag, set()).add(name)
                continue
            table = metadata_sites if kind == "code" else metadata if kind == "metadata" else source_reads
            table.setdefault(name, set()).add(tag)
            if kind == "source" and "::" in tag:
                shared = "@class:" + tag.rsplit("::", 1)[0]
                if shared in scopes:
                    table[name].add(shared)

    def members(contexts):
        return sorted({node for tag in contexts for node in scopes.get(tag, ())})

    payload = {
        "metadata_observers": {name: members(tags) for name, tags in sorted(metadata.items())},
        "metadata_initialization": sorted(initialization),
        "metadata_sites": {name: members(tags) for name, tags in sorted(metadata_sites.items())},
        "metadata_initialization_sites": sorted(initialization_sites),
        "metadata_sites_complete": True,
        "observer_source_reads": {name: members(tags) for name, tags in sorted(source_reads.items())},
        "source_initialization": sorted(source_initialization),
        "source_observation_complete": True,
        "observer_children": members(children),
        "verified_observer_children": members(tag for tag, expected in expected_children.items() if len(child_starts.get(tag, ())) == expected),
        "observer_collection": {"python": sys.version.split()[0], "platform": sys.platform, "tests": len(scopes["@session"]), "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    child_output.cleanup()
    print(json.dumps({"metadata_files": len(metadata), "metadata_initialization": sorted(initialization), "child_tests": len(payload["observer_children"])}))


def child_hook():
    """Observe fresh spawn interpreters without changing pool/task execution."""
    root = os.environ.get("BLAST_OBSERVER_ROOT", "") + "/"
    output = os.environ.get("BLAST_OBSERVER_OUTPUT")
    tag = os.environ.get("BLAST_OBSERVER_CONTEXT")
    if not output or not tag:
        return
    busy, seen = [False], set()

    def audit(event, values):
        if busy[0]:
            return
        busy[0] = True
        try:
            kind, name, code = None, None, None
            if event == "open" and isinstance(values[0], str):
                name = os.path.abspath(values[0])
                if name.startswith(root) and name.endswith(".py") and not import_read():
                    kind = "source"
            elif event == "object.__getattr__":
                obj, field = values
                if field == "__code__" and isinstance(obj, types.FunctionType):
                    code = obj.__code__
                elif field == "f_code" and isinstance(obj, types.FrameType):
                    code = obj.f_code
                elif field == "tb_frame" and isinstance(obj, types.TracebackType):
                    code = obj.tb_frame.f_code
                if code is not None:
                    name = code.co_filename
                if name and name.startswith(root + "sqlglot/"):
                    kind = "metadata"
            if kind:
                entry = (kind, name[len(root):], tag)
                if entry not in seen:
                    seen.add(entry)
                    with (Path(output) / f"{os.getpid()}.jsonl").open("a") as stream:
                        stream.write(json.dumps(entry) + "\n")
                if kind == "metadata":
                    entry = ("code", f"{name[len(root):]}:{code.co_firstlineno}", tag)
                    if entry not in seen:
                        seen.add(entry)
                        with (Path(output) / f"{os.getpid()}.jsonl").open("a") as stream:
                            stream.write(json.dumps(entry) + "\n")
        finally:
            busy[0] = False

    sys.addaudithook(audit)
    with (Path(output) / f"{os.getpid()}.jsonl").open("a") as stream:
        stream.write(json.dumps(("start", str(os.getpid()), tag)) + "\n")


def import_read():
    """Loading unchanged executable syntax is distinct from inspecting text."""
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        # get_source() also uses get_data(); it is a genuine source observer.
        # Only get_code() identifies loading executable module bytecode.
        if code.co_filename == "<frozen importlib._bootstrap_external>" and code.co_name == "get_code":
            return True
        frame = frame.f_back
    return False


if __name__ == "__main__":
    main()
