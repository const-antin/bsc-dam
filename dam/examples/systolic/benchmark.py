#!/usr/bin/env python3
"""Test/measurement harness; BSC itself enforces all requested DAM contexts."""
import argparse,hashlib,json,os,platform,re,shutil,statistics,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark_utils import run,outputs
from test_utils import compiler_environment
from reference import prepare,evaluate
TOP='mkTensorParallelSystolic'

def parse(paths):
    vals,done=outputs(paths)
    rows=[]
    for path in paths:
        for line in path.read_text().splitlines():
            if line.startswith('ROW '):rows.append(tuple(map(int,line.split()[1:])))
    return vals,done,sorted(rows)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--k',type=int,nargs='+',default=[1024,4096])
    ap.add_argument('--max-k',type=int,default=4096)
    ap.add_argument('--arrays',type=int,choices=[2,4,8,16],default=2)
    ap.add_argument('--generated-inputs',action='store_true',help='Generate operands in BSV; requires exactly one compile-time K and reads no input files')
    ap.add_argument('--repeats',type=int,default=5)
    ap.add_argument('--skip-build',action='store_true');ap.add_argument('--build-only',action='store_true')
    ap.add_argument('--skip-verify',action='store_true');ap.add_argument('--verify-only',action='store_true')
    a=ap.parse_args();assert a.max_k%a.arrays==0 and a.arrays<=a.max_k<=32768*a.arrays
    assert all(k%a.arrays==0 and a.arrays<=k<=a.max_k for k in a.k)
    assert not a.generated_inputs or len(a.k)==1,'Generated inputs use one compile-time K per build'
    suffix=f'-generated-k{a.k[0]}' if a.generated_inputs else ''
    base=ROOT/'build/systolic'/f'arrays{a.arrays}-maxk{a.max_k}{suffix}';base.mkdir(parents=True,exist_ok=True)
    out=ROOT/'results/systolic'/base.name;out.mkdir(parents=True,exist_ok=True)
    env=compiler_environment()
    env.update(DAM_HEADLESS='1',DAM_SKIP_CLOCKS='0',DAM_WORKER_TIMEOUT_SECONDS='600')
    loader=ROOT/'lib/DAM/bluesim-headless';runtime=ROOT/'lib/DAM/dam-runtime'
    bsc=ROOT/'bin/bsc-dam';bundle=base/'sim.dam'
    prepare(base,a.max_k//a.arrays,a.arrays,a.k[0]//a.arrays if a.generated_inputs else None)
    if not a.skip_build:
        snapshot=base/'sources';snapshot.mkdir(exist_ok=True)
        for path in [Path(__file__),ROOT/'examples/systolic/reference.py',ROOT/'examples/systolic/SystolicBF16.bsv',base/'SystolicConfig.bsv']:
            shutil.copy2(path,snapshot/path.name)
        common=[bsc,'-sim','-p','+:'+str(ROOT/'examples/systolic'),'-bdir',base,'-simdir',base,'-info-dir',base,
                '-steps','100000000','-parallel-sim-link','2','+RTS','-K256m','-M24G','-RTS']
        if a.generated_inputs:common+=['-D','SYSTOLIC_GENERATED_INPUTS']
        print(f'Compiling {a.arrays} full 64x64 arrays',flush=True)
        run(common+['-u','-g',TOP,ROOT/'examples/systolic/SystolicBF16.bsv'],out/'compile.log',env,timeout=7200)
        assert 'can never fire' not in (out/'compile.log').read_text()
        print('Linking pure Bluesim',flush=True)
        run(common+['-e',TOP,'-Xc++','-O2','-o',base/'plain'],out/'plain-link.log',env,timeout=7200)
        print('Linking annotated DAM contexts',flush=True)
        run(common+['-e',TOP,'-dam','-dam-cycles',a.max_k//a.arrays+196,'-o',base/'sim'],out/'dam-link.log',env,timeout=14400)
        (out/'baseline-build.json').write_text(json.dumps(dict(compiler_sha256=hashlib.sha256((ROOT/'bin/bsc-dam-core').read_bytes()).hexdigest(),
            input_mode='bsv-generated' if a.generated_inputs else 'hex-files',flags=['-O2'],note='Both modes built from identical BSV with first-MAC initialization.'),indent=2)+'\n')
    if a.build_only:return
    header=(base/(TOP+'.h')).read_text()
    row_header=(base/'mkSystolicRow.h').read_text()
    per_row=len(re.findall(r'MOD_Reg<[^>]+> INST_accum_\d+;',row_header))
    row_counts={f'arrays_{s}':len(re.findall(r'MOD_mkSystolicRow INST_arrays_'+str(s)+r'_rows_\d+;',header)) for s in range(a.arrays)}
    pe_counts={name:count*per_row for name,count in row_counts.items()}
    assert per_row==64 and list(row_counts.values())==[64]*a.arrays,(per_row,row_counts)
    assert list(pe_counts.values())==[4096]*a.arrays,pe_counts
    if a.generated_inputs:
        assert 'MOD_RegFile' not in header,'Generated mode must not instantiate file-backed memories'
        assert not (base/'data').exists() and not (base/'config.hex').exists()
    manifest=json.loads((bundle/'manifest.json').read_text())
    required={c['instance']:c['partition'] for c in manifest['required_contexts']}
    assert set(required)=={*(f'arrays_{s}' for s in range(a.arrays)),'terminal'} and len(set(required.values()))==a.arrays+1,required
    assert len(manifest['partitions'])==a.arrays+1 and len(manifest['channels'])==a.arrays
    assert all(c['width']==1030 and c['depth']==8 for c in manifest['channels'])
    assert {c['name'] for c in manifest['channels']}=={f'partial_{i}' for i in range(a.arrays)}
    for channel in manifest['channels']:
        shard=int(channel['name'].rsplit('_',1)[1])
        assert channel['producer']==required[f'arrays_{shard}'] and channel['consumer']==required['terminal']
    for shard in range(a.arrays):
        p=next(p for p in manifest['partitions'] if p['id']==required[f'arrays_{shard}'])
        assert len(p['rules'])==129 and sum('advanceRow' in r for r in p['rules'])==64 and sum('emitRow' in r for r in p['rules'])==64,p['rules']
    comparator=ROOT/'build/compare-traces'
    if not a.skip_verify:run(['c++','-std=c++17','-O2',ROOT/'native/compare_traces.cpp','-o',comparator],out/'trace-checker-build.log',env)
    cases={}
    for k in a.k:
        shard_k=k//a.arrays;cycles=shard_k+196
        result=out/f'k{k}';result.mkdir(exist_ok=True)
        config=base/'config.hex'
        if not a.generated_inputs:config.write_text(f'{shard_k:04x}\n')
        oraclefile=result/'reference.json'
        if not oraclefile.exists():
            print('Independent BF16 and FP32 oracle, K=',k,flush=True)
            oraclefile.write_text(json.dumps(evaluate(base,shard_k,a.arrays,a.max_k//a.arrays if a.generated_inputs else None))+'\n')
        oracle=json.loads(oraclefile.read_text());expected=list(enumerate(oracle['output_bits']))
        ver=result/'verification';ver.mkdir(exist_ok=True)
        if not a.skip_verify:
            print('Verifying K=',k,flush=True)
            run([loader,base/'plain.so',TOP,cycles],ver/'plain.log',env,timeout=1800)
            golden=parse([ver/'plain.log'])
            assert golden[0]==expected,(golden[0][:8],expected[:8])
            assert golden[1]==[shard_k+190] and len(golden[2])==64
            assert all(r==i and s==1 for i,(r,s,t) in enumerate(golden[2]))
            assert all(t==shard_k+127+i for i,(r,s,t) in enumerate(golden[2]))
            reset_baseline=result/'plain-preflight.log'
            if reset_baseline.exists():assert parse([reset_baseline])==golden
            file_baseline=ROOT/'results/systolic'/f'arrays{a.arrays}-maxk{a.max_k}'/f'k{k}'/'verification/plain.log'
            if a.generated_inputs and file_baseline.exists():assert parse([file_baseline])==golden
            ve={**env,'DAM_CHANNELS':','.join(f'{c["name"]}:{c["width"]}' for c in manifest['channels']),
                'DAM_CYCLES':str(cycles),'DAM_TRACE':str(ver/'reference.trace'),
                'DAM_STATE_TRACE':str(ver/'reference.state'),'DAM_CHECK_STATE':'1'}
            run([loader,bundle/'reference-model.so',TOP,0],ver/'reference.log',ve,timeout=1800)
            assert parse([ver/'reference.log'])==golden
            # Independent check of all intermediate partial matrices, in
            # addition to final values and the exact reference/DAM traces.
            count=[0]*a.arrays
            for line in (ver/'reference.trace').read_text().splitlines():
                tick,name,action,value=line.split()
                if action!='enq':continue
                shard=int(name.rsplit('_',1)[1]);packed=int(value,0);row=packed>>1024
                want=sum(v<<(16*c) for c,v in enumerate(oracle['partial_bits'][shard][row*64:(row+1)*64]))
                assert packed&((1<<1024)-1)==want,(shard,row)
                count[shard]+=1
            assert count==[64]*a.arrays,count
            checks={}
            for mode,extra in [('dam',{}),('jitter',{'DAM_HOST_JITTER':'1'})]:
                target=ver/mode
                run([runtime,bundle/'manifest.json',bundle,target,cycles],ver/(mode+'.log'),{**ve,**extra},timeout=1800)
                assert parse(list(target.glob('worker-*.log')))==golden
                checks[mode]={suffix:json.loads(subprocess.check_output([str(comparator),str(ver/('reference'+suffix)),
                              *map(str,target.glob('worker-*'+suffix))],text=True)) for suffix in ['.trace','.state']}
            checks.update(passed=True,reset_model_outputs_and_cycles_match=reset_baseline.exists(),cycles=cycles,done=golden[1],row_cycles=golden[2],outputs=4096,
                          file_model_outputs_and_cycles_match=a.generated_inputs and file_baseline.exists(),
                          partial_outputs=4096*a.arrays,sha256=oracle['sha256'])
            (ver/'checks.json').write_text(json.dumps(checks,indent=2)+'\n')
            print('PASS: all values, partial sums, FIFO/register clocks and timestamps, K=',k,flush=True)
        checks=json.loads((ver/'checks.json').read_text());assert checks['passed']
        cases[k]=dict(result=result,cycles=cycles,oracle=oracle,checks=checks,golden=parse([ver/'plain.log']))
    if a.verify_only:return
    samples={k:{mode:[] for mode in ['bluesim','dam']} for k in a.k};warmups={k:{} for k in a.k}
    for rep in range(a.repeats+1):
        for k in (a.k if rep%2==0 else list(reversed(a.k))):
            c=cases[k]
            if not a.generated_inputs:(base/'config.hex').write_text(f'{k//a.arrays:04x}\n')
            for mode in (['bluesim','dam'] if rep%2==0 else ['dam','bluesim']):
                log=c['result']/f'{mode}-{rep}.log';target=c['result']/'timed-dam'
                command=[loader,base/'plain.so',TOP,c['cycles']] if mode=='bluesim' else [runtime,bundle/'manifest.json',bundle,target,c['cycles']]
                row=run(command,log,{**env,'DAM_NO_TRACE':'1'},timeout=1800)
                assert parse([log] if mode=='bluesim' else list(target.glob('worker-*.log')))==c['golden']
                if mode=='dam':
                    report=json.loads((target/'dam-report.json').read_text())
                    row['workers']=report['workers']
                    for key in ['executed_cycles','skipped_clock_cycles','exchanges','batched_clock_cycles','wire_bytes','pending_polls']:
                        row[key]=sum(w[key] for w in report['workers'])
                    assert row['skipped_clock_cycles']==0
                    row['startup_max']=max(w['startup_wall_seconds'] for w in report['workers'])
                if rep:samples[k][mode].append(row)
                else:warmups[k][mode]=row
                (out/'samples.json').write_text(json.dumps(samples,indent=2)+'\n')
                print(k,mode,rep,round(row['wall'],6),'s, CPU',round(row['cpu'],6),flush=True)
    rows=[]
    for k,c in cases.items():
        med={m:{v:statistics.median(s[v] for s in ss) for v in ['wall','cpu']} for m,ss in samples[k].items()}
        rows.append(dict(k=k,macs=64*64*k,checks=c['checks'],medians=med,speedup=med['bluesim']['wall']/med['dam']['wall'],
                         first8=c['oracle']['output'][:8],errors_vs_fp32=c['oracle']['errors_vs_fp32'],
                         errors_vs_exact_bf16_inputs=c['oracle']['errors_vs_exact_bf16_inputs'],samples=samples[k],warmups=warmups[k]))
    paths=[Path(__file__),ROOT/'examples/systolic/SystolicBF16.bsv',ROOT/'examples/systolic/reference.py',base/'SystolicConfig.bsv',
           base/'plain.so',bundle/'manifest.json',ROOT/'bin/bsc-dam-core',runtime,loader]+sorted(bundle.glob('partition-*-model.so'))+sorted((base/'data').glob('*.hex'))
    summary=dict(input_mode='bsv-generated' if a.generated_inputs else 'hex-files',baseline_build=json.loads((out/'baseline-build.json').read_text()) if (out/'baseline-build.json').exists() else None,rows=rows,shape=dict(m=64,n=64,max_k=a.max_k,arrays=a.arrays,reduction_tree_levels=a.arrays.bit_length()-1,pes_per_array=4096,contexts=required,generated_accumulators=pe_counts),repeats=a.repeats,
                 host=dict(platform=platform.platform(),logical_cpus=os.cpu_count(),
                           cpu=subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string'],text=True).strip() if sys.platform=='darwin' else platform.processor()),
                 files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                 notes=f'Same BSV and -O2; native headless loaders both sides. BF16 operands, separately rounded multiply/add and balanced BF16 reduction tree. {a.arrays} K shards; '+
                 ('BSV counter-based synthetic operands, no input/config files or memories; ' if a.generated_inputs else 'local banked memories; ')+
                 f'no HBM/bus model. Each fresh process computes one 64x64 output tile. Startup, initialization, output and reporting included. Clock skipping disabled. All builds and full trace verification finish before alternating warmup + repeated timing. Required annotations enforce exactly {a.arrays+1} explicit contexts.')
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps([{k:r[k] for k in ['k','medians','speedup','errors_vs_fp32','first8']} for r in rows],indent=2),flush=True)
if __name__=='__main__':main()
