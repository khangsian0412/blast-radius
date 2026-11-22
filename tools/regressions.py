"""Container regressions for import failures, metadata readers and subtests."""

import argparse
from pathlib import Path
import subprocess

SCRIPT = r"""
import ast, difflib, json, pathlib, subprocess, sys, tempfile
sys.path.insert(0, '/app')
from selector import Selector, OptimizedSelector
from collection_check import CollectionCheck
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
before=selector.select_conservative(patch)
assert before == selector.all, 'import-sensitive edits must keep the complete inventory'
optimized=OptimizedSelector(tests,pathlib.Path('/app/maps/coverage.json'),root)
optimized.import_validator=CollectionCheck(optimized)
assert not optimized.import_validator(patch), 'real-file collection must detect the changed import failure'
assert optimized.select_conservative(patch)==optimized.all, 'failed collection must disable the setting reduction'
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
node='tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata'
index=next(i for i,t in enumerate(tests) if t['id']==node)
for dialect in ('bigquery','duckdb','presto','redshift'):
    name=f'sqlglot/typing/{dialect}.py'
    source=(root/name).read_text()
    modified=source.replace('exp.DType.BIGINT','exp.DType.INT')
    assert modified != source
    patch=''.join(difflib.unified_diff(source.splitlines(True),modified.splitlines(True),'a/'+name,'b/'+name))
    selected=selector.select_conservative(patch)
    assert selected >> index & 1, f'{name}: source-inspection test omitted'
    request={'source':str(root),'overrides':{name:modified},'tests':[node],'collect':False,'plans':{},'cpu_budget':5}
    result=subprocess.run([sys.executable,'/app/probe.py'],input=json.dumps(request),capture_output=True,text=True,check=True,timeout=15)
    metadata=json.loads(result.stdout)
    assert node in metadata['failed'], metadata
name='sqlglot/generators/mysql.py'
source=(root/name).read_text()
modified=source.replace('supports_position=True','supports_position=False',1)
assert modified != source
patch=''.join(difflib.unified_diff(source.splitlines(True),modified.splitlines(True),'a/'+name,'b/'+name))
node='tests/dialects/test_dialect.py::TestDialect::test_operators'
index=next(i for i,t in enumerate(tests) if t['id']==node)
assert selector.select_conservative(patch) >> index & 1, 'callback failure omitted'
request={'source':str(root),'overrides':{name:modified},'tests':[node],'collect':False,'plans':{},'cpu_budget':5}
result=subprocess.run([sys.executable,'/app/probe.py'],input=json.dumps(request),capture_output=True,text=True,check=True,timeout=15)
callback=json.loads(result.stdout)
assert node in callback['failed'], callback
assert selector.map.get('import_calls_complete')
name='sqlglot/parser.py'
source=(root/name).read_text()
tree=ast.parse(source)
called=set(selector.map['import_calls'][name])
candidate=next(n for n in ast.walk(tree) if isinstance(n,ast.Lambda) and n.lineno in called and any(isinstance(v,ast.Constant) and type(v.value) is int for v in ast.walk(n.body)))
value=next(v for v in ast.walk(candidate.body) if isinstance(v,ast.Constant) and type(v.value) is int)
lines=source.splitlines(True)
start=sum(map(len,lines[:value.lineno-1]))+value.col_offset
end=sum(map(len,lines[:value.end_lineno-1]))+value.end_col_offset
modified=source[:start]+str(value.value+1)+source[end:]
patch=''.join(difflib.unified_diff(source.splitlines(True),modified.splitlines(True),'a/'+name,'b/'+name))
assert selector.select_conservative(patch)==selector.all, 'actual import-time callback was narrowed'
fixture='tests/fixtures/optimizer/tpc-h/tpc-h.sql'
fixture_bits=selector.pool[selector.map['fixtures'][fixture]]
executor=[i for i,t in enumerate(tests) if t['id'].startswith('tests/test_executor.py::TestExecutor::')]
assert executor and all(fixture_bits >> i & 1 for i in executor), 'shared fixture lost class setup dependencies'
name='sqlglot/executor/table.py'
tree=ast.parse((root/name).read_text())
table=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Table')
constructor=next(n for n in table.body if isinstance(n,ast.FunctionDef) and n.name=='__init__')
shared_function=selector.range_mask(name,constructor.lineno,constructor.end_lineno)
assert all(shared_function >> i & 1 for i in executor), 'shared setup function lost cached-state consumers'
name='sqlglot/optimizer/scope.py'
source=(root/name).read_text()
modified='# an apparently harmless comment\n'+source
patch=''.join(difflib.unified_diff(source.splitlines(True),modified.splitlines(True),'a/'+name,'b/'+name))
with tempfile.TemporaryDirectory() as temporary:
    data=pathlib.Path(temporary)
    (data/'commits').mkdir()
    (data/'commits'/'comment.patch').write_text(patch)
    (data/'tests.json').write_text(json.dumps({'tests':tests}))
    result=subprocess.run([sys.executable,'/app/selector.py','--source',str(root),'--data',str(data)],capture_output=True,text=True,check=True,timeout=15)
    output=json.loads(result.stdout)
    numbers=[number for worker in output['workers'] for number in worker]
    assert len(numbers)==len(tests) and set(numbers)=={t['number'] for t in tests}
print(json.dumps({'import_failure_files':len(files),'omitted_after_check':len(missing),'failed_subtest_detected':True,'failing_metadata_readers_retained':4,'default_comment_selection_covers_every_test':True,'callback_failure_retained':True,'import_time_callback_stays_broad':True,'shared_fixture_class_tests':len(executor)}))
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
