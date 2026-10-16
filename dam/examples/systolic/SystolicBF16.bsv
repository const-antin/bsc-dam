package SystolicBF16;
import FIFO::*;
import Vector::*;
import RegFile::*;
import FloatingPoint::*;
import SystolicConfig::*;

typedef FloatingPoint#(8,7) BF;
typedef struct {
    Bit#(6) row;
    Vector#(64,BF) values;
} PartialRow deriving (Bits);

(* noinline *)
function BF bfAdd(BF a, BF b);
    return tpl_1(addFP(a,b,Rnd_Nearest_Even));
endfunction
(* noinline *)
function BF bfMul(BF a, BF b);
    return tpl_1(multFP(a,b,Rnd_Nearest_Even));
endfunction

// Counter-based synthetic data: the same 32-bit mixing function used by the
// independent oracle/file generator. No RNG state, input files, or fill clocks.
(* noinline *)
function BF syntheticOperand(Bit#(1) matrix, Bit#(32) row, Bit#(32) col);
    Bit#(32) z = 'h9e3779b9*(1+row) + 'h85ebca6b*(1+col)
                 + 'hc2b2ae35*zeroExtend(matrix);
    z = (z^(z>>16))*'h7feb352d;
    z = (z^(z>>15))*'h846ca68b;
    z = z^(z>>16);
    Bit#(8) exponent = 123 + zeroExtend(z[8]);
    return unpack({z[31],exponent,z[6:0]});
endfunction

// A separately compiled row preserves 64 spatial PEs in one reusable class.
// Methods read old register values, so adjacent rows operate in parallel.
interface SystolicRow;
    method Action advance(Bit#(16) t, Bit#(16) kShard, Bool active,
                          BF a, Vector#(64,BF) north);
    method Vector#(64,BF) south;
    method Vector#(64,BF) result;
endinterface

(* synthesize *)
module mkSystolicRow(SystolicRow);
    Vector#(64,Reg#(BF)) east <- replicateM(mkRegU);
    Vector#(64,Reg#(BF)) down <- replicateM(mkRegU);
    Vector#(64,Reg#(BF)) accum <- replicateM(mkRegU);
    method Action advance(Bit#(16) t, Bit#(16) kShard, Bool active,
                          BF a, Vector#(64,BF) north);
        for (Integer c=0; c<64; c=c+1) begin
            if (active && t>=fromInteger(c) && t<kShard+fromInteger(c)) begin
                BF x = (c==0) ? a : east[c-1];
                east[c] <= x;
                down[c] <= north[c];
                // The first term initializes the accumulator. Operand
                // registers are written by the wavefront before use, so the
                // datapath needs no reset tree (control registers still do).
                BF prior = (t==fromInteger(c)) ? 0 : accum[c];
                accum[c] <= bfAdd(prior,bfMul(x,north[c]));
            end
        end
    endmethod
    method Vector#(64,BF) south = readVReg(down);
    method Vector#(64,BF) result = readVReg(accum);
endmodule

// Output-stationary array. Each PE has its own accumulator and registered
// east/south operand paths. The boundary memories supply skewed row/column
// streams; every operand reaches PE(r,c) at logical clock k+r+c.
(* dam_context *)
module mkSystolicArray#(Integer shard, FIFO#(PartialRow) outputQ)(Empty);
`ifndef SYSTOLIC_GENERATED_INPUTS
    Vector#(64,RegFile#(Bit#(16),BF)) aBanks = newVector;
    Vector#(64,RegFile#(Bit#(16),BF)) bBanks = newVector;
    for (Integer i=0; i<64; i=i+1) begin
        aBanks[i] <- mkRegFileLoad(bankFile(shard,0,i),0,fromInteger(maxKShard-1));
        bBanks[i] <- mkRegFileLoad(bankFile(shard,1,i),0,fromInteger(maxKShard-1));
    end
    RegFile#(Bit#(1),Bit#(16)) runConfig <- mkRegFileLoad(configFile,0,0);
`endif
    Vector#(64,SystolicRow) rows <- replicateM(mkSystolicRow);
    Reg#(Bit#(16)) step <- mkReg(0);
    Reg#(Bit#(7)) outputRow <- mkReg(0);
    Reg#(Bool) emitting <- mkReg(False);
`ifdef SYSTOLIC_GENERATED_INPUTS
    Bit#(16) kShard = fromInteger(activeKShard);
`else
    Bit#(16) kShard = runConfig.sub(0);
`endif

    rule advance (!emitting);
        if (step == kShard+125) emitting <= True;
        else step <= step+1;
    endrule

    // Separate row rules keep generated native functions bounded. All 64
    // rows can fire in the same cycle; this does not time-multiplex the PEs.
    for (Integer r=0; r<64; r=r+1) begin
        rule advanceRow (!emitting);
            // Clamp speculative RegFile reads during fill/drain to a valid
            // address; Bluesim can evaluate reads before the PE enable guard.
            Bit#(16) ai = (step>=fromInteger(r) && step<kShard+fromInteger(r))
                         ? step-fromInteger(r) : 0;
            Vector#(64,BF) north = newVector;
            if (r==0) begin
                for (Integer c=0; c<64; c=c+1) begin
                    Bit#(16) bi = (step>=fromInteger(c) && step<kShard+fromInteger(c))
                                 ? step-fromInteger(c) : 0;
`ifdef SYSTOLIC_GENERATED_INPUTS
                    north[c] = syntheticOperand(1,fromInteger(shard*maxKShard)+zeroExtend(bi),fromInteger(c));
`else
                    north[c] = bBanks[c].sub(bi);
`endif
                end
            end
            else north = rows[r-1].south;
`ifdef SYSTOLIC_GENERATED_INPUTS
            BF a = syntheticOperand(0,fromInteger(r),fromInteger(shard*maxKShard)+zeroExtend(ai));
`else
            BF a = aBanks[r].sub(ai);
`endif
            rows[r].advance(step-fromInteger(r),kShard,step>=fromInteger(r),a,north);
        endrule

        // Mutually exclusive row selection drains one row per clock.
        rule emitRow (emitting && outputRow==fromInteger(r));
            outputQ.enq(PartialRow {row:fromInteger(r), values:rows[r].result});
            outputRow <= outputRow+1;
        endrule
    end

endmodule

(* dam_context *)
module mkTerminalAdder#(Vector#(ArrayCount,FIFO#(PartialRow)) inputs)(Empty);
    Reg#(Bit#(32)) cycle <- mkReg(0);
    rule tick; cycle <= cycle+1; endrule
    rule reduce;
        Vector#(ArrayCount,PartialRow) partials = newVector;
        Bool aligned = True;
        for (Integer s=0; s<valueOf(ArrayCount); s=s+1) begin
            partials[s] = inputs[s].first;
            inputs[s].deq;
            aligned = aligned && (partials[s].row == partials[0].row);
        end
        for (Integer c=0; c<64; c=c+1) begin
            Vector#(ArrayCount,BF) sums = newVector;
            for (Integer s=0; s<valueOf(ArrayCount); s=s+1)
                sums[s] = partials[s].values[c];
            // Spatial balanced tree, with BF16 rounding at each addition.
            // ArrayCount is a compile-time power of two.
            for (Integer width=valueOf(ArrayCount); width>1; width=width/2)
                for (Integer s=0; s<width/2; s=s+1)
                    sums[s] = bfAdd(sums[2*s],sums[2*s+1]);
            Bit#(12) index=zeroExtend(partials[0].row)*64+fromInteger(c);
            $display("OUTPUT %0d %04h",index,pack(sums[0]));
        end
        $display("ROW %0d %0d %0d",partials[0].row,pack(aligned),cycle);
        if (partials[0].row==63) $display("DONE %0d",cycle);
    endrule
endmodule

(* synthesize *)
module mkTensorParallelSystolic(Empty);
    Vector#(ArrayCount,FIFO#(PartialRow)) partial <- replicateM(mkSizedFIFO(8));
    Vector#(ArrayCount,Empty) arrays = newVector;
    for (Integer s=0; s<valueOf(ArrayCount); s=s+1)
        arrays[s] <- mkSystolicArray(s,partial[s]);
    Empty terminal <- mkTerminalAdder(partial);
endmodule
endpackage
