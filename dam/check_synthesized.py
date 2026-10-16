#!/usr/bin/env python3
"""Synthesized FIFO interfaces: real compiler, runtime and exact cycle traces."""
import json
from scripts.context_test_utils import ROOT, ROWS, ENV, run, build

GOOD=(ROOT/'tests/fixtures/SynthesizedFIFO.bsv').read_text()
TOP='mkSynthesizedFIFO'

def accepted(name, source=GOOD, **kwargs):
    folder,manifest=build('synth-'+name,source,TOP,verify=True,**kwargs)
    lines=(folder/'sim.dam/validation/reference.log').read_text().splitlines()
    assert lines==[f'RESULT {i} {i+2}' for i in range(12)],lines
    return folder,manifest

first,m=accepted('interfaces')
assert {c['instance'] for c in m['required_contexts']}=={'a','b'}
assert {c['name'] for c in m['channels']}=={'a.iq','a.oq','b.iq','b.oq'}
assert all(c['width']==80 for c in m['channels'])
assert len(m['partitions'])==3 and m['main_partition'] is not None
assert not any(p['skip_safe'] for p in m['partitions'])
accepted('depth-one',GOOD.replace('mkSizedFIFO(3)','mkSizedFIFO(1)').replace('mkSizedFIFO(4)','mkSizedFIFO(1)'))
accepted('fifo2',GOOD.replace('mkSizedFIFO(3)','mkFIFO').replace('mkSizedFIFO(4)','mkFIFO'))

# Retaining synthesized arithmetic helpers must not hide their dependencies.
helper=GOOD.replace('(* synthesize, dam_context *)',
    '(* noinline *) function Bit#(64) increment(Bit#(64) x); return x+1; endfunction\n(* synthesize, dam_context *)').replace('t.value + 1','increment(t.value)')
accepted('noinline-arithmetic',helper)

# The queues themselves can be inside a further synthesized implementation.
queues='''(* synthesize *) module mkQueues(Stage);
 FIFO#(Token) iq <- mkSizedFIFO(3);
 FIFO#(Token) oq <- mkSizedFIFO(4);
 interface inputQ = iq;
 interface outputQ = oq;
endmodule
'''
nested=GOOD.replace('(* synthesize, dam_context *)',queues+'\n(* synthesize, dam_context *)')
nested=nested.replace('module mkStage(Stage);\n FIFO#(Token) iq <- mkSizedFIFO(3);\n FIFO#(Token) oq <- mkSizedFIFO(4);',
 'module mkStage(Stage);\n Stage qs <- mkQueues;\n let iq = qs.inputQ;\n let oq = qs.outputQ;')
_,nm=accepted('nested-queues',nested)
assert {c['name'] for c in nm['channels']}=={'a.qs.iq','a.qs.oq','b.qs.iq','b.qs.oq'}

# An unannotated synthesized parent does not hide explicit descendants.
wrapped=GOOD.replace('(* synthesize *)\nmodule mkSynthesizedFIFO(Empty);',
 '''(* synthesize *) module mkWrapper(Stage);
 Stage child <- mkStage;
 return child;
endmodule
(* synthesize *)
module mkSynthesizedFIFO(Empty);''').replace('Stage a <- mkStage;', 'Stage a <- mkWrapper;').replace('Stage b <- mkStage;', 'Stage b <- mkWrapper;')
_,wm=accepted('nested-contexts',wrapped)
assert {c['instance'] for c in wm['required_contexts']}=={'a.child','b.child'}

for fifo in ['mkBypassFIFO','mkPipelineFIFO']:
    build('synth-'+fifo,GOOD.replace('mkSizedFIFO(3)',fifo),TOP,expected='cannot be isolated')
