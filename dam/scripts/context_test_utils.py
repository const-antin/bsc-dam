#!/usr/bin/env python3
"""Regression harness: native BSC owns annotation parsing, analysis, and execution."""
import json
import os
from pathlib import Path
import subprocess
from .test_utils import ROOT, run, compiler_environment

BASE = ROOT / 'build/context-checks'
BASE.mkdir(parents=True, exist_ok=True)
ENV = compiler_environment()
GUARD = BASE / 'no-python'
GUARD.mkdir(exist_ok=True)
for name in ['python', 'python3']:
    p = GUARD / name
    p.write_text('#!/bin/sh\necho Python-is-forbidden >&2\nexit 97\n')
    p.chmod(0o755)
ENV['PATH'] = str(GUARD) + ':' + ENV['PATH']
ROWS = []


def build(name, source, top, expected=None, workers=None, verify=False, single=False):
    folder = BASE / name
    folder.mkdir(exist_ok=True)
    src = folder / (top[2:] + '.bsv')
    src.write_text(source)
    model = folder / 'sim'
    common = [ROOT/'bin/bsc-dam', '-sim', '-p', '+:'+str(folder),
              '-bdir', folder, '-simdir', folder, '-info-dir', folder]
    options = ['-dam', '-dam-cycles', '96', '-o', model]
    if workers is not None:
        options += ['-dam-workers', str(workers)]
    if verify:
        options += ['-dam-verify']
    if single:
        command = common + options + ['-u', '-g', top, src]
    else:
        run(common + ['-u', '-g', top, src], env=ENV, log=folder/'compile.log', timeout=180)
        command = common + ['-e', top] + options
    try:
        run(command, env=ENV, log=folder/'link.log', timeout=300)
    except RuntimeError:
        if expected is None:
            raise
        log = (folder/'link.log').read_text()
        assert expected in log, (name, expected, log[-3000:])
        assert 'DAM fallback:' not in log, (name, 'silently fell back')
        ROWS.append(dict(case=name, passed=True, diagnostic=expected))
        print('PASS:', name, expected, flush=True)
        return
    assert expected is None, (name, 'unexpected successful build')
    bundle = folder/'sim.dam'
    decision = json.loads((bundle/'decision.json').read_text())
    manifest = json.loads((bundle/'manifest.json').read_text())
    main=manifest['main_partition']
    expected_workers=len(manifest['required_contexts'])+(main is not None)
    assert decision == dict(execution='dam', policy='explicit_contexts', workers=expected_workers, main_partition=main)
    assert len(manifest['partitions']) == expected_workers
    assert main is None or main not in [c['partition'] for c in manifest['required_contexts']]
    assert manifest['schema']==3 and manifest['protocol_version']==5
    assert not ({'cost_model','static_total','bridge_cost'} & manifest.keys())
    assert all('static_cost' not in p for p in manifest['partitions'])
    if verify:
        assert json.loads((bundle/'validation/validation.json').read_text())['passed']
    ROWS.append(dict(case=name, passed=True, decision=decision,
                     required_contexts=manifest['required_contexts']))
    print('PASS:', name, manifest['required_contexts'], flush=True)
    return folder, manifest
