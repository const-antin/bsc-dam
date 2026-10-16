#!/usr/bin/env python3
"""Check exact trace merging, including malformed and unequal traces."""
import json
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
checker=ROOT/'build/compare-traces'
checker.parent.mkdir(parents=True,exist_ok=True)
subprocess.run(['c++','-std=c++17','-O2',ROOT/'native/compare_traces.cpp','-o',checker],check=True)
cases=[
    ('reordered', '0 0 a 1\n0 0 b 2\n8 80 a 3\n', ['0 0 b 2\n','0 0 a 1\n8 80 a 3\n',''], True),
    ('empty', '', ['', ''], True),
    ('duplicate', '0 a 1\n', ['0 a 1\n','0 a 1\n'], False),
    ('missing', '0 a 1\n1 a 2\n', ['0 a 1\n'], False),
    ('extra', '0 a 1\n', ['0 a 1\n1 a 2\n'], False),
    ('cycle', '1 a 1\n', ['2 a 1\n'], False),
    ('value', '1 a 1\n', ['1 a 2\n'], False),
    ('timestamp', '1 10 a 1\n', ['1 11 a 1\n'], False),
    ('backwards', '0 a 1\n1 a 2\n', ['1 a 2\n0 a 1\n'], False),
    ('malformed', '0 a 1\n', ['bad a 1\n'], False),
    ('overflow', '0 a 1\n', ['18446744073709551616 a 1\n'], False),
]
with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder)
    for name,want,streams,passed in cases:
        expected=folder/'expected';expected.write_text(want);observed=[]
        for i,stream in enumerate(streams):
            p=folder/str(i);p.write_text(stream);observed.append(p)
        result=subprocess.run([checker,expected,*observed],capture_output=True,text=True)
        assert (result.returncode==0)==passed,(name,result.stdout,result.stderr)
        if passed:assert json.loads(result.stdout)['events']==len(want.splitlines())
        print('PASS',name)
