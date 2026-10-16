#!/usr/bin/env python3
"""Ordinary-context regressions for the native DAM compiler."""
import json
import subprocess
from pathlib import Path
from scripts.context_test_utils import ROOT, BASE, ENV, ROWS, run, build

GOOD = (ROOT/'tests/fixtures/RequiredContexts.bsv').read_text()
folder, manifest = build('valid', GOOD, 'mkRequiredContexts', verify=True)
contexts = manifest['required_contexts']
assert len(contexts) == 2 and len({c['partition'] for c in contexts}) == 2
parts = {p['id']:p for p in manifest['partitions']}
for c in contexts:
    assert len(parts[c['partition']]['rules']) == 2, c
assert not any('localQ' in c['name'] for c in manifest['channels'])
lines = (folder/'sim.dam/validation/reference.log').read_text().splitlines()
assert lines == [f'RESULT {i} {i+2}' for i in range(12)], lines
build('single-command', GOOD, 'mkRequiredContexts', verify=True, single=True)
build('worker-cap', GOOD, 'mkRequiredContexts', workers=2, expected='dam_context requirements')


def test_source(module, top_body='', imports='', top_attr=''):
    return f'''package ContextCase;
import FIFO::*;
{imports}
{module}
(* synthesize {top_attr} *)
module mkContextCase(Empty);
FIFO#(Bit#(32)) q <- mkSizedFIFO(4);
Reg#(Bit#(32)) n <- mkReg(0);
{top_body}
rule produce (n < 8); q.enq(n); n <= n+1; endrule
endmodule
endpackage
'''

build('shared-state', test_source('''
(* dam_context *) module mkStage#(FIFO#(Bit#(32)) q, Reg#(Bit#(32)) n)(Empty);
rule consume; q.deq; n <= q.first; endrule
endmodule''', 'Empty s <- mkStage(q,n);'), 'mkContextCase', expected='cannot be isolated')
build('empty', test_source('(* dam_context *) module mkStage(Empty); endmodule',
                         'Empty s <- mkStage;'), 'mkContextCase', expected='has no surviving rules')
build('optimized-away', test_source('''(* dam_context *) module mkStage(Empty);
Reg#(Bit#(32)) r <- mkReg(0);
rule never (False); r <= r+1; endrule
endmodule''', 'Empty s <- mkStage;'), 'mkContextCase', expected='has no surviving rules')
build('unsupported-primitive', test_source('''(* dam_context *) module mkStage#(FIFO#(Bit#(32)) q)(Empty);
FIFO#(Bit#(32)) memoryQ <- mkSizedBRAMFIFO(16);
rule consume; memoryQ.enq(q.first); q.deq; endrule
endmodule''','Empty s <- mkStage(q);', 'import BRAMFIFO::*;'), 'mkContextCase', expected='dam_context requirement failed')
build('synthesized-child', test_source('''(* synthesize, dam_context *) module mkStage(Empty);
Reg#(Bit#(32)) r <- mkReg(0);
rule tick; r <= r+1; endrule
endmodule''','Empty s <- mkStage;'), 'mkContextCase', expected='at least two FIFO-connected workers')
build('top-annotation', test_source('', top_attr=', dam_context'), 'mkContextCase', expected='simulation top')
build('nested-annotations', test_source('''
(* dam_context *) module mkInner#(FIFO#(Bit#(32)) q)(Empty);
rule consume; q.deq; endrule
endmodule
(* dam_context *) module mkOuter#(FIFO#(Bit#(32)) q)(Empty);
Empty inner <- mkInner(q);
endmodule''','Empty outer <- mkOuter(q);'), 'mkContextCase', expected='overlapping dam_context')
# Registered state accessed only from an external method caller is still owned
# by the annotated instance, even if no internal rule accesses it.
build('method-state-leak', test_source('''
interface Stage; method Action put(Bit#(32) x); method Bit#(32) get; endinterface
(* dam_context *) module mkStage#(FIFO#(Bit#(32)) q)(Stage);
Reg#(Bit#(32)) r <- mkReg(0);
rule consume; q.deq; endrule
method Action put(Bit#(32) x); r <= x; endmethod
method Bit#(32) get = r;
endmodule''', '''Stage s <- mkStage(q);
rule leak; s.put(n); $display("%0d", s.get); endrule'''), 'mkContextCase', expected='exposes non-FIFO state')
# Same-cycle FIFO semantics force a dependency across the requested boundary.
bypass = test_source('''
(* dam_context *) module mkStage#(FIFO#(Bit#(32)) q)(Empty);
rule consume; q.deq; endrule
endmodule''','Empty s <- mkStage(q);','import SpecialFIFOs::*;').replace('q <- mkSizedFIFO(4)', 'q <- mkBypassFIFO')
bypass = bypass.replace('rule consume; q.deq; endrule', 'FIFO#(Bit#(32)) localQ <- mkSizedFIFO(4);\nrule consume; localQ.enq(q.first); q.deq; endrule')
build('bypass-cut', bypass, 'mkContextCase', expected='cannot be isolated')
# Rule splitting must preserve context ownership through optimization.
split = GOOD.replace('localQ.enq(Token { value: t.value + 1, index: t.index });',
    '(* split *) if (t.index[0] == 1)\n'
    '    localQ.enq(Token { value: t.value + 2, index: t.index });\n'
    'else localQ.enq(Token { value: t.value + 1, index: t.index });')
