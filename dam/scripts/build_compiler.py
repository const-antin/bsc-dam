#!/usr/bin/env python3
"""Build BSC and DAM on Linux/macOS, optionally reusing an installed BSC library."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def solver_libraries(system):
    if system == 'Linux':
        return [('stp', 'libstp.so', 'libstp.so.1'),
                ('yices', 'libyices.so', 'libyices.so.2.6')]
    if system == 'Darwin':
        return [('stp', 'libstp.dylib', 'libstp.dylib'),
                ('yices', 'libyices.dylib', 'libyices.2.dylib')]
    raise ValueError('DAM builds currently support Linux and macOS')


def link_if_missing(link, target):
    if link.exists():
        return
    if link.is_symlink():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def prepare_installed_libraries(source, lib, system):
    for sub, name, original in solver_libraries(system):
        target = lib/'SAT'/original
        if not target.is_file():
            raise FileNotFoundError(f'Missing installed solver library: {target}')
        link_if_missing(source/'src/vendor'/sub/'lib'/name, target)
    for path, target in [('stp/include_hs', 'HaskellIfc'),
                         ('yices/include_hs', 'v2.6/HaskellIfc')]:
        link_if_missing(source/'src/vendor'/path, target)


def compiler_wrapper(lib):
    # Both variables are supported by the upstream wrapper too. The native
    # loader uses the relevant one; preserve any user-supplied search paths.
    return '\n'.join([
        '#!/bin/sh', 'set -eu',
        'HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
        'if [ -z "${BLUESPECDIR:-}" ]; then',
        '  BLUESPECDIR=' + shlex.quote(str(lib)), 'fi',
        'export BLUESPECDIR',
        'export PATH="$BLUESPECDIR/../bin:$PATH"',
        'export LD_LIBRARY_PATH="$BLUESPECDIR/SAT${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"',
        'export DYLD_LIBRARY_PATH="$BLUESPECDIR/SAT${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"',
        'exec "$HERE/bsc-dam-core" "$@"', ''])


def install_binary(source, lib):
    binary = ROOT/'bin'
    binary.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source/'src/comp/bsc', binary/'bsc-dam-core')
    wrapper = binary/'bsc-dam'
    wrapper.write_text(compiler_wrapper(lib))
    wrapper.chmod(0o755)
    # No Python is invoked by the installed compiler or simulator.
    runtime = ROOT/'lib/DAM'
    (runtime/'include').mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT/'bin/dam-runtime', runtime/'dam-runtime')
    shutil.copy2(ROOT/'build/dam_skip_runtime.o', runtime/'dam_runtime.o')
    for path in list((ROOT/'native').glob('*.h')) + [ROOT/'build/dam_fifo.h', ROOT/'build/dam_reg.h', ROOT/'build/dam_regfile.h']:
        shutil.copy2(path, runtime/'include'/path.name)
    (runtime/'build-config.json').write_text(json.dumps({'bluespec_lib': str(lib)}, indent=2)+'\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bluespec-lib', type=Path,
                   help='reuse a matching BSC library/SAT installation; otherwise build everything into SOURCE/inst')
    p.add_argument('--ghc', default='ghc')
    p.add_argument('--package-db', type=Path)
    p.add_argument('--source', type=Path)
    p.add_argument('--jobs', type=int, default=4)
    args = p.parse_args()
    if args.jobs < 1:
        p.error('--jobs must be positive')
    system = platform.system()
    try:
        solver_libraries(system)
    except ValueError as e:
        p.error(str(e))
    source = (args.source or ROOT.parent).resolve()
    if not (source/'src/comp/DamPartition.hs').exists():
        p.error('--source must point to this DAM-enabled BSC fork')
    ghc_path = Path(shutil.which(args.ghc) or args.ghc).resolve()
    ghc = shlex.quote(str(ghc_path))
    env = dict(os.environ, PATH=str(ghc_path.parent)+os.pathsep+os.environ['PATH'])
    if args.package_db:
        db = str(args.package_db.resolve())
        ghc += ' -package-db '+shlex.quote(db)
        env['GHC_PACKAGE_PATH'] = db+os.pathsep+env.get('GHC_PACKAGE_PATH', '')
    options = ['BSC_BUILD=NOOPT', f'GHCJOBS={args.jobs}', 'GHC='+ghc]
    if args.bluespec_lib:
        lib = args.bluespec_lib.resolve()
        prepare_installed_libraries(source, lib, system)
        run(['make', '-C', source/'src/vendor/htcl', 'install', 'GHC='+ghc], env=env)
        run(['make', '-C', source/'src/comp', 'bsc', *options], env=env)
    else:
        lib = source/'inst/lib'
        # Upstream builds the solvers, compiler, Bluetcl, BSV libraries and
        # Bluesim kernel in dependency order. No preinstalled BSC is needed.
        run(['make', '-C', source, '-j'+str(args.jobs), 'install-src', *options], env=env)
    run(['make', 'prepare-skip', 'BSC_SOURCE='+str(source), 'BSC_LIB='+str(lib)], cwd=ROOT, env=env)
    install_binary(source, lib)
    print(f'Built {ROOT / "bin/bsc-dam"} using {lib}')


if __name__ == '__main__':
    main()
