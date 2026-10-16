#!/usr/bin/env python3
"""Shared development-only timing and trace comparison helpers."""
import argparse,json,os,platform,resource,statistics,subprocess,time,hashlib,signal,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def run(args,log,env,timeout=1800):
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w') as f:
        load_before=os.getloadavg();start=time.perf_counter();before=resource.getrusage(resource.RUSAGE_CHILDREN)
        child=subprocess.Popen(list(map(str,args)),cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
        def kill():
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        timer=threading.Timer(timeout,kill);timer.daemon=True;timer.start()
        try:rc=child.wait()
        finally:timer.cancel()
        elapsed=time.perf_counter()-start;after=resource.getrusage(resource.RUSAGE_CHILDREN)
    if rc:raise RuntimeError(f'Command exited {rc}: see {log}')
    return dict(load_before=load_before,load_after=os.getloadavg(),wall=elapsed,
                cpu=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
                user_cpu=after.ru_utime-before.ru_utime,system_cpu=after.ru_stime-before.ru_stime)
def outputs(paths):
    vals=[];done=[]
    for p in paths:
        for line in p.read_text().splitlines():
            if line.startswith('OUTPUT '):
                _,i,v=line.split();vals.append((int(i),int(v,16)))
            if line.startswith('DONE '):done.append(int(line.split()[1]))
    return sorted(vals),done

def compare_files(expected,files,folder):
    # External sort bounds RAM for full-size cycle/register traces.
    a=folder/'expected.sorted';b=folder/'observed.sorted'
    with a.open('w') as f:subprocess.run(['sort',str(expected)],stdout=f,check=True,env={**os.environ,'LC_ALL':'C'})
    with b.open('w') as f:subprocess.run(['sort',*map(str,files)],stdout=f,check=True,env={**os.environ,'LC_ALL':'C'})
    def digest(p):
        h=hashlib.sha256();count=0
        with p.open('rb') as f:
            for line in f:h.update(line);count+=1
        return h.hexdigest(),count
    da,db=digest(a),digest(b)
    assert da==db,(expected,da,db)
    a.unlink();b.unlink();return dict(sha256=da[0],events=da[1])
