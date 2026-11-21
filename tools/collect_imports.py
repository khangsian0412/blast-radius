"""Record code executed while collecting the unchanged baseline test suite."""

import coverage
import json
import os
from pathlib import Path
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    cov = coverage.Coverage(
        source=[str(root / "sqlglot"), str(root / "tests")], data_file=None
    )
    cov.start()
    import pytest

    result = pytest.main(
        ["tests", "--collect-only", "-q", "-o", "addopts=", "--disable-warnings"]
    )
    cov.stop()
    if result:
        raise SystemExit("Baseline collection failed")
    data = cov.get_data()
    files = {
        str(Path(path).relative_to(root)): sorted(data.lines(path) or [])
        for path in data.measured_files()
        if Path(path).is_relative_to(root)
    }
    output.write_text(json.dumps({"import_lines": files}, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
