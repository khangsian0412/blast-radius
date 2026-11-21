"""Record baseline initializer execution and dynamic import-time dependencies."""

import coverage
import importlib
import json
import os
from pathlib import Path
import sys
from instrument import install, attribute_owner


def main():
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    unsafe = set()
    dependencies = {}
    module_files = {}
    for path in (root / "sqlglot").rglob("*.py"):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module_files[".".join(parts)] = path.relative_to(root).as_posix()

    def attribute(obj, name):
        key = attribute_owner(obj, name)
        if key:
            unsafe.add(key)

    finder = install(root, lambda obj: None, attribute)

    def profile(frame, event, arg):
        if event == "call" and finder.import_stack:
            path = frame.f_code.co_filename
            if path.startswith(str(root / "sqlglot") + "/"):
                dependency = Path(path).relative_to(root).as_posix()
                consumer = module_files.get(finder.import_stack[-1])
                if consumer and consumer != dependency:
                    dependencies.setdefault(dependency, set()).add(consumer)

    cov = coverage.Coverage(
        source=[str(root / "sqlglot"), str(root / "tests")], data_file=None
    )
    cov.start()
    sys.setprofile(profile)
    import pytest

    result = pytest.main(
        ["tests", "--collect-only", "-q", "-o", "addopts=", "--disable-warnings"]
    )
    if result:
        raise SystemExit("Baseline collection failed")
    for module in sorted(module_files):
        if not module.endswith(".__main__"):
            importlib.import_module(module)
    sys.setprofile(None)
    cov.stop()
    data = cov.get_data()
    files = {
        str(Path(path).relative_to(root)): sorted(data.lines(path) or [])
        for path in data.measured_files()
        if Path(path).is_relative_to(root)
    }
    output.write_text(
        json.dumps(
            {
                "import_lines": files,
                "import_attributes": sorted(unsafe),
                "import_dependencies": {
                    key: sorted(value) for key, value in dependencies.items()
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
