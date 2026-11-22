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

    def configured(self):
        self.source = "class Example:\n    VALUE = 1\n"
        self.data["files"][self.name]["lines"] = {"2": 1}
        self.data.update(
            metadata_sites_complete=True, attribute_dependencies_complete=True,
            classes={"sqlglot.example.Example": 2},
            attributes={"sqlglot.example.Example.VALUE": 1},
        )
        return self.optimized()

    def test_literal_setting_needs_a_completed_collection_check(self):
        selector = self.configured()
        patch = diff(self.source, self.source.replace("VALUE = 1", "VALUE = 2"))
        self.assertEqual(selector.select_conservative(patch), 15)
        selector.import_validator = lambda patch: False
        self.assertEqual(selector.select_conservative(patch), 15)
        selector.import_validator = lambda patch: True
        self.assertEqual(selector.select_conservative(patch), 7)

    def test_setting_reduction_cannot_hide_a_second_code_change(self):
        selector = self.configured()
        selector.import_validator = lambda patch: True
        changed = self.source.replace("VALUE = 1", "VALUE = 2") + "OTHER = 2\n"
        self.assertEqual(selector.select_conservative(diff(self.source, changed)), 15)

    def test_new_setting_type_keeps_every_test(self):
        selector = self.configured()
        selector.import_validator = lambda patch: True
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("VALUE = 1", "VALUE = 'two'"))), 15)

    def identity_comparison(self):
        self.source = "def f(a, b):\n    return a is b\n"
        self.data["files"][self.name]["lines"] = {"2": 1}
        self.data["metadata_sites_complete"] = True
        self.data["types"] = {"example.E": {"enum_identity": True}}
        self.data["type_profiles"] = {self.name + ":2:11": [["example.E"], ["example.E"]]}
        return self.optimized()

    def test_enum_proof_requires_observed_canonical_members(self):
        selector = self.identity_comparison()
        patch = diff(self.source, self.source.replace(" is ", " == "))
        self.assertEqual(selector.select_conservative(patch), 5)
        self.data["canonical_enum_profiles"] = {self.name + ":2:11": [True, True]}
        self.assertEqual(selector.select_conservative(patch), 4)

    def test_proven_comparison_still_keeps_code_observers(self):
        selector = self.identity_comparison()
        self.data["canonical_enum_profiles"] = {self.name + ":2:11": [True, True]}
        self.data["metadata_sites"] = {self.name + ":1": 2}
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace(" is ", " == "))), 6)

    def test_reading_another_source_file_is_not_a_dependency(self):
        selector = self.branching_body()
        self.data["source_reads"] = {"sqlglot/other.py": 3}
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("return 1", "return 3"))), 1)

    def test_legacy_maps_keep_the_global_source_reader_guard(self):
        selector = self.branching_body()
        self.data["source_reads"] = {"sqlglot/other.py": 3}
        del self.data["metadata_sites_complete"]
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("return 1", "return 3"))), 7)

    def closure_setting(self, extra=""):
        self.source = (
            "def make_callback(enabled):\n" + extra
            + "    def callback(x):\n        return x if enabled else 0\n"
            + "    return callback\n\nclass Example:\n    VALUE = make_callback(True)\n"
        )
        self.data.update(metadata_sites_complete=True, attribute_dependencies_complete=True,
            classes={"sqlglot.example.Example": 2}, attributes={"sqlglot.example.Example.VALUE": 1})
        selector = self.optimized()
        selector.import_validator = lambda patch: True
        return selector

    def test_closure_parameter_change_keeps_all_setting_consumers(self):
        selector = self.closure_setting()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("make_callback(True)", "make_callback(False)"))), 7)

    def test_factory_that_performs_other_work_keeps_every_test(self):
        selector = self.closure_setting("    print(enabled)\n")
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("make_callback(True)", "make_callback(False)"))), 15)

    def test_changing_the_factory_itself_keeps_every_test(self):
        selector = self.closure_setting()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("make_callback(True)", "other_callback(True)"))), 15)

    def unread_dictionary(self):
        selector = self.configured()
        self.source = "class Example:\n    VALUE = {'x': (1, 2)}\n"
        (self.root / self.name).write_text(self.source)
        selector.file_cache.clear()
        self.data["dictionaries"] = {"sqlglot.example.Example.VALUE": {"keys": ["x"], "reads": {}, "unsafe": False}}
        return selector

    def test_zero_value_reads_keep_source_observers_without_an_import_probe(self):
        selector = self.unread_dictionary()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("(1, 2)", "(1, 3)"))), 4)

    def test_one_recorded_value_read_prevents_the_zero_read_proof(self):
        selector = self.unread_dictionary()
        self.data["dictionaries"]["sqlglot.example.Example.VALUE"]["reads"] = {"x": 1}
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("(1, 2)", "(1, 3)"))), 15)

    def test_escaped_dictionary_cannot_use_an_unread_value_proof(self):
        selector = self.unread_dictionary()
        self.data["dictionaries"]["sqlglot.example.Example.VALUE"]["unsafe"] = True
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("(1, 2)", "(1, 3)"))), 15)

    def test_changed_encoding_cookie_cannot_hide_an_import_failure(self):
        selector = self.unread_dictionary()
        self.source += "# Unicode: é\n"
        (self.root / self.name).write_text(self.source)
        selector.file_cache.clear()
        changed = "# coding: ascii\n" + self.source.replace("(1, 2)", "(1, 3)")
        self.assertEqual(selector.select_conservative(diff(self.source, changed)), 15)

    def test_unexecuted_entry_keeps_metadata_readers_without_unrelated_callers(self):
        self.source = "def f(x):\n    if x:\n        return 1\n    if False:\n        return 2\n    return 3\n"
        self.data["files"][self.name]["lines"] = {"2": 1, "3": 1, "4": 2, "6": 2}
        self.data["metadata_sites_complete"] = True
        selector = self.optimized()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("return 2", "return 4"))), 4)

    def test_new_compile_warning_is_import_sensitive_even_in_unexecuted_code(self):
        self.source = "def f(x):\n    if False:\n        return x == 1\n    return 3\n"
        self.data["files"][self.name]["lines"] = {"2": 1, "4": 2}
        self.data["metadata_sites_complete"] = True
        selector = self.optimized()
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("x == 1", "x is 1"))), 15)

    def test_verified_child_still_keeps_its_actual_code_inspection_dependency(self):
        selector = self.optimized()
        self.data.update(observer_children=1, verified_observer_children=1)
        self.data["metadata_observers"][self.name] = 1
        self.assertEqual(selector.select_conservative(diff(self.source, "# comment\n" + self.source)), 5)

    def test_measured_scalar_keeps_direct_and_reflective_consumers(self):
        selector = self.configured()
        self.data.update(scalar_collection={"baseline": True}, scalar_fields=["sqlglot.example.Example.VALUE"], scalar_reads={"sqlglot.example.Example.VALUE": 1})
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("VALUE = 1", "VALUE = 2"))), 5)

    def test_collection_time_scalar_read_disables_runtime_only_reduction(self):
        selector = self.configured()
        self.data.update(scalar_collection={"baseline": True}, scalar_fields=["sqlglot.example.Example.VALUE"], scalar_reads={"sqlglot.example.Example.VALUE": 1}, scalar_initialization=["sqlglot.example.Example.VALUE"])
        self.assertEqual(selector.select_conservative(diff(self.source, self.source.replace("VALUE = 1", "VALUE = 2"))), 15)


if __name__ == "__main__":
    unittest.main()
