"""Refine baseline enum domains without re-profiling every parser comparison."""

import concurrent.futures
import enum
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys

from instrument import install


def main():
    root, mapping, output = map(lambda value: Path(value).resolve(), sys.argv[1:])
    data = json.loads(mapping.read_text())
    sites = {
        site for site, domains in data["type_profiles"].items()
        if any(data["types"].get(kind, {}).get("enum_identity") for domain in domains for kind in domain)
    }
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    members = {}

    def record(site, side, value):
        flags = members.setdefault(site, [True, True])
        flags[side] &= (
            isinstance(value, enum.Enum)
            and any(value is member for member in vars(type(value)).get("_member_map_", {}).values())
        )

    install(root, lambda obj: None, lambda obj, name: None, record_value=record, sites=sites, only_operands=True)

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
    import pytest
    failures = []
    class Plugin:
        def pytest_runtest_logreport(self, report):
            if report.failed:
                failures.append(report.nodeid)
    result = pytest.main(["tests", "-q", "--tb=short", "--disable-warnings", "-o", "addopts=", "-p", "no:cacheprovider"], plugins=[Plugin()])
    if result or failures:
        raise SystemExit(f"Enum baseline failed: {result}, {failures}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "canonical_enum_profiles": members,
        "enum_collection": {"python": sys.version.split()[0], "platform": sys.platform, "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "target_sites": len(sites), "observed_sites": len(members)},
    }, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
