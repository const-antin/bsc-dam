#!/usr/bin/env python3
"""Build a restricted clock-skipping kernel alongside, not over, stock Bluesim."""
import argparse
import os
import shlex
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    args = p.parse_args()
    src = args.source.resolve() / 'src/bluesim'
    out = ROOT / 'build/skip-kernel'
    out.mkdir(parents=True, exist_ok=True)
    reg = (src/'bs_prim_mod_reg.h').read_text()
    reg = reg.replace('#define __BS_PRIM_MOD_REG_H__', '#define __BS_PRIM_MOD_REG_H__\n#include "dam_bits.h"')
    old = '''    if ((reset_type != ASYNC_RESET_REG) || !suppress_write)
      value = x;'''
    assert reg.count(old) == 1
    reg = reg.replace(old, '''    if ((reset_type != ASYNC_RESET_REG) || !suppress_write) {
      value = x;
      if (dam_state_tracing()) dam_reg_write(dam_full_name(this).c_str(), dam_pack(x, bits), bk_now(sim_hdl));
    }''')
    (ROOT/'build/dam_reg.h').write_text(reg)
    regfile=(src/'bs_prim_mod_regfile.h').read_text()
    regfile='#include "dam_bits.h"\n'+regfile
    assert regfile.count('    init_from_file(memfile, bin_format);')==1
    regfile=regfile.replace('    init_from_file(memfile, bin_format);',
        '    if (dam_memory_needed(dam_full_name(this).c_str())) init_from_file(memfile, bin_format);')
    (ROOT/'build/dam_regfile.h').write_text(regfile)
    for name in ('event_queue.h', 'event_queue.cxx', 'kernel.cxx'):
        text = (src / name).read_text()
        if name == 'event_queue.h':
            text = text.replace('  // debugging utility function', '''  // Only the currently executing positive clock edge may exist. No pending
  // resets, UI events, after-edge callbacks, or other clocks may be skipped.
  bool skip_clock(tEventFn expected, tTime delta);

  // debugging utility function''')
        elif name == 'event_queue.cxx':
            text += '''
bool EventQueue::skip_clock(tEventFn expected, tTime delta)
{
  if (!in_event || removed_self || count != 0 ||
      executing_event.fn != expected || executing_event.data.value != 1 ||
      priority_group(executing_event.priority) != PG_LOGIC ||
      delta > ~tTime(0) - executing_event.at) return false;
  executing_event.at += delta;
  return true;
}
'''
        else:
            text += '''
// Called at the beginning of the positive-edge schedule, before evaluating
// any rule. A zero-cycle query is also used to advertise safe capability.
extern "C" bool bk_dam_skip_cycles(void* handle, unsigned long long cycles)
{
  tSimStateHdl s = static_cast<tSimStateHdl>(handle);
  if (!s || !s->queue || s->clocks.size() != 1 || s->reset_tick_requests ||
      s->in_combo_schedule || s->need_dummy_edges || s->force_halt ||
      s->call_dump_state || s->call_dump_cycle_counts || s->vcd.state != VCD_OFF)
    return false;
  tClockInfo& c = s->clocks[0];
  if (!c.period || !c.on_posedge || c.on_negedge || c.after_posedge ||
      c.after_negedge || c.posedge_limit || c.negedge_limit ||
      c.initial_value != CLK_LOW || c.current_value != CLK_HIGH || c.posedge_at != s->sim_time ||
      cycles > (~tTime(0) - s->sim_time) / c.period ||
      cycles > ~tUInt64(0) - c.posedge_count) return false;
  tTime delta = cycles * c.period;
  if (!s->queue->skip_clock(run_edge_schedule_event, delta)) return false;
  s->sim_time += delta;
  c.posedge_at += delta;
  c.combinational_at += delta;
  c.posedge_count += cycles;
  // No negative-edge callbacks exist in this restricted configuration, so
  // their counters stay unchanged exactly as in ordinary Bluesim.
  return true;
}

extern "C" bool bk_dam_check_clock(void* handle, unsigned long long cycle)
{
  tSimStateHdl s = static_cast<tSimStateHdl>(handle);
  if (!s || s->clocks.size() != 1) return false;
  const tClockInfo& c = s->clocks[0];
  return c.initial_value == CLK_LOW && c.posedge_count == cycle + 1 &&
         s->sim_time == c.initial_delay + cycle * c.period &&
         c.posedge_at == s->sim_time;
}
'''
        (out / name).write_text(text)
    # kernel.h must include our extended EventQueue declaration.
    (out / 'kernel.h').write_bytes((src / 'kernel.h').read_bytes())
    version = subprocess.check_output([str(src/'gen_version_h'), str(src.parent/'comp/BuildVersion.hs')])
    (out / 'version.h').write_bytes(version)
    objects = []
    for source in (out/'kernel.cxx', out/'event_queue.cxx', ROOT/'native/dam_runtime.cpp'):
        obj = out / (source.stem + '.o')
        subprocess.run([*shlex.split(os.environ.get('CXX', 'c++')),'-std=c++17','-O2','-fPIC','-fno-rtti','-DDAM_WITH_SKIP_KERNEL',
                        '-I'+str(out),'-I'+str(src),'-c',str(source),'-o',str(obj)], check=True)
        objects.append(str(obj))
    # Strong definitions prevent the corresponding objects in the stock
    # static kernel archive from being pulled into the final model.
    subprocess.run([*shlex.split(os.environ.get('LD', 'ld')),'-r','-o',str(ROOT/'build/dam_skip_runtime.o'),*objects],check=True)
    print('Built bridge with restricted Bluesim clock-skipping kernel')

if __name__ == '__main__':
    main()
