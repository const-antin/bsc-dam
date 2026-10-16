# Eight BF16 systolic arrays

This example computes one 64×64 output tile of a GEMM by splitting K across eight
64×64 systolic arrays. Each array is annotated with `(* dam_context *)`; a ninth
annotated module reduces their partial matrices through balanced BF16 adder trees.
The top only connects FIFOs, so it requires no main worker.

## Datapath

Each of the 32,768 PEs has its own accumulator and registered east/south operand
paths. All rows operate spatially in parallel. `(* synthesize *)` on each row
preserves a reusable Bluesim hierarchy within its array's DAM context.

Inputs, multiplication, accumulation, and final reduction use `FloatingPoint#(8,7)`.
Multiply and add round separately to nearest, ties to even; this is not fused FMA.
The first valid MAC initializes each accumulator, avoiding a datapath reset tree.
Deterministic synthesizable BSV logic generates signed BF16 operands at the array
edges. This models computation with synthetic inputs, not SRAM or HBM traffic.

After computing, each array emits 64 rows through a depth-eight FIFO. A packet
contains a six-bit row index and 64 BF16 values (1,030 bits). The reducer consumes
matching rows from all eight arrays and uses three levels of BF16 addition.

## Recorded results

For A[64,16384] × B[16384,64], each array processes 2,048 K terms. The computation
contains 67,108,864 MACs and completes at simulated cycle 2238.

| Host | Pure Bluesim | DAM, 9 workers | Speedup |
|---|---:|---:|---:|
| Apple M5 Pro, macOS ARM64 | 2.806 s | 0.526 s | **5.33×** |
| Ubuntu 22.04 x86-64 | 7.497 s | 1.223 s | **6.13×** |

Measured on 2026-10-16. The Mac has an Apple M5 Pro with 18 logical CPUs;
The Linux host has four Xeon E7-8890 v3 sockets, totaling 72 physical cores and 144 logical
CPUs. These are five-run medians after one warmup, with alternating mode order.
Both modes use the same BSV, `-O2`, and native loader. Timings include process
startup, initialization, output, and reporting. Tracing is disabled during timing;
both modes execute every simulated clock cycle. Both hosts were shared and runs
were unpinned. The favorable mapping keeps arithmetic local and sends only 512 partial
rows between workers.

On Linux, the five runs ranged from 7.432–7.558 s for Bluesim and 1.211–1.236 s
for DAM. Both modes were built with GCC 10.5. Verification completed before timing.

All 32,768 partial values and 4,096 outputs match the independent BF16 oracle.
Pure Bluesim and DAM agree on output cycles, 201,346,748 register writes, and
1,024 FIFO events, including physical timestamps. Host-jitter checks also pass.
Relative L2 error versus FP32 is 5.3784%, due to BF16 accumulation; DAM adds no
numerical error. These checks cover the tested finite run, not arbitrary designs.

Raw samples and hashes identify the measured builds on
[macOS](measurements-generated.json) and [Linux](measurements-linux.json).
The macOS timings predate removal of the optional placement policy; this design
already used explicit annotations for every worker. The Linux run uses the current
compiler with strictly enforced explicit contexts. Earlier records for
[two arrays](measurements.json) and [memory-backed inputs](measurements-eight.json)
are retained separately.

## Reproduce

After [building the fork](../../README.md), from `dam/`:

```sh
make systolic-eight-generated-benchmark

# Recheck an existing build without timing:
python3 examples/systolic/benchmark.py --arrays 8 --max-k 16384 --k 16384 \
  --generated-inputs --skip-build --verify-only
```

The harness checks worker ownership, all 32,768 physical accumulators, numerical
results, and complete normal/jitter traces before timing. Builds and full trace
checks can take minutes and require tens of GB of temporary disk space.

`--arrays` supports 2, 4, 8, or 16. K must be a positive multiple of the array
count. Generated-input mode accepts one compile-time K per build; `--max-k`
defines the canonical shard offsets. `--skip-build`, `--verify-only`, and
`--repeats` control reuse and measurement. Omitting `--generated-inputs` selects
the earlier local-memory input implementation. Outputs are stored under
`build/systolic/` and `results/systolic/`.
