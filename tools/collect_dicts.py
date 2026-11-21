"""Measure reads of literal class configuration dictionaries on the baseline.

Only existing string keys with immutable literal values are eligible. Mutation,
import-time value reads, and escapes into external callables prevent narrowing.
"""

from __future__ import annotations
import ast
import concurrent.futures
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import pickle
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    active = [None]
    records = {}

    def observe(origin, key=None, escape=False):
        record = records[origin]
        if escape or not active[0]:
            record["unsafe"] = True
        if active[0]:
            keys = (
                record["keys"]
                if key is None
                else [key]
                if isinstance(key, str)
                else record["keys"]
            )
            for name in keys:
                record["reads"].setdefault(name, set()).add(active[0])

    class Config(dict):
        def __init__(self, origin, value):
            dict.__init__(self, value)
            self.origin = origin

        def __getitem__(self, key):
            observe(self.origin, key)
            return dict.__getitem__(self, key)

        def get(self, key, *default):
            observe(self.origin, key)
            return dict.get(self, key, *default)

        def __iter__(self):
            observe(self.origin)
            return dict.__iter__(self)

        def keys(self):
            observe(self.origin)
            return dict.keys(self)

        def items(self):
            observe(self.origin)
            return dict.items(self)

        def values(self):
            observe(self.origin)
            return dict.values(self)

        def copy(self):
            observe(self.origin)
            return dict.copy(self)

        def __repr__(self):
            observe(self.origin)
            return dict.__repr__(self)

        __str__ = __repr__

        def __eq__(self, other):
            observe(self.origin)
            return dict.__eq__(self, other)

        def __ne__(self, other):
            observe(self.origin)
            return dict.__ne__(self, other)

        def __or__(self, other):
            observe(self.origin)
            return dict.__or__(self, other)

        def __ror__(self, other):
            observe(self.origin)
            return dict.__ror__(self, other)

        def __reduce_ex__(self, protocol):
            observe(self.origin, escape=True)
            return (dict, (dict(self),))

        def __setitem__(self, key, value):
            observe(self.origin, escape=True)
            return dict.__setitem__(self, key, value)

        def __delitem__(self, key):
            observe(self.origin, escape=True)
            return dict.__delitem__(self, key)

        def clear(self):
            observe(self.origin, escape=True)
            return dict.clear(self)

        def pop(self, *args):
            observe(self.origin, escape=True)
            return dict.pop(self, *args)

        def popitem(self):
            observe(self.origin, escape=True)
            return dict.popitem(self)

        def setdefault(self, *args):
            observe(self.origin, escape=True)
            return dict.setdefault(self, *args)

        def update(self, *args, **kwargs):
            observe(self.origin, escape=True)
            return dict.update(self, *args, **kwargs)

        def __ior__(self, other):
            observe(self.origin, escape=True)
            return dict.__ior__(self, other)

    def config(origin, value):
        records.setdefault(
            origin, {"keys": sorted(value), "reads": {}, "unsafe": False}
        )
        return Config(origin, value)

    def external(value, seen):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(value, Config):
            observe(value.origin, escape=True)
        elif type(value) in (list, tuple, set, frozenset):
            for item in value:
                external(item, seen)
        elif type(value) is dict:
            for item in value.values():
                external(item, seen)

    def call(fn, *args, **kwargs):
        module = getattr(fn, "__module__", "")
        owner = getattr(fn, "__self__", None)
        if (
            not isinstance(owner, Config)
            and fn is not len
            and fn is not bool
            and not (isinstance(module, str) and module.startswith("sqlglot"))
        ):
            seen = set()
            for value in args:
                external(value, seen)
            for value in kwargs.values():
                external(value, seen)
        return fn(*args, **kwargs)

    def comparison(value):
        if isinstance(value, Config):
            observe(value.origin)
        return value

    class Transform(ast.NodeTransformer):
        def __init__(self, module):
            self.module = module
            self.classes = []
            self.scope = []

        def visit_ClassDef(self, node):
            self.classes.append(node.name)
            self.scope.append("class")
            node = self.generic_visit(node)
            self.classes.pop()
            self.scope.pop()
            return node

        def visit_FunctionDef(self, node):
            self.scope.append("function")
            node = self.generic_visit(node)
            self.scope.pop()
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            original = node.value
            node = self.generic_visit(node)
            if (
                self.scope
                and self.scope[-1] == "class"
                and self.module.startswith("sqlglot.")
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(original, ast.Dict)
            ):
                self.wrap_dict(node, original, node.targets[0].id)
            return node

        def visit_AnnAssign(self, node):
            original = node.value
            node = self.generic_visit(node)
            if (
                self.scope
                and self.scope[-1] == "class"
                and self.module.startswith("sqlglot.")
                and isinstance(node.target, ast.Name)
                and isinstance(original, ast.Dict)
            ):
                self.wrap_dict(node, original, node.target.id)
            return node

        def wrap_dict(self, node, value, name):
            if not value.keys or not all(
                isinstance(key, ast.Constant) and isinstance(key.value, str)
                for key in value.keys
            ):
                return
            try:
                literals = ast.literal_eval(value)

                def immutable(item):
                    return (
                        type(item) in (bool, int, str, float, type(None))
                        or type(item) is tuple
                        and all(immutable(x) for x in item)
                    )

                if not all(immutable(item) for item in literals.values()):
                    return
            except (ValueError, TypeError, SyntaxError):
                return
            origin = ".".join([self.module, *self.classes, name])
            node.value = ast.copy_location(
                ast.Call(
                    func=ast.Name(id="__blast_config__", ctx=ast.Load()),
                    args=[ast.Constant(origin), node.value],
                    keywords=[],
                ),
                node.value,
            )

        def visit_Call(self, node):
            node = self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in {
                "super",
                "locals",
                "globals",
                "vars",
                "dir",
                "eval",
                "exec",
                "__import__",
            }:
                return node
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "_getframe",
                "currentframe",
            }:
                return node
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="__blast_call__", ctx=ast.Load()),
                    args=[node.func, *node.args],
                    keywords=node.keywords,
                ),
                node,
            )

        def visit_Compare(self, node):
            node = self.generic_visit(node)
            if any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                node.left = ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="__blast_compare__", ctx=ast.Load()),
                        args=[node.left],
                        keywords=[],
                    ),
                    node.left,
                )
                node.comparators = [
                    ast.copy_location(
                        ast.Call(
                            func=ast.Name(id="__blast_compare__", ctx=ast.Load()),
                            args=[item],
                            keywords=[],
                        ),
                        item,
                    )
                    for item in node.comparators
                ]
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = Transform(fullname).visit(
                ast.parse(Path(self.path).read_bytes(), filename=self.path)
            )
            return compile(
                ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True
            )

        def exec_module(self, module):
            module.__dict__.update(
                __blast_config__=config,
                __blast_call__=call,
                __blast_compare__=comparison,
            )
            super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not (
                fullname in {"sqlglot", "tests"}
                or fullname.startswith(("sqlglot.", "tests."))
            ):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if (
                spec
                and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
                and spec.origin
                and Path(spec.origin).is_relative_to(root)
            ):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    sys.meta_path.insert(0, Finder())

    class Pool:
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
                future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as error:
                future.set_exception(error)
            return future

        def shutdown(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    concurrent.futures.ProcessPoolExecutor = Pool
    import pytest

    classes = {}

    class Plugin:
        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            active[0] = item.nodeid
            yield
            active[0] = None

        def pytest_collection_modifyitems(self, items):
            cls_items = {}
            for item in items:
                if getattr(item, "cls", None):
                    cls_items.setdefault(item.cls, []).append(item.nodeid)
            for cls, members in cls_items.items():
                tag = "@class:" + members[0].rsplit("::", 1)[0]
                classes[tag] = set(members)
                for name in ("setUpClass", "tearDownClass"):
                    original = getattr(cls, name, None)
                    if original is None:
                        continue

                    def wrap(original, tag):
                        def run(_cls):
                            previous = active[0]
                            active[0] = tag
                            try:
                                return original()
                            finally:
                                active[0] = previous

                        return classmethod(run)

                    setattr(cls, name, wrap(original, tag))

    result = pytest.main(
        [
            "tests",
            "-q",
            "--tb=short",
            "--disable-warnings",
            "-o",
            "addopts=",
            "--maxfail=1",
        ],
        plugins=[Plugin()],
    )
    if result:
        raise SystemExit("Baseline dictionary instrumentation failed")
    # unittest class setup runs inside the first test's setup. Its dependencies
    # could reach every member, so expand reads to the whole class conservatively.
    for record in records.values():
        for names in record["reads"].values():
            for name in list(names):
                if name in classes:
                    names.remove(name)
                    names.update(classes[name])
        record["reads"] = {key: sorted(names) for key, names in record["reads"].items()}
    output.write_text(
        json.dumps({"dictionary_reads": records}, separators=(",", ":")) + "\n"
    )
    print(
        f"Collected {len(records)} literal dictionary origins, {sum(not x['unsafe'] for x in records.values())} eligible",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
