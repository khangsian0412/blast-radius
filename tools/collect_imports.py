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
    class_dependencies = {}
    class_modules = {}
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
            if finder.import_stack:
                consumer_module = finder.import_stack[-1]
                consumer = module_files.get(consumer_module)
                parent_class = key.rsplit(".", 1)[0]
                target_class = None
                frame = sys._getframe(1)
                while frame is not None:
                    values = frame.f_locals
                    if frame.f_code.co_name in {"__init_subclass__", "__new__"}:
                        candidate = (
                            values.get("klass")
                            if frame.f_code.co_name == "__new__"
                            else values.get("cls")
                        )
                        if (
                            isinstance(candidate, type)
                            and candidate.__module__ == consumer_module
                        ):
                            target_class = (
                                f"{candidate.__module__}.{candidate.__qualname__}"
                            )
                            break
                    if values.get("__module__") == consumer_module and isinstance(
                        values.get("__qualname__"), str
                    ):
                        target_class = consumer_module + "." + values["__qualname__"]
                        break
                    frame = frame.f_back
                if target_class:
                    if target_class != parent_class:
                        class_dependencies.setdefault(parent_class, set()).add(
                            target_class
                        )
                elif consumer:
                    class_modules.setdefault(parent_class, set()).add(consumer)
                origins = [
                    module for module in module_files if key.startswith(module + ".")
                ]
                if consumer and origins:
                    origin = module_files[max(origins, key=len)]
                    if origin != consumer:
                        dependencies.setdefault(origin, set()).add(consumer)

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
                "class_dependencies_complete": True,
                "class_dependencies": {
                    key: sorted(value) for key, value in class_dependencies.items()
                },
                "class_module_dependencies": {
                    key: sorted(value) for key, value in class_modules.items()
                },
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
