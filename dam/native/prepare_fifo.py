#!/usr/bin/env python3
"""Generate an instrumented copy of the installed FIFO primitive header.

The ordinary branch retains Bluesim's implementation for the reference/local
FIFOs. Only compiler-identified boundary FIFOs use the remote handshake branch.
"""
from pathlib import Path
import sys

source, dest = map(Path, sys.argv[1:])
text = source.read_text()
def replace(old,new):
    global text
    assert text.count(old)==1, old
    text=text.replace(old,new)
# This header is force-included before the generated model's reset header.
# GCC requires the reset functions to be declared when parsing the template.
replace('#include <cstdio>', '#include <cstdio>\n#include "bs_reset.h"\n#include "dam_bits.h"')
replace('    data = new T[depth];', '    dam_path = dam_full_name(this);\n    dam_name = dam_path.c_str();\n    init_val(dam_value, width);\n    data = new T[depth];')
replace('  const T& METH_first() const { return data[fst]; }', '''  const T& METH_first() const {
    if (dam_remote(dam_name)) { dam_unpack(dam_front(dam_name), bits, dam_value); return dam_value; }
    return data[fst];
  }''')
replace('    deq_at = bk_now(sim_hdl);', '''    if (dam_remote(dam_name)) { dam_dequeue(dam_name, dam_front(dam_name)); return; }
    dam_dequeue(dam_name, dam_pack(data[fst], bits));
    deq_at = bk_now(sim_hdl);''')
replace('    enq_at = bk_now(sim_hdl);', '''    dam_enqueue(dam_name, dam_pack(x, bits));
    if (dam_remote(dam_name)) return;
    enq_at = bk_now(sim_hdl);''')
replace('bool METH_notFull() const  { return (elems < size); }', 'bool METH_notFull() const { return dam_remote(dam_name) ? dam_ready(dam_name) : (elems < size); }')
replace('bool METH_notEmpty() const { return (elems != 0); }', 'bool METH_notEmpty() const { return dam_remote(dam_name) ? dam_valid(dam_name) : (elems != 0); }')
replace('  bool METH_i_notFull() const\n  {', '  bool METH_i_notFull() const\n  {\n    if (dam_remote(dam_name)) return dam_ready(dam_name);')
replace('  bool METH_i_notEmpty() const\n  {', '  bool METH_i_notEmpty() const\n  {\n    if (dam_remote(dam_name)) return dam_valid(dam_name);')
replace('      METH_clear();', '      dam_reset_fifo(dam_name);\n      METH_clear();')
replace('class MOD_Fifo : public Module\n{', 'class MOD_Fifo : public Module\n{\n  std::string dam_path;\n  const char* dam_name;\n  mutable T dam_value;')
dest.write_text(text)
