#!/usr/bin/env python3
"""Independent scalar BF16 oracle for K-sharded GEMM, plus FP32 comparison."""
import hashlib,json,math,struct
from pathlib import Path

TABLE=[struct.unpack('<f',struct.pack('<I',i<<16))[0] for i in range(65536)]
def bits(x):
    raw=struct.unpack('<I',struct.pack('<f',x))[0]
    return ((raw+0x7fff+((raw>>16)&1))>>16)&0xffff
def bf(x):return TABLE[bits(x)]
def f32(x):return struct.unpack('<f',struct.pack('<f',x))[0]

def sample(matrix,row,col):
    z=(0x9e3779b9*(1+row)+0x85ebca6b*(1+col)+0xc2b2ae35*matrix)&0xffffffff
    z=((z^(z>>16))*0x7feb352d)&0xffffffff
    z=((z^(z>>15))*0x846ca68b)&0xffffffff
    z^=z>>16
    # Signed normal BF16 in [1/16,1/4), varying mantissas and exponents.
    return ((z>>16)&0x8000)|((123+((z>>8)&1))<<7)|(z&127)

def write_changed(path,text):
    if not path.exists() or path.read_text()!=text:path.write_text(text)

def prepare(base,max_shard,arrays=2,generated_k=None):
    if generated_k is not None:
        write_changed(base/'SystolicConfig.bsv',f'''package SystolicConfig;
typedef {arrays} ArrayCount;
Integer maxKShard={max_shard};
Integer activeKShard={generated_k};
endpackage
''')
        return
    data=base/'data';data.mkdir(parents=True,exist_ok=True)
    # Fixed disjoint K slices; smaller cases concatenate prefixes from
    # all shards while retaining the same loaded memory images.
    for shard in range(arrays):
        for kind in range(2):
            for lane in range(64):
                values=[sample(kind,lane,shard*max_shard+k) if kind==0 else
                        sample(kind,shard*max_shard+k,lane) for k in range(max_shard)]
                write_changed(data/f's{shard}-{kind}-{lane}.hex',''.join(f'{v:04x}\n' for v in values))
    config=base/'config.hex';config.write_text(f'{max_shard:04x}\n')
    write_changed(base/'SystolicConfig.bsv',f'''package SystolicConfig;
typedef {arrays} ArrayCount;
Integer maxKShard={max_shard};
String configFile="{config}";
function String bankFile(Integer shard,Integer kind,Integer lane);
    return "{data}/s"+integerToString(shard)+"-"+integerToString(kind)+"-"+integerToString(lane)+".hex";
endfunction
endpackage
''')

def evaluate(base,shard_k,arrays=2,generated_max_shard=None):
    matrices=[]
    for shard in range(arrays):
        matrices.append([[ [TABLE[sample(kind,lane,shard*generated_max_shard+k) if kind==0 else
                                  sample(kind,shard*generated_max_shard+k,lane)] for k in range(shard_k)]
                         if generated_max_shard is not None else
                         [TABLE[int(v,16)] for v in (base/'data'/f's{shard}-{kind}-{lane}.hex').read_text().splitlines()[:shard_k]]
                         for lane in range(64)] for kind in range(2)])
    partials=[[] for _ in range(arrays)];exact=[];fp32=[];out=[]
    for r in range(64):
        for c in range(64):
            exact_parts=[];fp=0.
            for shard in range(arrays):
                a,b=matrices[shard];acc=0.;products=[]
                for k in range(shard_k):
                    product=a[r][k]*b[c][k]
                    acc=bf(acc+bf(product))
                    fp=f32(fp+product)
                    products.append(product)
                partials[shard].append(acc);exact_parts.append(math.fsum(products))
            level=[part[-1] for part in partials]
            while len(level)>1:
                level=[bf(level[i]+level[i+1]) for i in range(0,len(level),2)]
            out.append(level[0])
            exact.append(math.fsum(exact_parts));fp32.append(fp)
    output_bits=list(map(bits,out))
    error=lambda ref:dict(relative_l2=math.sqrt(sum((a-b)**2 for a,b in zip(out,ref))/sum(b*b for b in ref)),
                         max_abs=max(abs(a-b) for a,b in zip(out,ref)),rmse=math.sqrt(sum((a-b)**2 for a,b in zip(out,ref))/4096))
    return dict(k_per_array=shard_k,arrays=arrays,k=arrays*shard_k,output_bits=output_bits,output=out,
                partial_bits=[list(map(bits,part)) for part in partials],fp32=fp32,
                errors_vs_fp32=error(fp32),errors_vs_exact_bf16_inputs=error(exact),
                sha256=hashlib.sha256(struct.pack('<4096H',*output_bits)).hexdigest())