split_folder, split_manifest = build('split-rules', split, 'mkRequiredContexts', verify=True)
assert len(split_manifest['required_contexts']) == 2
assert (split_folder/'sim.dam/validation/reference.log').read_text().splitlines() == [
    f'RESULT {i} {i+2+2*(i%2)}' for i in range(12)]
# Explicit schedule dependencies must not be lost when enforcing a context.
ordered = GOOD.replace('(* synthesize *)', '(* synthesize, execution_order = "produce, stage.receive" *)')
build('schedule-edge', ordered, 'mkRequiredContexts', expected='cannot be isolated')
# Synthesized implementation hierarchy belongs to the allocating ordinary
# annotated module. Repeated instances and autonomous nested rules must stay
# separate, with all state/cycle/output traces preserved.
leaf = """
interface Leaf;
method Action put(Token x);
method Token get;
endinterface
(* synthesize *) module mkLeaf(Leaf);
Reg#(Token) value <- mkReg(unpack(0));
Reg#(Bit#(32)) clockCount <- mkReg(0);
rule tick; clockCount <= clockCount+1; endrule
method Action put(Token x); value <= x; endmethod
method Token get = value;
endmodule
"""
hier = GOOD.replace('// Each instance', leaf+'\n// Each instance')
hier = hier.replace('FIFO#(Token) localQ <- mkSizedFIFO(3);',
                    'Leaf leaf <- mkLeaf;\nReg#(Bool) full <- mkReg(False);')
hier = hier.replace('rule receive;', 'rule receive (!full);')
hier = hier.replace('localQ.enq(Token { value: t.value + 1, index: t.index });',
                    'leaf.put(Token { value: t.value + 1, index: t.index }); full <= True;')
hier = hier.replace('rule send;', 'rule send (full);').replace('outputQ.enq(localQ.first);',
                    'outputQ.enq(leaf.get);').replace('localQ.deq;', 'full <= False;')
hf, hm = build('owned-hierarchy', hier, 'mkRequiredContexts', verify=True)
assert (hf/'sim.dam/validation/reference.log').read_text().splitlines() == [f'RESULT {i} {i+2}' for i in range(12)]
assert len(hm['required_contexts']) == 2
for c in hm['required_contexts']:
    rules=next(p['rules'] for p in hm['partitions'] if p['id']==c['partition'])
    assert len(rules)==3 and any('.RL_tick' in r for r in rules), rules
# Observable child methods cannot silently escape global ordering.
observable = hier.replace('value <= x; endmethod', 'value <= x; $display("LEAF %0d", x.index); endmethod')
build('owned-hierarchy-effects', observable, 'mkRequiredContexts', expected='cannot be isolated')
# The compiler must never invent workers for missing annotations.
build('no-annotations', GOOD.replace('(* dam_context *)',''), 'mkRequiredContexts',
      expected='requires explicit dam_context annotations')
