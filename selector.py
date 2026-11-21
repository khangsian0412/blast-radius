"""Coverage-based test selection for independent sqlglot patches.

Only baseline coverage is stored. Patch names and sample answers never participate
in selection. The runtime uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import json
import math
import operator
from pathlib import Path, PurePosixPath
import re
import sys
import types

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HERE = Path(__file__).resolve().parent
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileChange:
    old: str | None
    new: str | None
    hunks: list = field(default_factory=list)


def patch_path(text: str) -> str | None:
    # Git quotes unusual names using C-style escapes. Ordinary sqlglot paths are
    # unquoted; refuse ambiguous paths rather than silently analyze another file.
    text = text.split("\t", 1)[0]
    if text == "/dev/null":
        return None
    if text.startswith('"'):
        text = json.loads(text)
    if text.startswith(("a/", "b/")):
        text = text[2:]
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe diff path")
    return str(path)


def parse_patch(text: str) -> list[FileChange]:
    lines = text.splitlines(keepends=True)
    changes = []
    current = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            line.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
        ):
            current = FileChange(
                patch_path(line[4:].rstrip("\r\n")),
                patch_path(lines[i + 1][4:].rstrip("\r\n")),
            )
            changes.append(current)
            i += 2
            continue
        match = HUNK.match(line)
        if match and current is not None:
            old_start, old_count, new_start, new_count = (
                int(v) if v is not None else 1 for v in match.groups()
            )
            body = []
            old_seen = new_seen = 0
            i += 1
            while i < len(lines) and (old_seen < old_count or new_seen < new_count):
                part = lines[i]
                if part.startswith("\\ No newline"):
                    if body:
                        body[-1] = body[-1].rstrip("\r\n")
                    i += 1
                    continue
                if not part or part[0] not in " +-":
                    raise ValueError("invalid hunk")
                body.append(part)
                old_seen += part[0] in " -"
                new_seen += part[0] in " +"
                i += 1
            if i < len(lines) and lines[i].startswith("\\ No newline"):
                if body:
                    body[-1] = body[-1].rstrip("\r\n")
                i += 1
            if old_seen != old_count or new_seen != new_count:
                raise ValueError("hunk length mismatch")
            current.hunks.append((old_start, old_count, new_start, new_count, body))
            continue
        if line.startswith(
            ("GIT binary patch", "Binary files ", "rename from ", "rename to ")
        ):
            raise ValueError("non-text change requires conservative selection")
        i += 1
    if not changes:
        raise ValueError("no analyzable file changes")
    return changes


def apply_change(source: str, change: FileChange) -> tuple[str, set[int], set[int]]:
    original = source.splitlines(keepends=True)
    result = []
    deleted = set()
    insertions = set()
    cursor = 0
    for old_start, old_count, _, _, body in change.hunks:
        start = old_start - 1 if old_count else old_start
        if start < cursor or start > len(original):
            raise ValueError("overlapping or out-of-range hunk")
        result.extend(original[cursor:start])
        cursor = start
        pending_additions = set()
        pending_deletion = False
        for line in body:
            kind, value = line[0], line[1:]
            if kind == " ":
                if not pending_deletion:
                    insertions.update(pending_additions)
                pending_additions.clear()
                pending_deletion = False
            if kind in " -":
                if cursor >= len(original) or original[cursor] != value:
                    raise ValueError("patch does not match the pinned baseline")
                if kind == "-":
                    deleted.add(cursor + 1)
                    pending_deletion = True
                else:
                    result.append(value)
                cursor += 1
            else:
                result.append(value)
                # Only a pure insertion needs neighboring anchors. A replacement
                # is already represented by the old lines it deletes.
                pending_additions.add(cursor)
        if not pending_deletion:
            insertions.update(pending_additions)
    result.extend(original[cursor:])
    return "".join(result), deleted, insertions


def iter_bits(bits: int):
    while bits:
        bit = bits & -bits
        yield bit.bit_length() - 1
        bits ^= bit


def union(values):
    result = 0
    for value in values:
        result |= value
    return result


class Selector:
    def __init__(
        self, tests: list[dict], map_path: Path | None, source: Path, *, map_data=None
    ):
        self.tests = tests
        self.source = source
        self.all = (1 << len(tests)) - 1
        self.map = (
            map_data if map_data is not None else json.loads(map_path.read_text())
        )
        if self.map.get("baseline") != BASELINE:
            raise ValueError("coverage baseline mismatch")
        by_id = {test["id"]: i for i, test in enumerate(tests)}
        translate = [by_id.get(name) for name in self.map["tests"]]
        known = set(self.map["tests"])
        self.unknown = union(
            1 << i for i, test in enumerate(tests) if test["id"] not in known
        )
        # Coverage is indexed by node ID, never by the organizer's test number.
        self.pool = []
        if self.map["tests"] == [test["id"] for test in tests]:
            self.pool = [int(encoded, 16) for encoded in self.map["pool"]]
        else:
            for encoded in self.map["pool"]:
                translated = 0
                for index in iter_bits(int(encoded, 16)):
                    target = translate[index]
                    if target is not None:
                        translated |= 1 << target
                self.pool.append(translated)
        self.always = self.pool[self.map.get("always", 0)]
        self.file_cache = {}
        self.tree_cache = {}
        self.code_cache = {}
        self.line_cache = {}
        self.file_masks = {}
        self.imports = None
        self.reasons = []

    def lines(self, name: str) -> dict[int, int]:
        if name not in self.line_cache:
            record = self.map["files"].get(name, {})
            self.line_cache[name] = {
                int(line): self.pool[index]
                for line, index in record.get("lines", {}).items()
            }
        return self.line_cache[name]

    def file_mask(self, name: str) -> int:
        if name not in self.file_masks:
            self.file_masks[name] = union(self.lines(name).values())
        return self.file_masks[name]

    def read(self, name: str) -> str:
        if name not in self.file_cache:
            self.file_cache[name] = (self.source / name).read_text(encoding="utf-8")
        return self.file_cache[name]

    def tree(self, name: str):
        if name not in self.tree_cache:
            self.tree_cache[name] = ast.parse(self.read(name), type_comments=True)
        return self.tree_cache[name]

    def range_mask(self, name, start, end):
        return union(
            mask for line, mask in self.lines(name).items() if start <= line <= end
        )

    def dependents(self, name):
        """Fallback for import-time edits, using the baseline's import graph."""
        if self.imports is None:
            reverse = {}
            for path in sorted(
                list((self.source / "sqlglot").rglob("*.py"))
                + list((self.source / "tests").rglob("*.py"))
            ):
                rel = path.relative_to(self.source).as_posix()
                try:
                    tree = self.tree(rel)
                except (SyntaxError, UnicodeError):
                    continue
                parts = list(PurePosixPath(rel).with_suffix("").parts)
                package = parts[:-1]
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        base = (
                            package[: len(package) - node.level + 1]
                            if node.level
                            else []
                        )
                        module = ".".join(base + ([node.module] if node.module else []))
                        modules = [module] + [
                            module + "." + alias.name for alias in node.names
                        ]
                    for module in modules:
                        candidate = module.replace(".", "/")
                        for imported in (candidate + ".py", candidate + "/__init__.py"):
                            reverse.setdefault(imported, set()).add(rel)
            for dependency, consumers in self.map.get(
                "import_dependencies", {}
            ).items():
                reverse.setdefault(dependency, set()).update(consumers)
            self.imports = reverse
        visited = {name}
        todo = [name]
        while todo:
            for dependent in self.imports.get(todo.pop(), ()):
                if dependent not in visited:
                    visited.add(dependent)
                    todo.append(dependent)
        bits = union(self.file_mask(path) for path in visited)
        for path in visited:
            module = (
                path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            )
            bits |= union(
                self.pool[index]
                for key, index in self.map.get("classes", {}).items()
                if key.startswith(module + ".")
            )
        for i, test in enumerate(self.tests):
            if test["file"] in visited:
                bits |= 1 << i
        return bits

    def equivalent(self, name, before, after):
        """Prove identity/equality rewrites from baseline operand type domains.

        Stored facts contain only types observed on the unmodified baseline.
        No changed operation, patch, failing-test label, or mutant result is stored.
        Unknown/custom equality always prevents the proof.
        """
        if self.unknown:
            return ast.dump(before, include_attributes=False) == ast.dump(
                after, include_attributes=False
            )
        pending = [(before, after)]
        facts = self.map.get("types", {})
        profiles = self.map.get("type_profiles", {})
        primitive_profiles = self.map.get("value_profiles", {})
        operations = {
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
        }
        while pending:
            old, new = pending.pop()
            if (
                isinstance(old, ast.Compare)
                and isinstance(new, ast.Compare)
                and len(old.ops) == len(new.ops) == 1
            ):
                pair = (type(old.ops[0]), type(new.ops[0]))
                if old.lineno in self.map.get("subprocess_lines", {}).get(name, ()):
                    continue
                if pair in {
                    (ast.Is, ast.Eq),
                    (ast.Eq, ast.Is),
                    (ast.IsNot, ast.NotEq),
                    (ast.NotEq, ast.IsNot),
                }:
                    domains = profiles.get(
                        f"{name}:{old.lineno}:{old.col_offset}", [[], []]
                    )
                    positive = pair in {(ast.Is, ast.Eq), (ast.Eq, ast.Is)}
                    equality_key = (
                        "default_equality" if positive else "identity_equality"
                    )
                    enum_key = "enum_identity" if positive else "enum_inequality"

                    def compatible(a, b):
                        left, right = facts.get(a, {}), facts.get(b, {})
                        left_identity = left.get(equality_key) or (
                            a == "builtins.NoneType" and left.get("singleton")
                        )
                        right_identity = right.get(equality_key) or (
                            b == "builtins.NoneType" and right.get("singleton")
                        )
                        return bool(
                            (left_identity and right_identity)
                            or (
                                a == b
                                and (
                                    (left.get("singleton") and right.get("singleton"))
                                    or (left.get(enum_key) and right.get(enum_key))
                                )
                            )
                        )

                    if (
                        domains[0]
                        and domains[1]
                        and all(
                            compatible(a, b) for a in domains[0] for b in domains[1]
                        )
                    ):
                        new.ops = [type(old.ops[0])()]
                if (
                    pair[0] in operations
                    and pair[1] in operations
                    and pair[0] is not pair[1]
                ):
                    samples = primitive_profiles.get(
                        f"{name}:{old.lineno}:{old.col_offset}", ()
                    )
                    if len(samples) == 2 and all(
                        not part["unknown"] and part["values"] for part in samples
                    ):
                        try:
                            left_values, right_values = (
                                [ast.literal_eval(value) for value in part["values"]]
                                for part in samples
                            )
                            if all(
                                operations[pair[0]](a, b) == operations[pair[1]](a, b)
                                for a in left_values
                                for b in right_values
                            ):
                                new.ops = [type(old.ops[0])()]
                        except (ValueError, TypeError, OverflowError):
                            pass
            if (
                isinstance(old, ast.AST)
                and isinstance(new, ast.AST)
                and type(old) is type(new)
            ):
                for field in old._fields:
                    a, b = getattr(old, field, None), getattr(new, field, None)
                    if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                        pending.append((a, b))
                    elif (
                        isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
                    ):
                        pending.extend(zip(a, b))
        return ast.dump(before, include_attributes=False) == ast.dump(
            after, include_attributes=False
        )

    def dictionary_mask(self, origin, old_value, new_value, *, filename=None):
        if filename and getattr(old_value, "lineno", -1) in self.map.get(
            "subprocess_lines", {}
        ).get(filename, ()):
            return None
        record = self.map.get("dictionaries", {}).get(origin)
        if (
            self.unknown
            or not record
            or record["unsafe"]
            or not isinstance(old_value, ast.Dict)
            or not isinstance(new_value, ast.Dict)
        ):
            return None
        try:
            old = ast.literal_eval(old_value)
            new = ast.literal_eval(new_value)
        except (ValueError, TypeError, SyntaxError):
            return None
        if (
            list(old) != list(new)
            or set(old) != set(record["keys"])
            or len(old_value.keys) != len(old)
        ):
            return None
        changed = [
            key
            for key, a, b in zip(old, old_value.values, new_value.values)
            if ast.dump(a, include_attributes=False)
            != ast.dump(b, include_attributes=False)
        ]
        if not changed:
            return None
        reads = union(self.pool[record["reads"].get(key, 0)] for key in changed)
        # A value read during a lazy import can feed a cached configuration.
        # Only the stronger no-value-read proof bypasses ordinary dependencies.
        return None if reads else 0

    def binding_signatures(self, tree, name):
        compiled = compile(tree, name, "exec", dont_inherit=True)
        signatures = {}
        pending = [(compiled, ())]
        while pending:
            code, path = pending.pop()
            if code.co_name != "<module>":
                signatures[path] = (
                    code.co_qualname,
                    code.co_varnames,
                    code.co_cellvars,
                    code.co_freevars,
                    code.co_argcount,
                    code.co_posonlyargcount,
                    code.co_kwonlyargcount,
                    code.co_flags,
                )
            children = [
                value for value in code.co_consts if isinstance(value, types.CodeType)
            ]
            pending.extend(
                (value, path + ((value.co_name, ordinal),))
                for ordinal, value in enumerate(children)
            )
        return signatures

    def python_change(self, change: FileChange) -> int:
        name = change.old
        old = self.read(name)
        new, deleted, inserted = apply_change(old, change)
        before = self.tree(name)
        try:
            after = ast.parse(new.encode("utf-8"), type_comments=True)
        except (SyntaxError, ValueError):
            return self.all  # Import failure can break collection of many modules.
        if self.equivalent(name, before, after):
            self.reasons.append(f"{name}: equivalent Python syntax/typed operations")
            return 0
        if name not in self.code_cache:
            self.code_cache[name] = self.binding_signatures(before, name)
        if self.code_cache[name] != self.binding_signatures(after, name):
            # A new local binding or yield can change earlier code in the same
            # function, even when the edited statement was never reached.
            return self.all
        if inserted:
            # A new definition can run decorators/defaults during import or
            # override an inherited method without executing either old anchor.
            return self.all

        executable = self.lines(name)
        functions = [
            node
            for node in ast.walk(before)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        statements = [node for node in ast.walk(before) if isinstance(node, ast.stmt)]
        changed = set(deleted)
        for gap in inserted:
            changed.update((max(1, gap), min(len(old.splitlines()), gap + 1)))
        bits = 0
        import_time_lines = []
        proved_unread = False
        uncertain = False
        for line in changed:
            containing = [
                node for node in functions if node.lineno <= line <= node.end_lineno
            ]
            # Default values, decorators and signatures run when a function is
            # defined, not when its body is called.
            body_owners = [
                node
                for node in containing
                if (isinstance(node, ast.Lambda) and line >= node.body.lineno)
                or (
                    not isinstance(node, ast.Lambda)
                    and node.body
                    and line >= node.body[0].lineno
                )
            ]
            if not body_owners:
                import_time_lines.append(line)
                continue
            owner = min(body_owners, key=lambda node: node.end_lineno - node.lineno)
            if any(
                owner.lineno <= point <= owner.end_lineno
                for point in self.map["files"].get(name, {}).get("init", ())
            ):
                bits |= self.dependents(name)
            direct = executable.get(line, 0)
            if not direct:
                covering = [
                    node
                    for node in statements
                    if node.lineno <= line <= node.end_lineno
                    and node.lineno >= owner.lineno
                ]
                covering.sort(
                    key=lambda node: (node.end_lineno - node.lineno, -node.lineno)
                )
                # Tracers record the first line of multi-line expressions.
                for node in covering:
                    direct = executable.get(node.lineno, 0)
                    if direct:
                        break
            if not direct:
                direct = self.range_mask(name, owner.lineno, owner.end_lineno)
                if not direct and any(
                    owner.lineno <= entry <= owner.end_lineno
                    for entry in self.map["files"].get(name, {}).get("init", ())
                ):
                    return self.all
                if not direct:
                    uncertain = True
            # Changing a late operand can make a previously short-circuited
            # expression run. Cover the entry to every containing Boolean test.
            for node in ast.walk(owner):
                if (
                    isinstance(node, (ast.BoolOp, ast.Compare, ast.IfExp))
                    and node.lineno <= line <= node.end_lineno
                ):
                    direct |= executable.get(node.lineno, 0)
            bits |= direct
        classes = [node for node in ast.walk(before) if isinstance(node, ast.ClassDef)]
        module = name.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for line in import_time_lines:
            parents = sorted(
                (node for node in classes if node.lineno <= line <= node.end_lineno),
                key=lambda node: node.lineno,
            )
            if parents:
                key = module + "." + ".".join(node.name for node in parents)
                target = parents[-1]
                fields = []
                for statement in target.body:
                    if statement.lineno <= line <= statement.end_lineno:
                        if isinstance(statement, ast.Assign):
                            fields.extend(
                                item.id
                                for item in statement.targets
                                if isinstance(item, ast.Name)
                            )
                        elif isinstance(statement, ast.AnnAssign) and isinstance(
                            statement.target, ast.Name
                        ):
                            fields.append(statement.target.id)
                field_keys = [key + "." + field for field in fields]
                narrowed = None
                if len(field_keys) == 1:
                    new_class = after
                    for parent in parents:
                        new_class = next(
                            (
                                entry
                                for entry in new_class.body
                                if isinstance(entry, ast.ClassDef)
                                and entry.name == parent.name
                            ),
                            None,
                        )
                        if new_class is None:
                            break
                    old_statement = next(
                        (
                            entry
                            for entry in target.body
                            if isinstance(entry, (ast.Assign, ast.AnnAssign))
                            and entry.lineno <= line <= entry.end_lineno
                        ),
                        None,
                    )
                    if new_class is not None and old_statement is not None:
                        for entry in new_class.body:
                            names = (
                                [
                                    item.id
                                    for item in entry.targets
                                    if isinstance(item, ast.Name)
                                ]
                                if isinstance(entry, ast.Assign)
                                else [entry.target.id]
                                if isinstance(entry, ast.AnnAssign)
                                and isinstance(entry.target, ast.Name)
                                else []
                            )
                            if names == fields:
                                narrowed = self.dictionary_mask(
                                    field_keys[0],
                                    old_statement.value,
                                    entry.value,
                                    filename=name,
                                )
                                break
                if narrowed is not None:
                    bits |= narrowed
                    proved_unread = True
                    continue
                attributes = self.map.get("attributes", {})
                init_reads = self.map.get("attribute_init", ())
                if field_keys and all(
                    field in attributes and field not in init_reads
                    for field in field_keys
                ):
                    bits |= union(self.pool[attributes[field]] for field in field_keys)
                else:
                    class_bits = self.pool[self.map.get("classes", {}).get(key, 0)]
                    class_bits |= union(
                        self.pool[attributes[field]]
                        for field in field_keys
                        if field in attributes
                    )
                    class_bits |= self.range_mask(
                        name, target.lineno, target.end_lineno
                    )
                    if any(field in init_reads for field in field_keys):
                        class_bits |= self.dependents(name)
                    bits |= class_bits or self.dependents(name)
            else:
                bits |= self.dependents(name)
        if uncertain:
            bits |= self.dependents(name) or self.all
        if not bits and not proved_unread:
            # A coverage hole is uncertainty, not proof that nothing can break.
            bits = self.dependents(name) or self.all
        return bits

    def select(self, patch: str) -> int:
        self.reasons = []
        result = self.unknown
        try:
            changes = parse_patch(patch)
            for change in changes:
                name = change.old or change.new
                if change.old != change.new:
                    result |= self.all
                elif name in self.map["fixtures"]:
                    result |= self.pool[self.map["fixtures"][name]] or self.all
                elif name.endswith(".py") and name in self.map["files"]:
                    result |= self.python_change(change)
                else:
                    # New resources, packaging and collection configuration may
                    # change behavior beyond the baseline's measured call paths.
                    result |= self.all
        except (
            OSError,
            UnicodeError,
            ValueError,
            SyntaxError,
            IndexError,
            RecursionError,
        ) as error:
            self.reasons.append(f"conservative fallback: {type(error).__name__}")
            return self.all
        return result | (self.always if result else 0)


def schedule(tests: list[dict]) -> list[list[int]]:
    """Choose LPT's best worker count under the exact integer-ms cost model."""
    if not tests:
        return []
    jobs = sorted(
        ((math.floor(test["seconds"] * 1000 + 0.5), test["number"]) for test in tests),
        key=lambda job: (-job[0], job[1]),
    )
    best_cost = float("inf")
    best = None
    for count in range(1, min(8, len(jobs)) + 1):
        loads = [0] * count
        workers = [[] for _ in range(count)]
        for duration, number in jobs:
            target = min(range(count), key=lambda i: (loads[i], len(workers[i]), i))
            workers[target].append(number)
            loads[target] += duration
        cost = max(loads) + 2000 * sum(bool(worker) for worker in workers)
        if cost < best_cost:
            best_cost, best = cost, [worker for worker in workers if worker]
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--map", type=Path, default=HERE / "maps/coverage.json")
    parser.add_argument("--no-probe", action="store_true")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    selector = Selector(tests, args.map, args.source)
    from verification import Verifier

    verifier = None if args.no_probe else Verifier(selector)
    total = 0
    for patch in sorted((args.data / "commits").glob("*.patch")):
        content = patch.read_text(encoding="utf-8")
        selected = selector.select(content)
        if verifier:
            selected = verifier.check(content, selected)
            selector.reasons.extend(verifier.log)
        chosen = [tests[i] for i in iter_bits(selected)]
        workers = schedule(chosen)
        total += len(chosen)
        print(
            json.dumps(
                {"commit": patch.stem, "workers": workers}, separators=(",", ":")
            ),
            flush=True,
        )
        print(
            f"{patch.stem}: {len(chosen)} tests, {len(workers)} workers"
            + ("; " + "; ".join(selector.reasons) if selector.reasons else ""),
            file=sys.stderr,
        )
    print(f"selected {total} test executions", file=sys.stderr)


if __name__ == "__main__":
    main()
