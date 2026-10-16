"""Portable installer tests; these do not substitute for Linux CI execution."""
import os
import platform
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import build_compiler as build


class BuildTests(unittest.TestCase):
    def test_solver_links(self):
        for system, names in [('Linux', [('stp', 'libstp.so', 'libstp.so.1'), ('yices', 'libyices.so', 'libyices.so.2.6')]),
                              ('Darwin', [('stp', 'libstp.dylib', 'libstp.dylib'), ('yices', 'libyices.dylib', 'libyices.2.dylib')])]:
            with self.subTest(system=system), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp)/'source'
                lib = Path(tmp)/'installed/lib'
                (lib/'SAT').mkdir(parents=True)
                for sub, link, installed in names:
                    (lib/'SAT'/installed).write_text(sub)
                build.prepare_installed_libraries(source, lib, system)
                build.prepare_installed_libraries(source, lib, system)
                for sub, link, installed in names:
                    self.assertEqual((source/'src/vendor'/sub/'lib'/link).read_text(), sub)

    def test_missing_solver_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FileNotFoundError, 'Missing installed solver library'):
                build.prepare_installed_libraries(Path(tmp), Path(tmp)/'lib', 'Linux')

    def test_unsupported_host_is_diagnostic(self):
        with self.assertRaisesRegex(ValueError, 'Linux and macOS'):
            build.solver_libraries('Windows')

    def test_wrapper_paths_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lib = base/"compiler's $(false) libraries"
            wrapper = base/'bsc-dam'
            wrapper.write_text(build.compiler_wrapper(lib))
            wrapper.chmod(0o755)
            core = base/'bsc-dam-core'
            cpp = base/'core.cpp'
            cpp.write_text('#include <cstdlib>\n#include <cstdio>\n#include <initializer_list>\nint main(int argc, char** argv) {\n  for (auto key : {"BLUESPECDIR", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PATH"}) {\n    auto value = std::getenv(key); std::puts(value ? value : "");\n  }\n  for (int i=1; i<argc; ++i) std::puts(argv[i]);\n}\n')
            subprocess.run([*shlex.split(os.environ.get('CXX', 'c++')), '-std=c++17', str(cpp), '-o', str(core)], check=True)
            for override in [None, str(base/'alternate lib')]:
                with self.subTest(override=override):
                    env = dict(os.environ, LD_LIBRARY_PATH='/old/linux', DYLD_LIBRARY_PATH='/old/mac')
                    env.pop('BLUESPECDIR', None)
                    if override:
                        env['BLUESPECDIR'] = override
                    result = subprocess.check_output([wrapper, 'argument with spaces', '$literal'], env=env, text=True).splitlines()
                    chosen = override or str(lib)
                    # macOS SIP removes incoming DYLD variables when /bin/sh starts.
                    dyld = chosen+'/SAT'+('' if platform.system() == 'Darwin' else ':/old/mac')
                    self.assertEqual(result[:3], [chosen, chosen+'/SAT:/old/linux', dyld])
                    self.assertTrue(result[3].startswith(chosen+'/../bin:'))
                    self.assertEqual(result[4:], ['argument with spaces', '$literal'])

    def test_default_build_includes_libraries_and_bluetcl(self):
        with patch('sys.argv', ['build_compiler.py', '--ghc', '/bin/sh']), \
             patch.object(build.platform, 'system', return_value='Linux'), \
             patch.object(build, 'run') as run, patch.object(build, 'install_binary') as install:
            build.main()
            calls = [c.args[0] for c in run.call_args_list]
            self.assertIn('install-src', calls[0])
            self.assertIn('prepare-skip', calls[1])
            install.assert_called_once_with(build.ROOT.parent, build.ROOT.parent/'inst/lib')


if __name__ == '__main__':
    unittest.main()
