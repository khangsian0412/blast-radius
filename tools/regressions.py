"""Container regressions for whole-file import failures and pytest subtests."""

import argparse
from pathlib import Path
import subprocess

SCRIPT = r"""
import ast, difflib, json, pathlib, subprocess, sys
sys.path.insert(0, '/app')
from selector import Selector
from verification import Verifier
root=pathlib.Path('/opt/sqlglot')
tests=json.loads(pathlib.Path('/data/tests.json').read_text())['tests']
selector=Selector(tests,pathlib.Path('/app/maps/coverage.json'),root)
name='sqlglot/expressions/query.py'
source=(root/name).read_text()
tree=ast.parse(source)
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Select')
assignment=next(n for n in cls.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='arg_types' for t in n.targets))
value=next(value for key,value in zip(assignment.value.keys,assignment.value.values) if isinstance(key,ast.Constant) and key.value=='exclude')
lines=source.splitlines(True)
start=sum(map(len,lines[:value.lineno-1]))+value.col_offset
end=sum(map(len,lines[:value.end_lineno-1]))+value.end_col_offset
modified=source[:start]+'True'+source[end:]
patch=''.join(difflib.unified_diff(source.splitlines(True),modified.splitlines(True),'a/'+name,'b/'+name))
before=selector.select(patch)
verifier=Verifier(selector)
after=verifier.check(patch,before)
report=verifier.last_report
assert report is not None and not report['collection_ok'], verifier.log
files=set(report['collection_errors'])
assert files, report
missing=[t['id'] for i,t in enumerate(tests) if t['file'] in files and not(after>>i&1)]
assert not missing, missing
node='tests/test_serde.py::TestSubtestProbe::test_detect'
request={'source':str(root),'overrides':{'tests/test_serde.py':'import unittest\nclass TestSubtestProbe(unittest.TestCase):\n    def test_detect(self):\n        with self.subTest(x=1):\n            self.assertEqual(1,2)\n'},'tests':[node],'collect':False,'plans':{},'cpu_budget':2}
result=subprocess.run([sys.executable,'/app/probe.py'],input=json.dumps(request),capture_output=True,text=True,check=True,timeout=15)
subtests=json.loads(result.stdout)
assert node in subtests['failed'] and node not in subtests['passed'], subtests
print(json.dumps({'import_failure_files':len(files),'omitted_after_check':len(missing),'failed_subtest_detected':True}))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="blast-radius")
    parser.add_argument("--data", type=Path, default=Path("sample"))
    args = parser.parse_args()
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",
        "--user",
        "65534:65534",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{(args.data / 'tests.json').resolve()}:/data/tests.json:ro",
        "--entrypoint",
        "python",
        args.image,
        "-c",
        SCRIPT,
    ]
    subprocess.run(command, check=True, timeout=60)


if __name__ == "__main__":
    main()
