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
from pathlib import Path, PurePosixPath
import re
import sys

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
        for line in body:
            kind, value = line[0], line[1:]
            if kind in " -":
                if cursor >= len(original) or original[cursor] != value:
                    raise ValueError("patch does not match the pinned baseline")
                if kind == "-":
                    deleted.add(cursor + 1)
                else:
                    result.append(value)
                cursor += 1
            else:
                result.append(value)
                # Insertions affect the control flow on both sides of this gap.
                insertions.add(cursor)
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
    def __init__(self, tests: list[dict], map_path: Path, source: Path):
        self.tests = tests
        self.source = source
        self.all = (1 << len(tests)) - 1
        self.map = json.loads(map_path.read_text())
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
            self.imports = reverse
        visited = {name}
        todo = [name]
        while todo:
            for dependent in self.imports.get(todo.pop(), ()):
                if dependent not in visited:
                    visited.add(dependent)
                    todo.append(dependent)
        bits = union(self.file_mask(path) for path in visited)
        for i, test in enumerate(self.tests):
            if test["file"] in visited:
                bits |= 1 << i
        return bits

    def python_change(self, change: FileChange) -> int:
        name = change.old
        old = self.read(name)
        new, deleted, inserted = apply_change(old, change)
        before = self.tree(name)
        try:
            after = ast.parse(new.encode("utf-8"), type_comments=True)
        except (SyntaxError, ValueError):
            return self.all  # Import failure can break collection of many modules.
        if ast.dump(before, include_attributes=False) == ast.dump(
            after, include_attributes=False
        ):
            self.reasons.append(f"{name}: unchanged Python syntax tree")
            return 0

        executable = self.lines(name)
        functions = [
            node
            for node in ast.walk(before)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        statements = [node for node in ast.walk(before) if isinstance(node, ast.stmt)]
        changed = set(deleted)
        for gap in inserted:
            changed.update((max(1, gap), min(len(old.splitlines()), gap + 1)))
        bits = 0
        import_time_lines = []
        for line in changed:
            containing = [
                node for node in functions if node.lineno <= line <= node.end_lineno
            ]
            # Default values, decorators and signatures run when a function is
            # defined, not when its body is called.
            body_owners = [
                node for node in containing if node.body and line >= node.body[0].lineno
            ]
            if not body_owners:
                import_time_lines.append(line)
                continue
            owner = min(body_owners, key=lambda node: node.end_lineno - node.lineno)
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
                class_bits = self.pool[self.map.get("classes", {}).get(key, 0)]
                class_bits |= self.range_mask(name, target.lineno, target.end_lineno)
                bits |= class_bits or self.dependents(name)
            else:
                bits |= self.dependents(name)
        if not bits:
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
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    selector = Selector(tests, args.map, args.source)
    total = 0
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected = selector.select(patch.read_text(encoding="utf-8"))
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
