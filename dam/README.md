# Build and test

The [main README](../README.md) describes the extension and its requirements.

## Prerequisites

The build helper supports Linux and macOS. It needs GHC (CI uses 9.6.7), Cabal,
Rust/Cargo, Python 3, and a C/C++ toolchain. See the
[upstream prerequisites](../INSTALL.md) for platform-specific tool installation.
On Ubuntu, install the native dependencies with:

```sh
sudo apt-get update
sudo apt-get install -y build-essential tcl-dev libgmp-dev pkg-config autoconf \
    gperf flex bison iverilog git python3
```

With GHC and Cabal on `PATH`, install the Haskell dependencies:

```sh
cabal update
cabal v1-install old-time regex-compat split strict-concurrency syb
```

From the fork root:

```sh
git submodule update --init --recursive
cd dam
cargo fetch --locked --manifest-path runtime/Cargo.toml
python3 scripts/build_compiler.py --jobs 4
```

This builds the compiler, solvers, Bluetcl, BSV libraries and Bluesim into `../inst`,
then installs `bin/bsc-dam` and the DAM runtime into `lib/DAM`. No existing BSC
installation or system-wide installation is required. The wrapper locates its
libraries and Bluetcl automatically; test and benchmark tooling use the same paths.

To reuse a matching BSC 2026.07.1 library/SAT installation instead:

```sh
python3 scripts/build_compiler.py --bluespec-lib /absolute/path/to/bsc/lib
```

Use `--ghc` and `--package-db` to select an existing GHC toolchain. Build from a
separate checkout on each OS/architecture; native object files are not portable.
`make prepare-skip BSC_LIB=/absolute/path/to/bsc/lib` refreshes the runtime after
bridge changes without rebuilding the compiler.

## Validation

```sh
make check-build     # platform library lookup and compiler wrapper
make check-headless  # native loader and DAM cycle equivalence
make check-contexts  # annotation boundaries, the single main worker, cycle equivalence
make check-synthesized # synthesized FIFO interfaces and invalid boundary checks
make check-graph     # dependency-graph routines against independent reference algorithms
make check-transport # wide/fragmented frames and invalid input handling
make check-traces    # exact trace merging, timestamps, duplicates, and malformed input
```

The context tests compare outputs and FIFO/register timestamps with the optional
idle-cycle optimization off/on and with host scheduling jitter. They also test ordinary Bluesim compilation and rejected
boundaries. Python is blocked inside compiler/simulator subprocesses during these
checks. The [systolic benchmark](examples/systolic/README.md) adds an independent
BF16 numerical oracle and full traces for the spatial datapath.

The [Linux CI workflow](../.github/workflows/dam-linux.yml) is configured to build
from source on Ubuntu 22.04 and 24.04 x86-64 and Ubuntu 24.04 ARM64, then run these
checks plus upstream smoke tests. It retains diagnostics as workflow artifacts.

A full source build and all checks above passed on Ubuntu 22.04.5 x86-64 with
GHC 9.6.7, GCC 10.5 and Rust 1.92.0, including the upstream Bluesim smoke test.
The eight-array systolic example also passed numerical and complete cycle/trace
checks at K=128 and K=16384, including host jitter. The
[full Linux benchmark](examples/systolic/README.md) measured a 6.13× speedup.
The Ubuntu 24.04 and ARM64 jobs have not yet been run.

## Synthesized contexts

`(* synthesize, dam_context *)` modules may expose registered FIFO interfaces.
At simulation link time, BSC follows method bodies and ready predicates through
synthesized children to the actual primitive instances. FIFO widths/depths and
producer/consumer ownership come from those instances, not interface names.
The original Bluesim hierarchy and global schedule remain intact.

Calls that expose non-FIFO state or introduce cross-context scheduling dependencies
are rejected, as are crossing bypass/pipeline FIFOs and FIFO `clear` operations.
Overlapping annotations remain invalid. This hierarchy path currently accepts
clock/reset constructor ports only. Its workers execute every simulated clock
cycle, including cycles spent waiting for FIFO data. Independent workers still
run in parallel, and batching reduces how often they communicate.

Worker count is inferred from explicit annotations plus the nonempty main worker.
`-dam-workers N` is an optional upper bound, with no default cap.

## Runtime controls

`DAM_HEADLESS=1` selects the native loader instead of Tcl. `DAM_NO_TRACE=1`
disables diagnostic tracing for measurements. `DAM_BATCH_CYCLES` limits clocks
per transport exchange (default 4096); batching executes every clock.
`DAM_SKIP_CLOCKS=1` enables an optional optimization called *clock skipping*: a
worker can jump over idle cycles when the compiler proves that no rule can fire
and its state cannot change before new FIFO data arrives. This optimization is
currently unavailable for the synthesized-interface path described above.
`DAM_WORKER_TIMEOUT_SECONDS` bounds startup and socket waits.

The systolic harness uses the headless loader and executes every simulated clock
cycle.
Workers currently allocate the complete model and execute only their assigned
schedules. Manifest schema 3 records explicit contexts, an optional main worker,
and protocol version 5; older bundles must be rebuilt.

## Source layout

- `../src/comp/DamPartition.hs`: ownership and FIFO/scheduling legality checks.
- `../src/comp/DamHierarchy.hs`: resolves synthesized methods to primitive accesses.
- `../src/comp/DamNecessity.hs`: necessary input conditions for safe clock skipping.
- `../src/comp/DamPlan.hs`: typed compiler/driver handoff.
- `../src/comp/DamDriver.hs`: native worker generation, linking, and verification.
- `native/` and `runtime/`: C++ Bluesim bridge and Rust DAM coordinator.
- `tests/`: small compiler regression fixtures; `examples/systolic/`: the benchmark.

Generated models, binaries, and trace logs are excluded from version control.
