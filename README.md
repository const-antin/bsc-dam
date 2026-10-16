# Parallel Bluesim with DAM

Are you simulating globally synchronous Bluespec designs with locally decoupled,
FIFO-connected blocks, and struggling with simulator performance? This repository
extends Bluespec's built-in simulator, **Bluesim**, with parallel simulation through
the [Dataflow Abstract Machine (DAM) simulator framework](https://doi.org/10.1109/ISCA59077.2024.00046)
(Zhang et al., ISCA 2024).

Annotate your modules with `(* dam_context *)` and build with `-dam`. Each annotated
instance runs in a dedicated simulation process, connected through FIFO channels
modeled in DAM. **All unannotated logic stays in one main worker**; if it is empty,
no main worker is created. No other hardware-source changes are needed for designs
that meet the requirements below.

The compiler checks the requested boundaries and builds the workers automatically.
It does not choose boundaries, merge contexts, or decide whether parallel execution
is worthwhile. An invalid separation is a compiler error. Without `-dam`, the same
annotated design can still run in ordinary Bluesim.

## Requirements

- Contexts communicate through supported registered Bluespec FIFOs, such as
  `mkFIFO` and `mkSizedFIFO`. Payloads may be any concrete type implementing `Bits`.
- Shared state and same-cycle dependencies must remain within a worker.
  Pipeline/bypass FIFOs with same-cycle behavior cannot cross a context boundary.
- The current DAM path requires one positive-edge clock, an `Empty` top, at least
  one annotated context, and at least two FIFO-connected workers.
- Annotations may be placed on ordinary modules or `(* synthesize *)` modules.
  Exposed FIFO methods are checked against their underlying primitives, guards,
  and scheduling dependencies. Nested/overlapping annotations are rejected.
- Worker count follows the annotations automatically. Optionally,
  `-dam-workers N` sets a hard cap including the main worker; exceeding it is an error.

This is a research prototype with a conservative set of supported primitives and
foreign effects. Unsupported constructs produce diagnostics. The full upstream
compatibility suite has not yet been run.

## Example: eight BF16 systolic arrays

The [example](dam/examples/systolic/SystolicBF16.bsv) splits a GEMM's K dimension
across eight physical 64×64 arrays. Each array produces a partial matrix; a ninth
context reduces those matrices into one 64×64 output tile.

The two module definitions carry the annotation:

```bsv
(* dam_context *)
module mkSystolicArray#(Integer shard, FIFO#(PartialRow) outputQ)(Empty);
    // 64 spatial rows, each containing 64 BF16 processing elements.
    // Send completed partial-result rows through outputQ.
endmodule

(* dam_context *)
module mkTerminalAdder#(Vector#(ArrayCount,FIFO#(PartialRow)) inputs)(Empty);
    // Consume one row from each array and perform a balanced BF16 reduction.
endmodule
```

These excerpts omit the bodies; the linked source is complete. The top instantiates
8 arrays, 8 ordinary depth-eight FIFOs, and the reducer. With `-dam`,
BSC checks those connections and creates exactly nine workers. The top contains no
rules, so no main worker is needed.

Parallel simulation makes sense when **each worker has considerable independent
work between FIFO exchanges**. Here, each systolic array performs substantial
local computation before sending completed result rows to the reducer. Running
those arrays concurrently can outweigh the overhead of starting workers and
communicating between processes.

Small contexts with little computation and frequent FIFO exchanges may instead
be slower than ordinary Bluesim. `dam_context` lets you choose these boundaries;
the compiler checks that they are valid, but does not predict a speedup.

For a 64×16384 by 16384×64 GEMM:

| Host | Pure Bluesim | DAM, 9 workers | Speedup |
|---|---:|---:|---:|
| Apple M5 Pro, macOS | 2.806 s | 0.526 s | **5.33×** |
| Linux (4× Xeon E7-8890 v3) | 7.497 s | 1.223 s | **6.13×** |

Medians of five runs after one warmup, including startup, with identical BSV and
`-O2` in both modes. The Mac has 18 logical CPUs; The Linux host has 72 physical cores and
144 logical CPUs. Runs were unpinned. Both modes execute every simulated clock
cycle; DAM runs the independent workers in parallel.
All partial/final values and complete FIFO/register cycle traces matched, including
runs with host scheduling jitter. Speedup depends on the design and chosen contexts.
See [methodology and reproduction](dam/examples/systolic/README.md) and
measurement samples for [macOS](dam/examples/systolic/measurements-generated.json)
and [Linux](dam/examples/systolic/measurements-linux.json).

## Build and run

The build helper supports Linux and macOS. It builds BSC, its libraries, Bluetcl,
and the DAM runtime from source. Install GHC with BSC's dependencies, Rust/Cargo,
Python 3, and the native build dependencies listed in the [build instructions](dam/README.md).

```sh
git clone --recurse-submodules https://github.com/const-antin/bsc-dam.git
cd bsc-dam
cd dam
cargo fetch --locked --manifest-path runtime/Cargo.toml
python3 scripts/build_compiler.py
make check-contexts
make check-synthesized
make systolic-eight-generated-benchmark
```

The compiler pass and driver are native Haskell; simulation uses C++ Bluesim workers
and the Rust DAM runtime. Python is used only for build/test and benchmark tooling.

Based on [B-Lang-org/bsc](https://github.com/B-Lang-org/bsc) commit
`63665af1d123270f508d1af0d775a7d243d7eb4f`. Compiler changes are in `src/comp/Dam*.hs`;
the runtime, tests, and example are under `dam/`. Upstream notices remain in
[COPYING](COPYING); DAM retains its MIT/Apache-2.0 licenses.
