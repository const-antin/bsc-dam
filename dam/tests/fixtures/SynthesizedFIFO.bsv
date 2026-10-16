package SynthesizedFIFO;
import FIFO::*;
import SpecialFIFOs::*;

typedef struct { Bit#(16) index; Bit#(64) value; } Token deriving (Bits, Eq);
interface Stage;
 interface FIFO#(Token) inputQ;
 interface FIFO#(Token) outputQ;
endinterface
(* synthesize, dam_context *)
module mkStage(Stage);
 FIFO#(Token) iq <- mkSizedFIFO(3);
 FIFO#(Token) oq <- mkSizedFIFO(4);
 rule work;
  let t = iq.first;
  oq.enq(Token { index: t.index, value: t.value + 1 }); iq.deq;
 endrule
 interface inputQ = iq;
 interface outputQ = oq;
endmodule
(* synthesize *)
module mkSynthesizedFIFO(Empty);
 Stage a <- mkStage;
 Stage b <- mkStage;
 Reg#(Bit#(16)) n <- mkReg(0);
 rule produce(n < 12); a.inputQ.enq(Token { index: n, value: zeroExtend(n) }); n <= n+1; endrule
 rule connect; b.inputQ.enq(a.outputQ.first); a.outputQ.deq; endrule
 rule consume; $display("RESULT %0d %0d",b.outputQ.first.index,b.outputQ.first.value); b.outputQ.deq; endrule
endmodule
endpackage
