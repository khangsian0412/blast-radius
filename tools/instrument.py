"""Baseline-only instrumentation for class usage and class-attribute reads.

The loader compiles the original files in memory, preserving source locations.
No patched program or known patch is involved in constructing these maps.
"""

from __future__ import annotations

import ast
import builtins
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys


def declaration_names(root: Path) -> set[str]:
    names = set()
    for path in (root / "sqlglot").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for statement in cls.body:
                if isinstance(statement, ast.Assign):
                    names.update(
                        n.id
                        for target in statement.targets
                        for n in ast.walk(target)
                        if isinstance(n, ast.Name)
                    )
                elif (
                    isinstance(statement, ast.AnnAssign)
                    and statement.value is not None
                    and isinstance(statement.target, ast.Name)
                ):
                    names.add(statement.target.id)
    return names


def attribute_owner(obj, name):
    """Resolve a class attribute's declaration without invoking its descriptor."""
    cls = obj if isinstance(obj, type) else type(obj)
    for parent in cls.__mro__:
        namespace = vars(parent)
        if name in namespace:
            module = namespace.get("__module__", "")
            if module.startswith("sqlglot"):
                return f"{module}.{parent.__qualname__}.{name}"
            break
    return None


def install(
    root: Path, record_class, record_attribute, record_type=None, record_value=None,
    *, sites=None, only_operands=False,
):
    names = declaration_names(root)
    import_stack = []

    def read(obj, name):
        value = getattr(obj, name)
        record_class(obj)
        record_attribute(obj, name)
        return value

    def dynamic(fn, obj, name, *default):
        value = fn(obj, name, *default)
        if fn is builtins.getattr and name in names:
            record_class(obj)
            record_attribute(obj, name)
        return value

    def operand(site, side, value):
        if record_type is not None:
            record_type(site, side, type(value))
        if record_value is not None:
            record_value(site, side, value)
        return value

    class Transform(ast.NodeTransformer):
        def __init__(self, filename):
            self.filename = filename

        def visit_Compare(self, node):
            node = self.generic_visit(node)
            if (
                (record_type is not None or record_value is not None)
                and (sites is None or f"{self.filename}:{node.lineno}:{node.col_offset}" in sites)
                and len(node.ops) == 1
                and isinstance(
                    node.ops[0],
                    (
                        ast.Is,
                        ast.IsNot,
                        ast.Eq,
                        ast.NotEq,
                        ast.Lt,
                        ast.LtE,
                        ast.Gt,
                        ast.GtE,
                    ),
                )
            ):
                site = f"{self.filename}:{node.lineno}:{node.col_offset}"

                def wrap(value, side):
                    return ast.copy_location(
                        ast.Call(
                            func=ast.Name(id="__blast_operand_type__", ctx=ast.Load()),
                            args=[ast.Constant(site), ast.Constant(side), value],
                            keywords=[],
                        ),
                        value,
                    )

                node.left = wrap(node.left, 0)
                node.comparators[0] = wrap(node.comparators[0], 1)
            return node

        def visit_Attribute(self, node):
            node = self.generic_visit(node)
            if only_operands:
                return node
            if isinstance(node.ctx, ast.Load) and node.attr in names:
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="__blast_attr_read__", ctx=ast.Load()),
                        args=[node.value, ast.Constant(node.attr)],
                        keywords=[],
                    ),
                    node,
                )
            return node

        def visit_Call(self, node):
            # Preserve locally shadowed getattr callables and argument evaluation.
            node = self.generic_visit(node)
            if only_operands:
                return node
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and 2 <= len(node.args) <= 3
                and not node.keywords
            ):
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="__blast_dynamic_read__", ctx=ast.Load()),
                        args=[node.func, *node.args],
                        keywords=[],
                    ),
                    node,
                )
            return node

        def visit_FunctionDef(self, node):
            node = self.generic_visit(node)
            if only_operands:
                return node
            arguments = [*node.args.posonlyargs, *node.args.args]
            if arguments and arguments[0].arg in {"self", "cls", "klass", "kls"}:
                position = (
                    1
                    if node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                    else 0
                )
                hook = ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="__blast_class_use__", ctx=ast.Load()),
                        args=[ast.Name(id=arguments[0].arg, ctx=ast.Load())],
                        keywords=[],
                    )
                )
                ast.copy_location(hook, node.body[min(position, len(node.body) - 1)])
                node.body.insert(position, hook)
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            source = Path(self.path).read_bytes()
            tree = Transform(Path(self.path).relative_to(root).as_posix()).visit(
                ast.parse(source, filename=self.path, type_comments=True)
            )
            return compile(
                ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True
            )

        def exec_module(self, module):
            module.__dict__.update(
                __blast_attr_read__=read,
                __blast_dynamic_read__=dynamic,
                __blast_class_use__=record_class,
                __blast_operand_type__=operand,
            )
            import_stack.append(module.__name__)
            try:
                super().exec_module(module)
            finally:
                import_stack.pop()

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
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

    finder = Finder()
    finder.import_stack = import_stack
    sys.meta_path.insert(0, finder)
    return finder
