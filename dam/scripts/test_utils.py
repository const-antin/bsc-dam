"""Subprocess helper for compiler regression tests."""
import json, os, signal, subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def run(args, *, env=None, log=None, timeout=120):
    args = [str(a) for a in args]
    with open(log, "w") if log else open(os.devnull, "w") as output:
        p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise RuntimeError(f"Timed out: {args}; see {log}")
        if rc:
            raise RuntimeError(f"Command failed ({rc}): {args}; see {log}")


def compiler_environment():
    """Use this build's library and Bluetcl, with an explicit environment override."""
    env = {k:v for k,v in os.environ.items() if not k.startswith(('DAM_', 'BSC_DAM_'))}
    config = ROOT/'lib/DAM/build-config.json'
    lib = env.get('BLUESPECDIR')
    if not lib:
        lib = json.loads(config.read_text())['bluespec_lib'] if config.exists() else str(ROOT.parent/'inst/lib')
    env['BLUESPECDIR'] = lib
    env['PATH'] = str(Path(lib).parent/'bin')+os.pathsep+env['PATH']
    return env
