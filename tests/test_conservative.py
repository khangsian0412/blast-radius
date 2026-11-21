import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from selector import BASELINE, Selector, parse_patch
from test_selector import diff


class ConservativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "sqlglot").mkdir()
        self.name = "sqlglot/example.py"
        self.source = "def f():\n    return 1\n\ndef g():\n    return 2\n"
        self.tests = [
            {"id": f"tests/test_{i}.py::test_check", "file": f"tests/test_{i}.py", "number": 10 + i, "seconds": 0.01}
            for i in range(4)
        ]
        self.data = {
            "baseline": BASELINE,
            "tests": [test["id"] for test in self.tests],
            "pool": ["0", "1", "2", "4"],
            "files": {self.name: {"lines": {"2": 1, "5": 2}}},
            "source_reads": {self.name: 3},
            "fixtures": {"tests/fixtures/x.sql": 1},
        }

    def select(self, changed):
        (self.root / self.name).write_text(self.source)
        selector = Selector(self.tests, None, self.root, map_data=self.data)
        return selector.select_conservative(diff(self.source, changed))

    def test_body_edit_keeps_function_callers_and_inspectors(self):
        self.assertEqual(self.select(self.source.replace("return 1", "return 3")), 5)

    def test_entire_function_coverage_is_kept_across_different_branches(self):
        self.source = "def f(x):\n    if x:\n        return 1\n    return 2\n"
        self.data["files"][self.name]["lines"] = {"3": 1, "4": 2}
        self.assertEqual(self.select(self.source.replace("return 1", "return 3")), 7)

    def test_multiline_callback_body_uses_function_coverage(self):
        self.source = "class Example:\n    CALLBACK = lambda value: (\n        value + 1\n    )\n"
        self.data["files"][self.name] = {"lines": {"3": 1}, "init": [2]}
        self.data["import_calls_complete"] = True
        self.data["import_calls"] = {}
        self.assertEqual(self.select(self.source.replace("value + 1", "value + 2")), 5)

    def test_callback_executed_during_import_keeps_every_test(self):
        self.source = "class Example:\n    CALLBACK = lambda value: (\n        value + 1\n    )\n"
        self.data["files"][self.name] = {"lines": {"3": 1}, "init": [2, 3]}
        self.data["import_calls_complete"] = True
        self.data["import_calls"] = {self.name: [2]}
        self.assertEqual(self.select(self.source.replace("value + 1", "value + 2")), 15)

    def test_callback_without_call_measurements_keeps_every_test(self):
        self.source = "class Example:\n    CALLBACK = lambda value: (\n        value + 1\n    )\n"
        self.data["files"][self.name] = {"lines": {"3": 1}, "init": [2]}
        self.assertEqual(self.select(self.source.replace("value + 1", "value + 2")), 15)

    def runtime_setting(self):
        self.source = "class Example:\n    VALUE = 1\n"
        self.data["attributes"] = {"sqlglot.example.Example.VALUE": 1}
        self.data["classes"] = {"sqlglot.example.Example": 2}
        self.data["files"][self.name]["lines"] = {"2": 1}

    def test_runtime_scalar_retains_all_class_and_attribute_users(self):
        self.runtime_setting()
        self.assertEqual(self.select(self.source.replace("VALUE = 1", "VALUE = 2")), 7)

    def test_scalar_read_during_import_is_not_narrowed(self):
        self.runtime_setting()
        self.data["attribute_init"] = ["sqlglot.example.Example.VALUE"]
        self.assertEqual(self.select(self.source.replace("VALUE = 1", "VALUE = 2")), 15)

    def test_new_setting_type_or_expression_is_not_narrowed(self):
        self.runtime_setting()
        for replacement in ('"changed"', '1 + 1'):
            self.assertEqual(self.select(self.source.replace("VALUE = 1", "VALUE = " + replacement)), 15)

    def test_missing_class_usage_prevents_runtime_setting_reduction(self):
        self.runtime_setting()
        self.data["classes"] = {}
        self.assertEqual(self.select(self.source.replace("VALUE = 1", "VALUE = 2")), 15)

    def test_comment_can_change_observable_code_metadata(self):
        changed = "# a seemingly harmless comment\n" + self.source
        self.assertEqual(ast.dump(ast.parse(self.source)), ast.dump(ast.parse(changed)))
        old_namespace, new_namespace = {}, {}
        exec(compile(self.source, "example.py", "exec"), old_namespace)
        exec(compile(changed, "example.py", "exec"), new_namespace)
        self.assertNotEqual(
            old_namespace["f"].__code__.co_firstlineno,
            new_namespace["f"].__code__.co_firstlineno,
        )
        self.assertEqual(self.select(changed), 15)

    def test_collection_time_function_change_keeps_every_test(self):
        self.data["files"][self.name]["init"] = [2]
        self.assertEqual(self.select(self.source.replace("return 1", "return 3")), 15)

    def test_function_docstring_is_definition_metadata(self):
        self.source = 'def f():\n    """one"""\n    return 1\n'
        self.assertEqual(self.select(self.source.replace('"""one"""', '"""two"""')), 15)

    def test_configuration_change_keeps_every_test(self):
        self.source = "class Example:\n    VALUE = 1\n"
        self.assertEqual(self.select(self.source.replace("VALUE = 1", "VALUE = 2")), 15)

    def test_missing_coverage_is_uncertain(self):
        self.data["files"][self.name]["lines"] = {}
        self.data["source_reads"] = {}
        self.assertEqual(self.select(self.source.replace("return 1", "return 3")), 15)

    def test_known_fixture_keeps_all_readers_and_source_inspectors(self):
        selector = Selector(self.tests, None, self.root, map_data=self.data)
        self.assertEqual(selector.select_conservative(diff("SELECT 1;\n", "SELECT 2;\n", "tests/fixtures/x.sql")), 5)

    def test_unmapped_or_empty_fixture_coverage_keeps_every_test(self):
        for mapping in ({}, {"tests/fixtures/x.sql": 0}):
            self.data["fixtures"] = mapping
            selector = Selector(self.tests, None, self.root, map_data=self.data)
            self.assertEqual(selector.select_conservative(diff("SELECT 1;\n", "SELECT 2;\n", "tests/fixtures/x.sql")), 15)

    def test_duplicate_file_sections_are_not_overwritten_by_a_probe(self):
        patch = diff(self.source, self.source.replace("return 1", "return 3"))
        patch += diff(self.source, "# comment\n" + self.source)
        with self.assertRaises(ValueError):
            parse_patch(patch)

    def test_an_unparsed_git_section_cannot_be_silently_ignored(self):
        patch = "diff --git a/sqlglot/example.py b/sqlglot/example.py\n"
        patch += diff(self.source, self.source.replace("return 1", "return 3"))
        patch += "diff --git a/other b/other\n"
        with self.assertRaises(ValueError):
            parse_patch(patch)

    def test_mode_only_section_requires_full_inventory(self):
        patch = diff(self.source, self.source.replace("return 1", "return 3"))
        patch += "diff --git a/other b/other\nold mode 100644\nnew mode 100755\n"
        with self.assertRaises(ValueError):
            parse_patch(patch)

    def test_complete_control_uses_only_inventory_and_ignores_patch_contents(self):
        data = self.root / "data"
        (data / "commits").mkdir(parents=True)
        (data / "tests.json").write_text(json.dumps({"tests": self.tests}))
        (data / "commits/a.patch").write_bytes(b"\xffnot even a readable patch")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "selector.py"), "--selection", "complete", "--data", str(data)],
            check=True, capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["commit"], "a")
        self.assertEqual(sorted(n for worker in output["workers"] for n in worker), [10, 11, 12, 13])

    def test_unreadable_patch_text_keeps_every_test_in_default_mode(self):
        data = self.root / "data"
        (data / "commits").mkdir(parents=True)
        (data / "tests.json").write_text(json.dumps({"tests": self.tests}))
        (data / "commits/a.patch").write_bytes(b"\xffnot a UTF-8 diff")
        mapping = self.root / "map.json"
        mapping.write_text(json.dumps(self.data))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "selector.py"), "--data", str(data), "--source", str(self.root), "--map", str(mapping)],
            check=True, capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        self.assertEqual(sorted(n for worker in output["workers"] for n in worker), [10, 11, 12, 13])


if __name__ == "__main__":
    unittest.main()
