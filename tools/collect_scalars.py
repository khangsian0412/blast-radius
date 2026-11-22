"""Measure scalar settings through real attribute access, including reflection.

Custom SQLGlot metaclasses and their instances retain their original getters.
Namespace reflection is conservatively attributed to every potentially read
setting. Only metaclasses that forward raw class namespaces unchanged to type
can certify runtime-only settings; other cases remain broad.
"""

import ast
import builtins
import concurrent.futures
from contextlib import contextmanager
import functools
import dis
import hashlib
import inspect
import json
import os
import pickle
from pathlib import Path
import sys
import types


def main():
    root, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    import pytest
    active = [None]
    busy = [False]
    scopes, classes, modules = {}, {}, {}
    reads, initialized, certified = {}, set(), set()
    hooks, references, trees, namespace_nodes, instructions = {}, [], {}, {}, {}
    original_build = builtins.__build_class__
    original_static = inspect.getattr_static

    def namespace(cls):
        return type.__getattribute__(cls, "__dict__")

    def scalar(value):
        return type(value) in (bool, int, str, float, type(None))

    def key(cls, name):
        if not isinstance(name, str) or not name.isupper():
            return None
        for parent in type.__getattribute__(cls, "__mro__"):
            data = namespace(parent)
            if name in data:
                module = data.get("__module__", "")
                return f"{module}.{type.__getattribute__(parent, '__qualname__')}.{name}" if module.startswith("sqlglot.") and scalar(data[name]) else None
        return None

    def record(cls, name):
        origin = key(cls, name)
        if origin:
            if active[0] is None:
                initialized.add(origin)
            else:
                reads.setdefault(origin, set()).add(active[0])

    def tree(file):
        if file not in trees:
            trees[file] = ast.parse(Path(file).read_bytes())
        return trees[file]

    def namespace_keys(frame):
        # A literal lookup reads only that value. Ambiguous/opaque namespace
        # access keeps every scalar, including cached namespace consumers.
        file = frame.f_code.co_filename
        if not file.startswith(str(root) + "/") or not file.endswith(".py"):
            return None
        code = frame.f_code
        if code not in instructions:
            instructions[code] = list(dis.get_instructions(code))
        instruction = next((item for item in reversed(instructions[code]) if item.offset <= frame.f_lasti), None)
        if instruction is None or not instruction.opname.startswith("LOAD_ATTR") or instruction.argval != "__dict__":
            return None
        if file not in namespace_nodes:
            parsed = tree(file)
            parents = {id(child): node for node in ast.walk(parsed) for child in ast.iter_child_nodes(node)}
            namespace_nodes[file] = {(n.lineno, n.end_lineno, n.col_offset, n.end_col_offset): (n, parents) for n in ast.walk(parsed) if isinstance(n, ast.Attribute) and n.attr == "__dict__"}
        position = instruction.positions
        found = namespace_nodes[file].get((position.lineno, position.end_lineno, position.col_offset, position.end_col_offset))
        if found is None:
            return None
        node, parents = found
        parent = parents.get(id(node))
        if isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents.get(id(parent))
            if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                return {call.args[0].value}
        if isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Constant) and isinstance(parent.slice.value, str):
            return {parent.slice.value}
        return None

    def trace_meta(meta):
        if meta in hooks:
            return
        original = type.__getattribute__(meta, "__getattribute__")
        @functools.wraps(original)
        def get(cls, name):
            if busy[0]:
                return original(cls, name)
            busy[0] = True
            try:
                if name == "__dict__":
                    names = namespace_keys(sys._getframe(1))
                    for field in namespace(cls) if names is None else names:
                        record(cls, field)
                else:
                    record(cls, name)
                return original(cls, name)
            finally:
                busy[0] = False
        type.__setattr__(meta, "__getattribute__", get)
        hooks[meta] = get

    def namespace_forwarded(meta):
        for parent in type.__getattribute__(meta, "__mro__"):
            if parent in (type, object):
                continue
            data = namespace(parent)
            if "__prepare__" in data or "__init__" in data:
                return False
            function = data.get("__new__")
            if function is None:
                continue
            function = function.__func__ if isinstance(function, staticmethod) else function
            if not isinstance(function, types.FunctionType):
                return False
            file, line = function.__code__.co_filename, function.__code__.co_firstlineno
            if not file.startswith(str(root) + "/sqlglot/"):
                return False
            definition = next((n for n in ast.walk(tree(file)) if isinstance(n, ast.FunctionDef) and n.lineno == line), None)
            if definition is None or len(definition.args.args) != 4:
                return False
            names = [arg.arg for arg in definition.args.args]
            parents = {id(child): node for node in ast.walk(definition) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(definition):
                if isinstance(node, ast.Name) and node.id == names[3] and isinstance(node.ctx, ast.Load):
                    call = parents.get(id(node))
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "__new__" and [arg.id if isinstance(arg, ast.Name) else None for arg in call.args] == names and isinstance(call.func.value, ast.Call) and isinstance(call.func.value.func, ast.Name) and call.func.value.func.id == "super" and not call.func.value.args and not call.keywords):
                        return False
        return True

    def build(body, name, *bases, **kwargs):
        cls = original_build(body, name, *bases, **kwargs)
        if not body.__module__.startswith("sqlglot."):
            return cls
        data = namespace(cls)
        own = [name for name, value in data.items() if name.isupper() and scalar(value)]
        meta = type(cls)
        if (not own and meta not in hooks) or meta is type or not namespace(meta).get("__module__", "").startswith("sqlglot."):
            return cls
        first = meta not in hooks
        trace_meta(meta)
        getter = type.__getattribute__(cls, "__getattribute__")
        if not getattr(getter, "__blast_scalar_getter__", False):
            @functools.wraps(getter)
            def get(obj, name):
                if not busy[0] and isinstance(name, str) and name.isupper():
                    busy[0] = True
                    try:
                        record(type(obj), name)
                    finally:
                        busy[0] = False
                return getter(obj, name)
            get.__blast_scalar_getter__ = True
            type.__setattr__(cls, "__getattribute__", get)
        references.append(cls)
        if namespace_forwarded(meta):
            for field in own:
                origin = key(cls, field)
                if origin is None:
                    continue
                certified.add(origin)
                if first:
                    initialized.add(origin)
        return cls

    def get_static(obj, name, *default):
        if not busy[0]:
            busy[0] = True
            try:
                record(obj if isinstance(obj, type) else type(obj), name)
            finally:
                busy[0] = False
        return original_static(obj, name, *default)

    builtins.__build_class__ = build
    inspect.getattr_static = get_static

    class Pool:
        def __init__(self, *a, initializer=None, initargs=(), **kw):
            self.initializer, self.initargs, self.ready = initializer, initargs, False
        def submit(self, fn, /, *a, **kw):
            future = concurrent.futures.Future()
            try:
                if not self.ready:
                    self.ready = True
                    if self.initializer:
                        self.initializer(*self.initargs)
                fn, a, kw = pickle.loads(pickle.dumps((fn, a, kw)))
                future.set_result(pickle.loads(pickle.dumps(fn(*a, **kw))))
            except BaseException as error:
                future.set_exception(error)
            return future
        def shutdown(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
    concurrent.futures.ProcessPoolExecutor = Pool

    @contextmanager
    def context(tag):
        previous, active[0] = active[0], tag
        try:
            yield
        finally:
            active[0] = previous

    failures = []
    class Plugin:
        def pytest_collection_modifyitems(self, items):
            scopes["@session"] = {item.nodeid for item in items}
            for item in items:
                scopes[item.nodeid] = {item.nodeid}
                file = item.nodeid.split("::")[0]
                tag = modules.setdefault(file, "@module:" + file)
                scopes.setdefault(tag, set()).add(item.nodeid)
                if getattr(item, "cls", None):
                    tag = classes.setdefault(item.cls, "@class:" + item.nodeid.rsplit("::", 1)[0])
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
            tag = classes.get(request.cls, "@session") if fixturedef.scope == "class" else modules[request.node.nodeid] if fixturedef.scope == "module" else "@session"
            first = len(fixturedef._finalizers)
            with context(tag):
                yield
            def wrap(finalizer):
                def run():
                    with context(tag):
                        return finalizer()
                return run
            fixturedef._finalizers[first:] = [wrap(f) for f in fixturedef._finalizers[first:]]
        def pytest_runtest_logreport(self, report):
            if report.failed:
                failures.append(report.nodeid)

    result = pytest.main(["tests", "-q", "--tb=short", "--disable-warnings", "-o", "addopts=", "-p", "no:cacheprovider"], plugins=[Plugin()])
    if result or failures or any(type.__getattribute__(meta, "__getattribute__") is not hook for meta, hook in hooks.items()):
        raise SystemExit(f"Scalar baseline incomplete: {result}, {failures}")
    builtins.__build_class__, inspect.getattr_static = original_build, original_static
    payload = {
        "scalar_reads": {key: sorted({node for tag in tags for node in scopes.get(tag, ())}) for key, tags in reads.items()},
        "scalar_initialization": sorted(initialized),
        "scalar_fields": sorted(certified),
        "scalar_collection": {"python": sys.version.split()[0], "platform": sys.platform, "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
