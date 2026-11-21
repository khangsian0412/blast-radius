import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from selector import BASELINE, Selector, apply_change, parse_patch
from verification import Verifier
from test_selector import diff


class PrecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'sqlglot').mkdir()
        self.name = 'sqlglot/example.py'
        self.source = 'def f(a, b):\n    return a is b\n'
        (self.root/self.name).write_text(self.source)
        self.tests = [
            {'number': 0, 'id': 'tests/test_x.py::test_a', 'file': 'tests/test_x.py', 'seconds': .001},
            {'number': 1, 'id': 'tests/test_x.py::test_b', 'file': 'tests/test_x.py', 'seconds': .002},
        ]
        self.data = {'baseline': BASELINE, 'tests': [t['id'] for t in self.tests], 'pool': ['0','1','2','3'], 'files': {self.name: {'lines': {'2': 3}}}, 'fixtures': {}, 'types': {}, 'type_profiles': {}}

    def selector(self):
        return Selector(self.tests, None, self.root, map_data=self.data)

    def domain(self, left, right, types):
        node = next(node for node in ast.walk(ast.parse(self.source)) if isinstance(node, ast.Compare))
        self.data['type_profiles'][f'{self.name}:{node.lineno}:{node.col_offset}'] = [left, right]
        self.data['types'] = types

    def test_enum_domain_proves_identity_equality(self):
        self.domain(['example.Color'], ['example.Color'], {'example.Color': {'enum_identity': True}})
        self.assertEqual(self.selector().select(diff(self.source, self.source.replace(' is ', ' == '))), 0)

    def test_default_object_equality_and_none_are_identity(self):
        self.domain(['example.Table','builtins.NoneType'], ['builtins.NoneType'], {'example.Table': {'default_equality': True}, 'builtins.NoneType': {'singleton': True}})
        self.assertEqual(self.selector().select(diff(self.source, self.source.replace(' is ', ' == '))), 0)

    def test_custom_equality_prevents_proof(self):
        self.domain(['example.Custom'], ['builtins.NoneType'], {'example.Custom': {'default_equality': False}, 'builtins.NoneType': {'singleton': True}})
        self.assertEqual(self.selector().select(diff(self.source, self.source.replace(' is ', ' == '))), 3)

    def test_enum_and_raw_string_are_not_identity_equivalent(self):
        self.domain(['example.Color','builtins.str'], ['example.Color'], {'example.Color': {'enum_identity': True}})
        self.assertEqual(self.selector().select(diff(self.source, self.source.replace(' is ', ' == '))), 3)

    def test_other_edits_are_not_hidden_by_a_local_proof(self):
        self.domain(['example.Color'], ['example.Color'], {'example.Color': {'enum_identity': True}})
        modified = self.source.replace(' is ', ' == ').replace('return a', 'return not a')
        self.assertEqual(self.selector().select(diff(self.source, modified)), 3)

    def test_replacement_does_not_select_the_next_statement(self):
        change, = parse_patch(diff('a = 1\nb = 2\n','a = 3\nb = 2\n'))
        _, removed, gaps = apply_change('a = 1\nb = 2\n', change)
        self.assertEqual(removed, {1})
        self.assertEqual(gaps, set())

    def test_attribute_read_precision_survives_a_blank_line(self):
        self.source = 'class Example:\n    FLAG = 1\n\n    def f(self):\n        return 0\n'
        (self.root/self.name).write_text(self.source)
        self.data['files'][self.name]['lines'] = {'5': 3}
        self.data['classes'] = {'sqlglot.example.Example': 3}
        self.data['attributes'] = {'sqlglot.example.Example.FLAG': 1}
        self.data['attribute_init'] = []
        self.assertEqual(self.selector().select(diff(self.source,self.source.replace('FLAG = 1','FLAG = 2'))), 1)

    def test_import_only_function_cannot_use_empty_runtime_coverage(self):
        self.source = 'def f():\n    return 1\n'
        (self.root/self.name).write_text(self.source)
        self.data['files'][self.name] = {'lines': {}, 'init': [2]}
        self.assertEqual(self.selector().select(diff(self.source,self.source.replace('return 1','return 2'))),3)

    def test_collection_failure_includes_every_test_in_the_failed_file(self):
        self.data['files'][self.name]['init'] = [2]
        selector = self.selector()
        response = {'collection_ok': False, 'collection_errors': ['tests/test_x.py'], 'passed': []}
        with patch('verification.subprocess.run', return_value=subprocess.CompletedProcess([],0,json.dumps(response).encode(),b'')):
            selected = Verifier(selector).check(diff(self.source,self.source.replace(' is ',' != ')),1)
        self.assertEqual(selected, 3)

    def test_unknown_collection_error_is_conservative(self):
        self.data['files'][self.name]['init'] = [2]
        response = {'collection_ok': False, 'collection_errors': [''], 'passed': []}
        with patch('verification.subprocess.run', return_value=subprocess.CompletedProcess([],0,json.dumps(response).encode(),b'')):
            self.assertEqual(Verifier(self.selector()).check(diff(self.source,self.source.replace(' is ',' != ')),1),3)

    def test_import_check_timeout_is_conservative(self):
        self.data['files'][self.name]['init'] = [2]
        with patch('verification.subprocess.run', side_effect=subprocess.TimeoutExpired('probe',8)):
            self.assertEqual(Verifier(self.selector()).check(diff(self.source,self.source.replace(' is ',' != ')),1),3)

    def test_probe_cannot_clear_an_unrequested_test(self):
        response = {'collection_ok': True, 'passed': ['tests/test_x.py::test_b']}
        with patch('verification.subprocess.run', return_value=subprocess.CompletedProcess([],0,json.dumps(response).encode(),b'')):
            self.assertEqual(Verifier(self.selector()).check(diff(self.source,self.source.replace(' is ',' != ')),1),1)

    def test_old_maps_never_skip_worker_tasks(self):
        self.assertEqual(Verifier(self.selector()).outside_sensitive([]),3)


if __name__ == '__main__': unittest.main()
