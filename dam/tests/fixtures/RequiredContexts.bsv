package RequiredContexts;
import FIFO::*;

typedef struct {
    Bit#(65) value;
    Bit#(64) index;
} Token deriving (Bits, Eq);

// Each instance must be one worker, including both rules and its local FIFO.
(* dam_context *)
module mkRequiredStage#(FIFO#(Token) inputQ, FIFO#(Token) outputQ)(Empty);
    FIFO#(Token) localQ <- mkSizedFIFO(3);
    rule receive;
        let t = inputQ.first;
        inputQ.deq;
        localQ.enq(Token { value: t.value + 1, index: t.index });
    endrule
    rule send;
        outputQ.enq(localQ.first);
        localQ.deq;
    endrule
endmodule

(* synthesize *)
module mkRequiredContexts(Empty);
    FIFO#(Token) a <- mkSizedFIFO(4);
    FIFO#(Token) b <- mkSizedFIFO(4);
    FIFO#(Token) c <- mkSizedFIFO(4);
    Empty stage <- mkRequiredStage(a,b);
    Empty stage_extra <- mkRequiredStage(b,c);
    Reg#(Bit#(64)) sent <- mkReg(0);
    rule produce (sent < 12);
        a.enq(Token { value: zeroExtend(sent), index: sent });
        sent <= sent + 1;
    endrule
    rule consume;
        let t = c.first;
        c.deq;
        $display("RESULT %0d %0d", t.index, t.value);
    endrule
endmodule
endpackage
