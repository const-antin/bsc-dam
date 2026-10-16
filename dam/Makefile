BSC_LIB ?= $(if $(BLUESPECDIR),$(BLUESPECDIR),$(abspath $(CURDIR)/../inst/lib))
BSC_DAM ?= $(CURDIR)/bin/bsc-dam
GHC ?= ghc
DL_LIBS = $(if $(filter Linux,$(shell uname -s)),-ldl,)

BSC_SOURCE ?= $(if $(wildcard $(CURDIR)/../src/comp/bsc.hs),$(abspath $(CURDIR)/..),$(CURDIR)/build/bsc-src)
.PHONY: prepare-skip
prepare-skip:
	mkdir -p build results bin
	python3 native/prepare_fifo.py "$(BSC_LIB)/Bluesim/bs_prim_mod_fifo.h" build/dam_fifo.h
	python3 native/prepare_kernel.py --source "$(BSC_SOURCE)"
	cargo build --release --offline --locked --manifest-path runtime/Cargo.toml
	cp runtime/target/release/bluesim-dam-pass-demo bin/dam-runtime
	mkdir -p lib/DAM/include
	cp bin/dam-runtime lib/DAM/dam-runtime
	cp build/dam_skip_runtime.o lib/DAM/dam_runtime.o
	cp native/*.h build/dam_fifo.h build/dam_reg.h build/dam_regfile.h lib/DAM/include/
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -std=c++17 -O2 -I"$(BSC_SOURCE)/src/bluesim" native/bluesim_headless.cpp $(LDFLAGS) -o build/bluesim-headless $(LDLIBS) $(DL_LIBS)
	cp build/bluesim-headless lib/DAM/bluesim-headless
	cp build/bluesim-headless bin/bluesim-headless


.PHONY: check-build check-headless check-contexts check-synthesized check-graph check-transport check-traces systolic-benchmark systolic-eight-benchmark systolic-eight-generated-benchmark
check-build:
	python3 -m unittest discover -s tests -p test_build.py
check-headless:
	python3 check_headless.py
check-contexts:
	python3 check_contexts.py
check-synthesized:
	python3 check_synthesized.py
check-graph:
	python3 check_graph.py --ghc "$(GHC)"
check-transport:
	python3 native/check_transport.py
check-traces:
	python3 native/check_trace_comparator.py
systolic-benchmark:
	python3 examples/systolic/benchmark.py
systolic-eight-benchmark:
	python3 examples/systolic/benchmark.py --arrays 8 --max-k 16384 --k 4096 16384
systolic-eight-generated-benchmark:
	python3 examples/systolic/benchmark.py --arrays 8 --max-k 16384 --k 16384 --generated-inputs
