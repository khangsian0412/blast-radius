"""Measure baseline dependencies of shared pytest fixtures, including xunit setup.

Pytest captures unittest setUpClass before collection_modifyitems. Wrapping the
class method there is too late. Instead trace fixture execution and finalizers,
and attribute each shared scope to every test that can consume its state.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    import coverage
    import pytest
    from instrument import install, attribute_owner

    active = [None]
    scopes, class_tags, module_tags = {}, {}, {}
    fixture_reads, source_reads, classes, attributes = {}, {}, {}, {}
    seen_classes, class_references = {}, {}
    cov = coverage.Coverage(source=[str(root / "sqlglot"), str(root / "tests")], data_file=None)
    prefix = str(root) + "/"

    def record(table, key):
        if active[0] is not None:
            table.setdefault(key, set()).add(active[0])

    def audit(event, values):
        if event != "open" or active[0] is None or not isinstance(values[0], str):
            return
        name = os.path.abspath(values[0])
        if name.startswith(prefix + "tests/fixtures/"):
            record(fixture_reads, name[len(prefix):])
        if name.startswith(prefix) and name.endswith(".py"):
            record(source_reads, name[len(prefix):])

    sys.addaudithook(audit)

    def record_class(obj):
        if active[0] is None:
            return
        cls = obj if isinstance(obj, type) else type(obj)
        seen = seen_classes.setdefault(active[0], set())
        if id(cls) in seen:
            return
        seen.add(id(cls))
        class_references[id(cls)] = cls
        for parent in cls.__mro__:
            if parent.__module__.startswith("sqlglot"):
                record(classes, f"{parent.__module__}.{parent.__qualname__}")

    def record_attribute(obj, name):
        if active[0] is not None:
            key = attribute_owner(obj, name)
            if key:
                record(attributes, key)

    install(root, record_class, record_attribute)

    @contextmanager
    def tracing(tag):
        previous = active[0]
        if previous is None:
            cov.start()
        active[0] = tag
        cov.switch_context(tag)
        try:
            yield
        finally:
            active[0] = previous
            if previous is None:
                cov.stop()
            else:
                cov.switch_context(previous)

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            scopes["@session"] = {item.nodeid for item in items}
            for item in items:
                file = item.nodeid.split("::")[0]
                tag = module_tags.setdefault(file, "@module:" + file)
                scopes.setdefault(tag, set()).add(item.nodeid)
                cls = getattr(item, "cls", None)
                if cls is not None:
                    tag = class_tags.setdefault(cls, "@class:" + item.nodeid.rsplit("::", 1)[0])
                    scopes.setdefault(tag, set()).add(item.nodeid)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            scope = fixturedef.scope
            if scope == "function":
                yield
                return
            if scope == "class" and request.cls in class_tags:
                tag = class_tags[request.cls]
            elif scope == "module":
                tag = module_tags[request.node.nodeid]
            elif scope == "package":
                tag = "@package:" + request.node.nodeid
                scopes[tag] = {node for node in scopes["@session"] if node.split("::")[0].startswith(request.node.nodeid.rstrip("/") + "/")}
                if not scopes[tag]:
                    tag = "@session"
            else:
                tag = "@session"
            first = len(fixturedef._finalizers)
            with tracing(tag):
                yield

            def wrap(finalizer):
                def finish():
                    with tracing(tag):
                        return finalizer()
                return finish

            # Generator fixture teardown runs after pytest_fixture_setup returns.
            # Preserve the same dependency scope when its finalizers execute.
            fixturedef._finalizers[first:] = [wrap(f) for f in fixturedef._finalizers[first:]]

    result = pytest.main(["tests", "-q", "--disable-warnings", "-o", "addopts=", "--tb=short", "-p", "no:cacheprovider"], plugins=[Plugin()])
    if result:
        raise SystemExit(f"Shared-dependency baseline failed: {result}")

    def members(contexts):
        return sorted({node for context in contexts for node in scopes.get(context, ())})

    lines = {}
    data = cov.get_data()
    for file in data.measured_files():
        if not file.startswith(prefix):
            continue
        record_lines = {str(line): members(contexts) for line, contexts in data.contexts_by_lineno(file).items()}
        lines[file[len(prefix):]] = {line: nodes for line, nodes in record_lines.items() if nodes}
    payload = {"shared_lines": lines}
    for key, table in [("shared_fixtures", fixture_reads), ("shared_source_reads", source_reads), ("shared_classes", classes), ("shared_attributes", attributes)]:
        payload[key] = {name: members(contexts) for name, contexts in sorted(table.items())}
    payload["shared_collection"] = {"python": sys.version.split()[0], "platform": sys.platform, "tests": len(scopes["@session"]), "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"Recorded shared dependencies: {len(lines)} files, {len(fixture_reads)} fixtures")


if __name__ == "__main__":
    main()
