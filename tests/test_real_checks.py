import os
import json
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collection_check import CollectionCheck, overlay
from selector import Selector
import test_conservative
from test_selector import diff


class RealCheckTests(unittest.TestCase):
    setUp = test_conservative.ConservativeTests.setUp

    def checker(self):
        self.patch = diff(self.source, self.source.replace("return 1", "return 3"))
        return CollectionCheck(Selector(self.tests, None, self.root, map_data=self.data))

    def test_passing_sibling_cannot_clear_a_failing_subtest(self):
        check = self.checker()
        report = {"collection_ok": True, "passed": [self.tests[0]["id"], self.tests[1]["id"]], "failed": [self.tests[1]["id"]]}
        with patch.object(check, "run", return_value=report):
            self.assertEqual(check.check_tests(self.patch, 3), 2)

    def test_collection_error_cannot_certify_a_passing_test(self):
        check = self.checker()
        report = {"collection_ok": False, "passed": [self.tests[0]["id"]], "failed": []}
        with patch.object(check, "run", return_value=report):
            self.assertEqual(check.check_tests(self.patch, 3), 3)

    def test_unknown_report_ids_are_not_used(self):
        check = self.checker()
        report = {"collection_ok": True, "passed": ["unrequested"], "failed": []}
        with patch.object(check, "run", return_value=report):
            self.assertEqual(check.check_tests(self.patch, 3), 3)

    def test_collection_and_whole_test_result_reused_only_for_same_request(self):
        check = self.checker()
        report = {"collection_ok": True, "passed": [self.tests[0]["id"]], "failed": [self.tests[1]["id"]]}
        with patch.object(check, "run", return_value=report) as run:
            self.assertTrue(check.validate_selection(self.patch, 3))
            self.assertEqual(check.check_tests(self.patch, 3), 2)
            self.assertEqual(run.call_count, 1)
            run.assert_called_once_with(self.patch, [test["id"] for test in self.tests[:2]], require_imports=True)
            check.check_tests(self.patch, 3)
            self.assertEqual(run.call_count, 2)

    def test_no_probe_still_checks_collection_without_running_tests(self):
        check = self.checker()
        check.allow_tests = False
        with patch.object(check, "run", return_value={"collection_ok": True}) as run:
            self.assertTrue(check.validate_selection(self.patch, 3))
            run.assert_called_once_with(self.patch)

    def test_same_size_same_timestamp_patch_cannot_reuse_baseline_bytecode(self):
        baseline = self.root / "baseline"
        baseline.mkdir()
        source = baseline / "example.py"
        source.write_text("value = 1\n")
        cached = Path(py_compile.compile(str(source), doraise=True))
        original_cache = cached.read_bytes()
        destination = self.root / "patched"
        overlay(baseline, destination, {"example.py": "value = 2\n"})
        original_stat = source.stat()
        os.utime(destination / "example.py", ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        result = subprocess.run([sys.executable, "-B", "-c", "import example; print(example.value)"], cwd=destination, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "2")
        self.assertEqual(cached.read_bytes(), original_cache)

    def test_loader_source_inspection_is_distinct_from_loading_code(self):
        source = self.root / "observed.py"
        source.write_text("value = 1\n")
        script = '''import importlib.machinery, json, os, sys
from tools.collect_observers import import_read
source = sys.argv[1]
events = []
def audit(event, values):
    if event == "open" and values[0] == source:
        events.append(import_read())
sys.addaudithook(audit)
loader = importlib.machinery.SourceFileLoader("observed", source)
loader.get_code("observed")
loader.get_source("observed")
print(json.dumps(events))
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(source)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [True, False])


if __name__ == "__main__":
    unittest.main()
