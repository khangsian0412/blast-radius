import os
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


if __name__ == "__main__":
    unittest.main()
