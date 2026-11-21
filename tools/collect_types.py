"""Collect baseline operand type domains. Never evaluates a proposed edit."""

from __future__ import annotations
import concurrent.futures
import enum
import inspect
import json
import math
import os
from pathlib import Path
import pickle
import sys
from instrument import install


def main():
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    profiles = {}
    types = {}
    keys = {}
    value_profiles = {}

    def record(site, side, cls):
        identity = id(cls)
        if identity not in keys:
            key = f"{cls.__module__}.{cls.__qualname__}"
            keys[identity] = key
            equality = inspect.getattr_static(cls, "__eq__", None)
            safe_enum = (
                issubclass(cls, enum.Enum)
                and not issubclass(cls, enum.Flag)
                and equality in (str.__eq__, int.__eq__, object.__eq__)
            )
            inequality = inspect.getattr_static(cls, "__ne__", None)
            fact = {
                "default_equality": equality is object.__eq__,
                "identity_equality": equality is object.__eq__
                and inequality is object.__ne__,
                "enum_identity": safe_enum,
                "enum_inequality": safe_enum
                and inequality in (str.__ne__, int.__ne__, object.__ne__),
                "singleton": cls is type(None) or cls is bool,
            }
            if key in types and types[key] != fact:
                types[key] = {
                    "default_equality": False,
                    "enum_identity": False,
                    "singleton": False,
                }
            else:
                types[key] = fact
        profiles.setdefault(site, [set(), set()])[side].add(keys[identity])

    def bounded(value, depth=0):
        kind = type(value)
        if kind in (bool, type(None)):
            return True
        if kind is int:
            return value.bit_length() <= 128
        if kind is float:
            return math.isfinite(value)
        if kind is str:
            return len(value) <= 80
        return (
            kind is tuple
            and depth < 3
            and len(value) <= 6
            and all(bounded(item, depth + 1) for item in value)
        )

    def record_value(site, side, value):
        profile = value_profiles.setdefault(
            site,
            [{"values": set(), "unknown": False}, {"values": set(), "unknown": False}],
        )[side]
        if profile["unknown"]:
            return
        if not bounded(value):
            profile["unknown"] = True
            profile["values"].clear()
            return
        profile["values"].add(repr(value))
        if len(profile["values"]) > 64:
            profile["unknown"] = True
            profile["values"].clear()

    install(root, lambda obj: None, lambda obj, name: None, record, record_value)

    class Pool:
        def __init__(self, *a, initializer=None, initargs=(), **kw):
            self.initializer = initializer
            self.args = initargs
            self.initialized = False

        def submit(self, fn, /, *args, **kwargs):
            f = concurrent.futures.Future()
            try:
                if not self.initialized:
                    self.initialized = True
                    if self.initializer:
                        self.initializer(*self.args)
                fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as error:
                f.set_exception(error)
            return f

        def shutdown(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    concurrent.futures.ProcessPoolExecutor = Pool
    import pytest

    result = pytest.main(
        ["tests", "-q", "--disable-warnings", "--tb=short", "-o", "addopts="]
    )
    if result:
        raise SystemExit(f"Baseline type collection failed: {result}")
    output.write_text(
        json.dumps(
            {
                "type_profiles": {
                    site: [sorted(a), sorted(b)]
                    for site, (a, b) in sorted(profiles.items())
                },
                "types": types,
                "value_profiles": {
                    site: [
                        {"values": sorted(part["values"]), "unknown": part["unknown"]}
                        for part in parts
                    ]
                    for site, parts in value_profiles.items()
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        f"Collected {len(profiles)} operand domains and {len(types)} type facts",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