# Exposing a method named like a FIFO operation does not make it a FIFO.
state=GOOD.replace('interface FIFO#(Token) inputQ;', 'interface FIFO#(Token) inputQ;\n method Action setValue(Token x);')
state=state.replace(' FIFO#(Token) iq <-', ' Reg#(Token) secret <- mkReg(unpack(0));\n FIFO#(Token) iq <-')
state=state.replace(' interface inputQ = iq;', ' method Action setValue(Token x); secret <= x; endmethod\n interface inputQ = iq;')
state=state.replace('a.inputQ.enq(Token { index: n, value: zeroExtend(n) });', 'a.inputQ.enq(Token { index: n, value: zeroExtend(n) }); a.setValue(unpack(0));')
build('synth-exposed-register',state,TOP,expected='exposes non-FIFO state')
# The FIFO's ready predicate must not depend on another worker's registers.
guarded=GOOD.replace(' FIFO#(Token) iq <-', ' Reg#(Bool) enabled <- mkReg(True);\n rule toggle; enabled <= !enabled; endrule\n FIFO#(Token) iq <-')
guarded=guarded.replace(' interface inputQ = iq;', ''' interface FIFO inputQ;
  method Action enq(Token x) if (enabled); iq.enq(x); endmethod
  method first = iq.first;
  method Action deq; iq.deq; endmethod
  method Action clear; iq.clear; endmethod
 endinterface''')
build('synth-register-guard',guarded,TOP,expected='cannot be isolated')
build('synth-clear',GOOD.replace('n <= n+1;', 'n <= n+1; a.outputQ.clear;'),TOP,expected='FIFO clear or unsupported FIFO method')
build('synth-same-side',GOOD.replace('n <= n+1;', 'n <= n+1; a.outputQ.enq(unpack(0));'),TOP,expected='cannot be isolated')
build('synth-overlap',wrapped.replace('(* synthesize *) module mkWrapper', '(* synthesize, dam_context *) module mkWrapper'),TOP,expected='overlapping dam_context')
build('synth-worker-cap',GOOD,TOP,workers=2,expected='exceeding -dam-workers')
# Extra effects in an exported method are not a transparent FIFO boundary.
printed=GOOD.replace(' interface inputQ = iq;', """ interface FIFO inputQ;
  method Action enq(Token x); iq.enq(x); $display("EXPORTED %0d", x.index); endmethod
  method first = iq.first;
  method Action deq; iq.deq; endmethod
  method Action clear; iq.clear; endmethod
 endinterface""")
build('synth-method-effect',printed,TOP,expected='exposes non-FIFO state')
# State behind a returned value is checked, not just action-method bodies.
value=GOOD.replace(' FIFO#(Token) iq <-', ' Reg#(Bit#(64)) bias <- mkReg(0);\n rule tick; bias <= bias+1; endrule\n FIFO#(Token) iq <-')
value=value.replace(' interface outputQ = oq;', """ interface FIFO outputQ;
  method Action enq(Token x); oq.enq(x); endmethod
  method Token first; let t=oq.first; return Token {index:t.index,value:t.value+bias}; endmethod
  method Action deq; oq.deq; endmethod
  method Action clear; oq.clear; endmethod
 endinterface""")
build('synth-value-state',value,TOP,expected='cannot be isolated')
# An ordinary annotated module inside a synthesized wrapper uses the same proof.
ordinary=wrapped.replace('(* synthesize, dam_context *)','(* dam_context *)')
accepted('ordinary-inside-synth',ordinary)
# An annotated ordinary wrapper cannot contain another annotated context.
build('synth-ordinary-overlap',wrapped.replace('(* synthesize *) module mkWrapper', '(* dam_context *) module mkWrapper'),TOP,expected='overlapping dam_context')
# The uninstrumented ordinary Bluesim link path still accepts the annotations.
run([ROOT/'bin/bsc-dam','-sim','-bdir',first,'-simdir',first,'-info-dir',first,
     '-e',TOP,'-o',first/'plain'],env=ENV,log=first/'plain-link.log',timeout=300)
run([first/'plain','-m','96'],env=ENV,log=first/'plain-run.log',timeout=120)
assert [l for l in (first/'plain-run.log').read_text().splitlines() if l.startswith('RESULT ')]==[f'RESULT {i} {i+2}' for i in range(12)]
ROWS.append(dict(case='synth-ordinary-bluesim',passed=True))
# More than the former default of eight workers requires no numeric flag.
many=GOOD.replace(' Stage b <- mkStage;', ' Stage b <- mkStage;\n'+'\n'.join(f' Stage extra{i} <- mkStage;' for i in range(7)))
_,mm=accepted('implicit-count',many)
assert len(mm['required_contexts'])==9 and len(mm['partitions'])==10
(ROOT/'results/synthesized-checks.json').write_text(json.dumps(ROWS,indent=2)+'\n')
print(f'PASS: {len(ROWS)} synthesized boundary cases; exact FIFO/register/time/output comparisons in DAM, skip and jitter modes',flush=True)
