#!/usr/bin/env python3
"""Exercise real C++ bridge framing, including fragmented and malformed input."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAGIC = 0x44414D33
WIDTHS = [0, 1, 31, 32, 33, 129, 1024, 8193]


def main():
    with tempfile.TemporaryDirectory(prefix='dam-transport-', dir='/tmp') as tmp:
        folder = Path(tmp)
        harness = folder / 'harness.cpp'
        harness.write_text('''#include "dam_runtime.h"
int main() {
    const char* names[] = {"c0", "c1", "c2", "c3", "c4", "c5", "c6", "c7"};
    for (;;) {
        dam_begin_cycle(nullptr);
        for (auto name : names) {
            if (dam_valid(name) && dam_ready(name)) {
                dam_enqueue(name, dam_front(name));
                dam_dequeue(name, dam_front(name));
            }
        }
        dam_end_cycle();
    }
}
''')
        exe = folder / 'harness'
        subprocess.run(['c++', '-std=c++17', '-O2', '-I' + str(ROOT / 'native'),
                        str(harness), str(ROOT / 'native/dam_runtime.cpp'), '-o', str(exe)], check=True)

        def exchange(mode, sparse=False, batched=False):
            path = str(folder / (mode + ('-batched' if batched else '-sparse' if sparse else '-dense') + '.sock'))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path)
                listener.listen(1)
                listener.settimeout(10)
                env = {**os.environ, 'DAM_SOCKET': path, 'DAM_TRACE': '/dev/null',
                       'DAM_CYCLES': '3', 'DAM_CHANNELS': ','.join(f'c{i}:{w}' for i, w in enumerate(WIDTHS))}
                env.pop('DAM_STATE_TRACE', None)
                env['DAM_TRANSPORT'] = 'batched' if batched else 'sparse' if sparse else 'dense'
                child = subprocess.Popen([exe], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(10)
                        for cycle in range(3):
                            request = [MAGIC + int(sparse)+int(batched), cycle, len(WIDTHS)]
                            if batched:request.append(cycle if mode!='bad-horizon' else 3)
                            expected = [MAGIC + int(sparse)+int(batched), cycle, len(WIDTHS), 0, 0, 0]
                            for i, width in enumerate(WIDTHS):
                                n = (width + 31) // 32
                                payload = [(0xFEDCBA98 ^ (cycle * 91 + j)) & 0xFFFFFFFF for j in range(n)]
                                if width % 32:
                                    payload[-1] &= (1 << (width % 32)) - 1
                                valid = not sparse or cycle != 1 or i % 2 == 0
                                ready = not sparse or cycle != 2
                                request.extend([width, int(ready), int(valid), *(payload if valid or not sparse else [])])
                                transferred = valid and ready
                                expected.extend([width, int(transferred), int(transferred), *(payload if transferred or not sparse else [])])
                            if mode == 'bad-magic':
                                request[0] ^= 1
                            if mode == 'bad-handshake':
                                request[4+int(batched)] = 2
                            if mode == 'bad-padding':
                                # Channel zero has no payload; channel one has one bit.
                                request[9+int(batched)] |= 2
                            wire = struct.pack('<' + 'I' * len(request), *request)
                            if sparse:
                                wire = struct.pack('<I', len(wire) + (4 if mode == 'bad-length' else 0)) + wire
                            if mode == 'truncated':
                                conn.sendall(wire[:-1])
                                conn.shutdown(socket.SHUT_WR)
                                break
                            if mode == 'fragmented':
                                # Fragments deliberately cross word and channel boundaries.
                                for offset in range(0, len(wire), 7):
                                    conn.sendall(wire[offset:offset + 7])
                            else:
                                conn.sendall(wire)
                            if mode.startswith('bad-'):
                                break
                            if sparse:
                                expected = [4*len(expected)] + expected
                            reply = bytearray()
                            while len(reply) < 4 * len(expected):
                                chunk = conn.recv(min(13, 4 * len(expected) - len(reply)))
                                assert chunk, 'truncated reply'
                                reply.extend(chunk)
                            assert list(struct.unpack('<' + 'I' * len(expected), reply)) == expected
                        _, err = child.communicate(timeout=10)
                        if mode in ('bulk', 'fragmented'):
                            assert child.returncode == 0, err
                        else:
                            assert child.returncode == 2, (mode, child.returncode, err)
                            reason = {'bad-magic': b'bad request magic', 'bad-handshake': b'invalid handshake',
                                      'bad-padding': b'noncanonical payload', 'truncated': b'transport closed', 'bad-length': b'invalid request length', 'bad-horizon':b'invalid batch horizon'}[mode]
                            assert reason in err, err
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
            print('batched' if batched else 'sparse' if sparse else 'dense', mode, 'PASS', flush=True)

        modes = ['bulk', 'fragmented', 'bad-magic', 'bad-handshake', 'bad-padding', 'truncated']
        for mode in modes:
            exchange(mode)
            exchange(mode, True)
            exchange(mode, True, True)
        exchange("bad-length", True)
        exchange("bad-length", True, True)
        exchange("bad-horizon", True, True)
        result = dict(passed=True, protocols=["dense-v3", "sparse-v4", "batched-v5"], modes=modes+["bad-length","bad-horizon"], widths=WIDTHS, cycles=3)
        out = ROOT / 'results/transport-check.json'
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
