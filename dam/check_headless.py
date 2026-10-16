#!/usr/bin/env python3
"""Verify the native dlopen loader and DAM workers without a Tcl host."""
import json
from scripts.context_test_utils import ROOT, ENV, run, build

folder, manifest = build('headless', (ROOT/'tests/fixtures/SynthesizedFIFO.bsv').read_text(),
                         'mkSynthesizedFIFO')
bundle = folder/'sim.dam'
out = bundle/'headless-validation'
out.mkdir(exist_ok=True)
loader = ROOT/'lib/DAM/bluesim-headless'
runtime = ROOT/'lib/DAM/dam-runtime'
env = dict(ENV, DAM_CHANNELS=','.join(f"{c['name']}:{c['width']}" for c in manifest['channels']),
           DAM_CYCLES='96', DAM_CHECK_STATE='1', DAM_TRACE=str(out/'reference.trace'),
           DAM_STATE_TRACE=str(out/'reference.state'))
run([loader, bundle/'reference-model.so', 'mkSynthesizedFIFO', '0'],
    env=env, log=out/'reference.log', timeout=120)
expected = [f'RESULT {i} {i+2}' for i in range(12)]
assert (out/'reference.log').read_text().splitlines() == expected
for jitter in ['0', '1']:
    observed = out/('dam-jitter-'+jitter)
    observed.mkdir(exist_ok=True)
    run([runtime, bundle/'manifest.json', bundle, observed, '96'],
        env=dict(ENV, DAM_HEADLESS='1', DAM_HOST_JITTER=jitter, DAM_CHECK_STATE='1'),
        log=out/('runtime-'+jitter+'.log'), timeout=120)
    for suffix in ['trace', 'state', 'log']:
        actual = [line for p in sorted(observed.glob('worker-*.'+suffix)) for line in p.read_text().splitlines()]
        assert sorted(actual) == sorted((out/('reference.'+suffix)).read_text().splitlines()), (jitter, suffix)
(ROOT/'results/headless-checks.json').write_text(json.dumps({'passed': True, 'workers': len(manifest['partitions']),
    'checked': ['outputs', 'FIFO cycles', 'register cycles and physical timestamps', 'host jitter']}, indent=2)+'\n')
print('PASS: native loader and DAM agree on all outputs, FIFO/register cycles and physical timestamps, including host jitter')
