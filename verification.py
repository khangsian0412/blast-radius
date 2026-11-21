"""Budgeted runtime checks; uncertainty always retains tests."""

from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

from selector import Selector, apply_change, iter_bits, parse_patch, union


class Verifier:
    def __init__(self, selector, *, worker=None, cpu_budget=30.0):
        self.selector = selector
        self.tests = selector.tests
        self.worker = worker or Path(__file__).with_name("probe.py")
        self.deadline = time.monotonic() + 130
        self.cpu_budget = cpu_budget
        self.cpu_used = 0.0
        self.tasks = None
        self.groups = {}
        for i, item in enumerate(selector.map.get("task_ids", ())):
            self.groups.setdefault(item["test"], []).append((i, item["ordinal"]))
        self.log = []
        self.last_report = None

    def task_selector(self):
        if self.tasks is None:
            data = self.selector.map
            jobs = [
                {
                    "id": f"{item['test']}@@task:{item['ordinal']}",
                    "number": i,
                    "file": item["test"].split("::")[0],
                    "seconds": 0,
                }
                for i, item in enumerate(data.get("task_ids", ()))
            ]
            task_map = {
                **data,
                "tests": [item["id"] for item in jobs],
                "pool": data.get("task_pool", ["0"]),
                "files": {
                    name: {
                        "lines": record.get("tasks", {}),
                        "init": record.get("init", ()),
                    }
                    for name, record in data["files"].items()
                },
                "classes": data.get("class_tasks", {}),
                "attributes": data.get("attribute_tasks", {}),
                "fixtures": {},
                "always": 0,
            }
            self.tasks = Selector(jobs, None, self.selector.source, map_data=task_map)
        return self.tasks

    def outside_sensitive(self, changes):
        data = self.selector.map
        # Old maps lack this distinction: no task may be skipped with them.
        if not data.get("outside_complete"):
            return self.selector.all
        bits = 0
        for change in changes:
            name = change.old
            old = self.selector.read(name)
            _, removed, gaps = apply_change(old, change)
            lines = set(removed) | set(gaps) | {gap + 1 for gap in gaps}
            tree = self.selector.tree(name)
            functions = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            outside = data["files"].get(name, {}).get("outside", {})
            for line in lines:
                owners = [
                    node for node in functions if node.lineno <= line <= node.end_lineno
                ]
                if owners:
                    owner = min(owners, key=lambda node: node.end_lineno - node.lineno)
                    if any(
                        owner.body[0].lineno <= entry <= owner.end_lineno
                        for entry in data["files"].get(name, {}).get("init", ())
                    ):
                        return self.selector.all
                    bits |= union(
                        self.selector.pool[value]
                        for point, value in outside.items()
                        if owner.lineno <= int(point) <= owner.end_lineno
                    )
                else:
                    # Import-time state can feed any later worker task.
                    return self.selector.all
        return bits

    def check(self, patch, selected):
        self.log = []
        self.last_report = None
        if not selected:
            return selected
        try:
            changes = parse_patch(patch)
            if any(
                change.old != change.new
                or not change.old.startswith("sqlglot/")
                or not change.old.endswith(".py")
                for change in changes
            ):
                return selected
            overrides = {}
            collect = False
            for change in changes:
                new, removed, gaps = apply_change(
                    self.selector.read(change.old), change
                )
                ast.parse(new.encode("utf-8"))
                overrides[change.old] = new
                points = removed | gaps | {gap + 1 for gap in gaps}
                initial = self.selector.map["files"].get(change.old, {}).get("init", ())
                if gaps or points.intersection(initial):
                    collect = True
                functions = [
                    node
                    for node in ast.walk(self.selector.tree(change.old))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                for point in points:
                    owners = [
                        node
                        for node in functions
                        if node.body and node.body[0].lineno <= point <= node.end_lineno
                    ]
                    if not owners or any(
                        any(
                            node.body[0].lineno <= entry <= node.end_lineno
                            for entry in initial
                        )
                        for node in owners
                    ):
                        collect = True
            excluded = set(self.selector.map.get("traced_children", ()))
            excluded.update(
                self.tests[i]["id"]
                for i in iter_bits(self.selector.always | self.selector.unknown)
            )
            chosen = [
                self.tests[i]
                for i in iter_bits(selected)
                if self.tests[i]["id"] not in excluded
            ]
            targets = []
            plans = {}
            if self.cpu_used < self.cpu_budget and time.monotonic() < self.deadline - 5:
                # Cheap whole tests need no filtering assumptions.
                if len(chosen) <= 10 and sum(t["seconds"] for t in chosen) <= float(
                    os.environ.get("BLAST_SMALL_TEST_SECONDS", "0.7")
                ):
                    targets = [t["id"] for t in chosen]
                elif self.groups:
                    sensitive = self.outside_sensitive(changes)
                    eligible = [
                        (i, self.tests[i])
                        for i in iter_bits(selected & ~sensitive)
                        if self.tests[i]["id"] in self.groups
                        and self.tests[i]["seconds"] >= 1.0
                    ]
                    if eligible:
                        affected = self.task_selector().select(patch)
                        ranked = []
                        for i, test in eligible:
                            group = self.groups[test["id"]]
                            run = [
                                ordinal
                                for index, ordinal in group
                                if affected >> index & 1
                            ]
                            fraction = len(run) / len(group)
                            if fraction <= 0.35:
                                ranked.append(
                                    (test["seconds"] * (1 - fraction), test["id"])
                                )
                                plans[test["id"]] = {
                                    "run": run,
                                    "count": max(ordinal for _, ordinal in group) + 1,
                                }
                        targets = [name for _, name in sorted(ranked, reverse=True)[:3]]
            if not collect and not targets:
                return selected
            remaining = self.deadline - time.monotonic()
            if remaining < 3:
                return self.selector.all if collect else selected
            request = {
                "source": str(self.selector.source),
                "overrides": overrides,
                "collect": collect,
                "tests": targets,
                "plans": plans,
                "cpu_budget": min(
                    float(os.environ.get("BLAST_TEST_CPU_SECONDS", "1.25")),
                    max(0.25, self.cpu_budget - self.cpu_used),
                ),
            }
            start = resource.getrusage(resource.RUSAGE_CHILDREN)
            with tempfile.TemporaryDirectory(prefix="blast-") as temporary:
                result = subprocess.run(
                    [sys.executable, str(self.worker)],
                    input=json.dumps(request).encode(),
                    capture_output=True,
                    timeout=min(8, remaining - 1),
                    env={
                        **os.environ,
                        "TMPDIR": temporary,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": str(self.selector.source),
                    },
                )
            stop = resource.getrusage(resource.RUSAGE_CHILDREN)
            spent = stop.ru_utime + stop.ru_stime - start.ru_utime - start.ru_stime
            self.cpu_used += spent
            if result.returncode:
                self.log.append(
                    f"probe uncertain ({result.returncode}); tests retained"
                )
                return self.selector.all if collect else selected
            report = json.loads(result.stdout)
            self.last_report = report
            if collect and not report.get("collection_ok"):
                failed_files = set(report.get("collection_errors", ()))
                # Import failures can leave registries and partially initialized
                # modules inconsistent beyond the files pytest reports directly.
                selected = self.selector.all
                self.log.append(
                    f"import errors in {len(failed_files)} test files; all tests retained"
                )
            passed = set(report.get("passed", ())) & set(targets)
            for i in list(iter_bits(selected)):
                if self.tests[i]["id"] in passed:
                    selected &= ~(1 << i)
            self.log.append(
                f"checked {len(targets)} tests, cleared {len(passed)}, CPU {spent:.3f}s"
            )
            return selected
        except (
            OSError,
            ValueError,
            SyntaxError,
            KeyError,
            IndexError,
            subprocess.TimeoutExpired,
        ):
            self.log.append("incomplete probe; conservative selection")
            # If an import-sensitive probe did not complete, any file could fail.
            return self.selector.all if locals().get("collect") else selected