# Independent unannotated rules stay in the same main worker as top-level I/O.
extra = GOOD.replace('    Empty stage <-',
    '    Reg#(Bit#(32)) count <- mkReg(0);\n    rule topTick; count <= count+1; endrule\n    Empty stage <-')
_, main_manifest=build('single-main-worker',extra,'mkRequiredContexts',verify=True)
main=next(p for p in main_manifest['partitions'] if p['id']==main_manifest['main_partition'])
assert len(main_manifest['partitions'])==3
assert set(main['rules'])=={'RL_produce','RL_consume','RL_topTick'}
# A single explicit stage plus all remaining logic in main is legal.
# Put the whole pair of stages in one ordinary annotated wrapper instead.
wrapped=GOOD.replace('(* dam_context *)','')
wrapper='(* dam_context *)\nmodule mkPair#(FIFO#(Token) a, FIFO#(Token) c)(Empty);\nFIFO#(Token) b <- mkSizedFIFO(4);\nEmpty stage <- mkRequiredStage(a,b);\nEmpty stage_extra <- mkRequiredStage(b,c);\nendmodule\n'
wrapped=wrapped.replace('(* synthesize *)',wrapper+'\n(* synthesize *)')
wrapped=wrapped.replace('    Empty stage <- mkRequiredStage(a,b);\n    Empty stage_extra <- mkRequiredStage(b,c);','    Empty pair <- mkPair(a,c);')
_, single=build('one-context-plus-main',wrapped,'mkRequiredContexts',verify=True)
assert len(single['required_contexts'])==1 and len(single['partitions'])==2 and single['main_partition'] is not None
# If every rule is annotated, omit the empty main worker.
a=GOOD.index('    Reg#(Bit#(64)) sent');b=GOOD.rindex('endmodule')
io_body=GOOD[a:b]
io_module='(* dam_context *)\nmodule mkIO#(FIFO#(Token) a, FIFO#(Token) c)(Empty);\n'+io_body+'endmodule\n'
all_marked=GOOD.replace(io_body,'    Empty io <- mkIO(a,c);\n').replace('(* synthesize *)',io_module+'\n(* synthesize *)')
_, no_main=build('empty-main-omitted',all_marked,'mkRequiredContexts',verify=True)
assert no_main['main_partition'] is None and len(no_main['partitions'])==3
# Ordinary Bluesim remains available both with and without annotations.
for name in ['valid','no-annotations']:
    folder=BASE/name
    run([ROOT/'bin/bsc-dam','-sim','-bdir',folder,'-simdir',folder,'-info-dir',folder,
         '-e','mkRequiredContexts','-o',folder/'plain'],env=ENV,log=folder/'plain-link.log',timeout=300)
    run([folder/'plain','-m','96'],env=ENV,log=folder/'plain-run.log',timeout=120)
    assert [line for line in (folder/'plain-run.log').read_text().splitlines() if line.startswith('RESULT ')]==[f'RESULT {i} {i+2}' for i in range(12)]
    ROWS.append(dict(case='ordinary-'+name,passed=True))
    print('PASS: ordinary Bluesim',name,flush=True)
for flag in ['-dam-force','-dam-min-gain','-dam-ipc-weight','-dam-worker-overhead']:
    result=subprocess.run([ROOT/'bin/bsc-dam',flag],env=ENV,capture_output=True,text=True)
    assert result.returncode != 0 and 'Unrecognized flag' in result.stdout+result.stderr,(flag,result)
    ROWS.append(dict(case='removed-flag-'+flag,passed=True))
print('PASS: removed policy flags rejected',flush=True)
Path(ROOT/'results/context-checks.json').write_text(json.dumps(ROWS, indent=2)+'\n')
print(f'PASS: {len(ROWS)} cases; exact FIFO/register/time/output comparisons in DAM, skip, and jitter modes; Python blocked in BSC', flush=True)
