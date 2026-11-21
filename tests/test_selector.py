import difflib
import json
from pathlib import Path
import tempfile
import unittest

from selector import BASELINE, Selector, apply_change, parse_patch, schedule


def diff(old, new, name="sqlglot/example.py"):
    return "".join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), "a/" + name, "b/" + name
        )
    )


class DiffTests(unittest.TestCase):
    def test_replacement_and_insertion_reconstruct_exactly(self):
        old = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
        new = "prefix\na\nb\nchanged\nd\ne\nf\ng\nh\ni\n"
        (change,) = parse_patch(diff(old, new))
        result, deleted, inserted = apply_change(old, change)
        self.assertEqual(result, new)
        self.assertEqual(deleted, {3, 10})
        self.assertIn(0, inserted)

    def test_deletion_and_creation(self):
        (deleted,) = parse_patch(
            "--- a/sqlglot/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        )
        (added,) = parse_patch("--- /dev/null\n+++ b/sqlglot/b.py\n@@ -0,0 +1 @@\n+x\n")
        self.assertIsNone(deleted.new)
        self.assertIsNone(added.old)
        self.assertEqual(apply_change("", added)[0], "x\n")

    def test_mismatched_context_is_rejected(self):
        (change,) = parse_patch(diff("a\nb\n", "a\nc\n"))
        with self.assertRaises(ValueError):
            apply_change("wrong\nb\n", change)

    def test_parent_directory_path_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_patch("--- a/../escape.py\n+++ b/../escape.py\n")

    def test_missing_final_newline(self):
        patch = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        (change,) = parse_patch(patch)
        self.assertEqual(apply_change("old", change)[0], "new")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sqlglot").mkdir()
        (self.root / "tests").mkdir()
        self.name = "sqlglot/example.py"
        self.old = (
            "def f(a, b):\n    if (a\n        and b):\n        return 1\n    return 0\n"
        )
        (self.root / self.name).write_text(self.old)
        self.tests = [
            {
                "number": 42,
                "id": "tests/test_x.py::test_a",
                "file": "tests/test_x.py",
                "seconds": 0.1,
            },
            {
                "number": 7,
                "id": "tests/test_x.py::test_b",
                "file": "tests/test_x.py",
                "seconds": 0.2,
            },
        ]
        self.map = {
            "baseline": BASELINE,
            "tests": [t["id"] for t in self.tests],
            "pool": ["0", "1", "2", "3"],
            "files": {self.name: {"lines": {"2": 3, "3": 1, "4": 1, "5": 2}}},
            "fixtures": {"tests/fixtures/x.sql": 2},
        }

    def selector(self):
        path = self.root / "map.json"
        path.write_text(json.dumps(self.map))
        return Selector(self.tests, path, self.root)

    def test_short_circuit_operand_includes_condition_entry(self):
        patch = diff(self.old, self.old.replace("and b", "or b"))
        self.assertEqual(self.selector().select(patch), 3)

    def test_comments_do_not_change_syntax_tree(self):
        self.assertEqual(
            self.selector().select(diff(self.old, "# explanation\n" + self.old)), 0
        )

    def test_fixture_reads_are_dependencies(self):
        patch = diff("SELECT 1;\n", "SELECT 2;\n", "tests/fixtures/x.sql")
        self.assertEqual(self.selector().select(patch), 2)

    def test_unseen_resource_runs_all_tests(self):
        self.assertEqual(
            self.selector().select(diff("a\n", "b\n", "tests/config.json")), 3
        )

    def test_invalid_python_runs_all_tests(self):
        self.assertEqual(
            self.selector().select(
                diff(self.old, self.old.replace("return 1", "return ("))
            ),
            3,
        )

    def test_inventory_numbers_are_translated_by_node_id(self):
        self.tests.reverse()
        selector = self.selector()
        patch = diff("x\n", "y\n", "tests/fixtures/x.sql")
        # Map IDs follow the original test inventory, but the organizer reordered.
        self.assertEqual(selector.select(patch), 1)

    def test_unknown_test_is_never_silently_omitted(self):
        self.tests.append(
            {
                "number": 99,
                "id": "tests/test_new.py::test_c",
                "file": "tests/test_new.py",
                "seconds": 1,
            }
        )
        self.assertEqual(
            self.selector().select(diff("x\n", "y\n", "tests/fixtures/x.sql")), 6
        )

    def test_multi_file_patch_unions_dependencies(self):
        patch = diff("x\n", "y\n", "tests/fixtures/x.sql") + diff(
            self.old, self.old.replace("return 1", "return 2")
        )
        self.assertEqual(self.selector().select(patch), 3)

    def test_encoding_comment_can_break_import(self):
        self.old = 'def f():\n    return "é"\n'
        (self.root / self.name).write_text(self.old)
        patch = diff(self.old, "# coding: ascii\n" + self.old)
        self.assertEqual(self.selector().select(patch), 3)

    def test_class_constants_use_observed_class_dependencies(self):
        self.old = "class Example:\n    VALUE = 1\n\nclass Other:\n    VALUE = 2\n"
        (self.root / self.name).write_text(self.old)
        self.map["files"][self.name]["lines"] = {}
        self.map["classes"] = {"sqlglot.example.Example": 1, "sqlglot.example.Other": 2}
        patch = diff(self.old, self.old.replace("VALUE = 1", "VALUE = 3"))
        self.assertEqual(self.selector().select(patch), 1)

    def test_module_constant_uses_reverse_import_dependencies(self):
        self.old = "VALUE = 1\n"
        (self.root / self.name).write_text(self.old)
        (self.root / "tests/test_x.py").write_text(
            "from sqlglot.example import VALUE\n"
        )
        self.map["files"][self.name]["lines"] = {}
        self.assertEqual(self.selector().select(diff(self.old, "VALUE = 2\n")), 3)


class SchedulingTests(unittest.TestCase):
    def test_empty_selection_costs_nothing(self):
        self.assertEqual(schedule([]), [])

    def test_short_jobs_use_one_worker(self):
        jobs = [{"number": i, "seconds": 0.001} for i in range(100)]
        workers = schedule(jobs)
        self.assertEqual(len(workers), 1)
        self.assertEqual(set(workers[0]), set(range(100)))

    def test_long_parallel_jobs_use_all_eight_workers(self):
        jobs = [{"number": i, "seconds": 100} for i in range(8)]
        self.assertEqual(len(schedule(jobs)), 8)

    def test_partition_is_complete_disjoint_and_deterministic(self):
        jobs = [{"number": i, "seconds": (i % 13) * 0.389} for i in range(103)]
        workers = schedule(jobs)
        flat = [i for worker in workers for i in worker]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(set(flat), set(range(103)))
        self.assertLessEqual(len(workers), 8)
        self.assertEqual(workers, schedule(list(reversed(jobs))))


if __name__ == "__main__":
    unittest.main()
