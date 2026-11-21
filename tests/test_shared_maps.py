import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SharedMapTests(unittest.TestCase):
    def test_scope_members_are_added_to_every_dependency_without_forward_pool_indexes(self):
        nodes = [f"tests/test_x.py::TestShared::test_{n}" for n in range(3)]
        baseline = {
            "tests": nodes, "pool": ["0", "1"],
            "files": {"sqlglot/x.py": {"lines": {"2": 1}, "outside": {}, "init": [1]}},
            "classes": {"sqlglot.x.Shared": 1},
            "attributes": {"sqlglot.x.Shared.FLAG": 1},
            # These forward references were emitted by the old collector.
            "classes_outside": {"sqlglot.x.Shared": 99},
            "attributes_outside": {"sqlglot.x.Shared.FLAG": 99},
            "fixtures": {"tests/fixtures/x.sql": 1},
            "source_reads": {},
        }
        payload = {
            "shared_lines": {"sqlglot/x.py": {"2": nodes[1:]}},
            "shared_classes": {"sqlglot.x.Shared": nodes[1:]},
            "shared_attributes": {"sqlglot.x.Shared.FLAG": nodes[1:]},
            "shared_fixtures": {"tests/fixtures/x.sql": nodes[1:]},
            "shared_source_reads": {"sqlglot/x.py": nodes},
            "shared_collection": {"tests": 3},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, shared = root / "map.json", root / "shared.json"
            target.write_text(json.dumps(baseline))
            shared.write_text(json.dumps(payload))
            subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "tools/merge_maps.py"), str(target), str(shared)], check=True, capture_output=True)
            merged = json.loads(target.read_text())
        def bits(index):
            return int(merged["pool"][index], 16)
        self.assertEqual(bits(merged["files"]["sqlglot/x.py"]["lines"]["2"]), 7)
        self.assertEqual(bits(merged["files"]["sqlglot/x.py"]["outside"]["2"]), 6)
        self.assertEqual(merged["files"]["sqlglot/x.py"]["init"], [1])
        for table, key in [
            ("classes", "sqlglot.x.Shared"),
            ("classes_outside", "sqlglot.x.Shared"),
            ("attributes", "sqlglot.x.Shared.FLAG"),
            ("attributes_outside", "sqlglot.x.Shared.FLAG"),
            ("fixtures", "tests/fixtures/x.sql"),
            ("source_reads", "sqlglot/x.py"),
        ]:
            self.assertEqual(bits(merged[table][key]), 7, table)
        self.assertTrue(merged["outside_class_pool_valid"])


if __name__ == "__main__":
    unittest.main()
