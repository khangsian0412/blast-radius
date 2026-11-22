import ast
import unittest

from selector import OptimizedSelector
import test_conservative
from test_selector import diff


class ObserverTests(unittest.TestCase):
    setUp = test_conservative.ConservativeTests.setUp

    def optimized(self):
        (self.root / self.name).write_text(self.source)
        self.data["observer_collection"] = {"tests": 4}
        self.data["metadata_observers"] = {self.name: 2}
        self.data["observer_children"] = 0
        return OptimizedSelector(self.tests, None, self.root, map_data=self.data)

    def test_comment_keeps_observers_that_never_call_the_function(self):
        selector = self.optimized()
        # Readers (bit 2) and the code observer (bit 1) stay; unrelated callers
        # need not run merely because code locations shifted.
        self.assertEqual(selector.select_conservative(diff(self.source, "# comment\n" + self.source)), 6)

    def test_import_time_code_observer_requires_full_inventory(self):
        selector = self.optimized()
        self.data["metadata_initialization"] = [self.name]
        self.assertEqual(selector.select_conservative(diff(self.source, "# comment\n" + self.source)), 15)

    def test_child_consumers_are_preserved(self):
        selector = self.optimized()
        self.data["observer_children"] = 1
        self.assertEqual(selector.select_conservative(diff(self.source, "# comment\n" + self.source)), 7)

    def test_comment_cannot_hide_a_later_file_change(self):
        selector = self.optimized()
        patch = diff(self.source, "# comment\n" + self.source)
        patch += diff("old\n", "new\n", "unknown-resource")
        self.assertEqual(selector.select_conservative(patch), 15)

    def test_ast_equivalent_docstring_is_still_source_observable(self):
        self.source = 'def f():\n    "one"\n    return 1\n'
        changed = self.source.replace('"one"', "'one'")
        self.assertEqual(ast.dump(ast.parse(self.source)), ast.dump(ast.parse(changed)))
        selector = self.optimized()
        self.assertEqual(selector.select_conservative(diff(self.source, changed)), 6)

    def branching_body(self):
        self.source = "def f(x):\n    if x:\n        return 1\n    return 2\n"
        self.data["files"][self.name]["lines"] = {"3": 1, "4": 2}
        self.data["metadata_sites_complete"] = True
        return self.optimized()

    def test_body_replacement_keeps_entry_context_and_cached_code_readers(self):
        selector = self.branching_body()
        self.data["metadata_sites"] = {self.name + ":1": 2}
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("return 1", "return 3"))), 7)

    def test_unrelated_branch_is_not_a_dependency_of_return_expression(self):
        selector = self.branching_body()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("return 1", "return 3"))), 5)

    def test_cached_collection_time_code_read_keeps_every_test(self):
        selector = self.branching_body()
        self.data["metadata_initialization_sites"] = [self.name + ":1"]
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("return 1", "return 3"))), 15)

    def test_short_circuit_change_keeps_the_whole_condition_entry(self):
        self.source = "def f(a, b):\n    if (a\n        and b):\n        return 1\n    return 0\n"
        self.data["files"][self.name]["lines"] = {"2": 1, "3": 2, "4": 2}
        self.data["metadata_sites_complete"] = True
        selector = self.optimized()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("and b", "or b"))), 7)


if __name__ == "__main__":
    unittest.main()
