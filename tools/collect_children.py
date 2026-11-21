"""Trace literal Python subprocess programs in the unmodified baseline tests."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    found = []
    files = {}
    for file in (root / "tests").rglob("test_*.py"):
        tree = ast.parse(file.read_text())
        owners = [
            (
                node,
                [
                    parent.name
                    for parent in ast.walk(tree)
                    if isinstance(parent, ast.ClassDef)
                    and parent.lineno <= node.lineno <= parent.end_lineno
                ],
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        for function, parents in owners:
            constants = {
                statement.targets[0].id: statement.value.value
                for statement in function.body
                if isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Constant)
            }
            for call in ast.walk(function):
                if (
                    not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "run"
                    or not call.args
                    or not isinstance(call.args[0], ast.List)
                ):
                    continue
                values = []
                for argument in call.args[0].elts:
                    values.append(
                        argument.value
                        if isinstance(argument, ast.Constant)
                        else constants.get(argument.id)
                        if isinstance(argument, ast.Name)
                        else None
                    )
                if (
                    len(values) != 3
                    or values[:2] not in (["python", "-c"], ["python3", "-c"])
                    or not isinstance(values[2], str)
                ):
                    continue
                nodeid = "::".join(
                    [file.relative_to(root).as_posix(), *parents, function.name]
                )
                wrapper = (
                    "import coverage,json,pathlib\nc=coverage.Coverage(source=["
                    + repr(str(root / "sqlglot"))
                    + "],data_file=None)\nc.start()\nexec(compile("
                    + repr(values[2])
                    + ",'<string>','exec'))\nc.stop()\nd=c.get_data()\nprint('BLAST_CHILD '+json.dumps({str(pathlib.Path(p).relative_to("
                    + repr(str(root))
                    + ")):sorted(d.lines(p) or []) for p in d.measured_files()}))"
                )
                result = subprocess.run(
                    [sys.executable, "-c", wrapper],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={**os.environ, "PYTHONPATH": str(root)},
                )
                if result.returncode:
                    raise SystemExit(
                        f"Baseline subprocess failed: {nodeid}\n{result.stderr}"
                    )
                measurements = [
                    line
                    for line in result.stdout.splitlines()
                    if line.startswith("BLAST_CHILD ")
                ]
                if len(measurements) != 1:
                    raise SystemExit("Missing child coverage")
                found.append(nodeid)
                for name, lines in json.loads(
                    measurements[0].removeprefix("BLAST_CHILD ")
                ).items():
                    for line in lines:
                        files.setdefault(name, {}).setdefault(str(line), []).append(
                            nodeid
                        )
    output.write_text(
        json.dumps(
            {"child_lines": files, "traced_children": sorted(found)},
            separators=(",", ":"),
        )
        + "\n"
    )
    print(f"Traced {len(found)} baseline child programs")


if __name__ == "__main__":
    main()
