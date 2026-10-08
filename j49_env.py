#!/usr/bin/env python3
"""Read and edit the U-Boot environment on a j49 NAND image, as fw_setenv does.

The image is the emulator's: raw 2112-byte pages (2048 data + 64 OOB).
U-Boot keeps two copies, env0 at
0x340000 and env1 at 0x3a0000 (redundant format: CRC32 of the variables,
a generation byte, then 0x1fffb bytes of NUL-separated var=value). U-Boot
boots with the valid copy of the higher generation; this edits that copy in
place and rewrites its pages with fresh BCH4 ECC (the GPMC's code: 7 bytes
per 512-byte sector at OOB 36), as the NAND would hold them after a write.

    ./j49_env.py NAND.raw                       # print the active env
    ./j49_env.py NAND.raw addmodel='...' x=     # set, unset (empty)
    ./j49_env.py NAND.raw --root 1              # boot root1 first

--root N puts the root file system N boots from first in bootcmd: "run
nandbootN || run nandboot<other> || ...", as a system update switches it.
"""
import argparse
import re
import struct
import sys
import zlib

PAGE, OOB = 2048, 64
PAGE_RAW = PAGE + OOB
ENV_COPIES = (0x340000, 0x3a0000)
ENV_SIZE = 0x20000
ECC_OFFSET, ECC_BYTES = 36, 7

# BCH t=4 over GF(2^13), x^13 + x^4 + x^3 + x + 1: the generator polynomial
# as the GPMC uses it (bit i = coefficient of x^i, degree 52).
def _bch4_generator():
    exp, log = [0] * 8191 * 2, [0] * 8192
    x = 1
    for i in range(8191):
        exp[i] = exp[i + 8191] = x
        log[x] = i
        x <<= 1
        if x & 0x2000:
            x ^= 0x201b
    g, done = [1], set()
    for r in (1, 3, 5, 7):
        if r in done:
            continue
        m = [1]
        j = r
        while j not in done:
            done.add(j)
            m = [0] + m          # m(x) * x
            for k in range(len(m) - 1):
                if m[k + 1]:
                    m[k] ^= exp[log[m[k + 1]] + j]
            j = j * 2 % 8191
        prod = [0] * (len(g) + len(m) - 1)
        for a, ga in enumerate(g):
            for b, mb in enumerate(m):
                prod[a + b] ^= ga & (mb & 1)
        g = prod
    return sum(c << i for i, c in enumerate(g[:52]))


BCH4_G = _bch4_generator()


def bch4_parity(data):
    r, top, mask = 0, 1 << 51, (1 << 52) - 1
    for byte in data:
        for b in range(7, -1, -1):
            fb = bool(r & top) ^ ((byte >> b) & 1)
            r = (r << 1) & mask
            if fb:
                r ^= BCH4_G
    return r


def read_data(f, off, n):
    out = bytearray()
    while len(out) < n:
        page, col = divmod(off + len(out), PAGE)
        f.seek(page * PAGE_RAW + col)
        out += f.read(min(PAGE - col, n - len(out)))
    return bytes(out)


def write_data(f, off, data):
    """Rewrite whole pages: data, and each sector's ECC in the OOB."""
    assert off % PAGE == 0 and len(data) % PAGE == 0
    for p in range(len(data) // PAGE):
        page = off // PAGE + p
        f.seek(page * PAGE_RAW)
        raw = bytearray(f.read(PAGE_RAW))
        raw[:PAGE] = data[p * PAGE:(p + 1) * PAGE]
        for s in range(4):
            sector = raw[s * 512:(s + 1) * 512]
            at = PAGE + ECC_OFFSET + s * ECC_BYTES
            if sector == b'\xff' * 512:
                raw[at:at + ECC_BYTES] = b'\xff' * ECC_BYTES   # erased
            else:
                raw[at:at + ECC_BYTES] = bch4_parity(sector).to_bytes(7, 'big')
        f.seek(page * PAGE_RAW)
        f.write(raw)


def load_env(f, off):
    blob = read_data(f, off, ENV_SIZE)
    crc, gen = struct.unpack_from('<IB', blob)
    if zlib.crc32(blob[5:ENV_SIZE]) != crc:
        return None
    pairs = [e.decode(errors='replace') for e in blob[5:].split(b'\0\0')[0].split(b'\0') if e]
    return gen, dict(p.split('=', 1) for p in pairs if '=' in p)


def save_env(f, off, gen, env):
    body = b'\0'.join(f'{k}={v}'.encode() for k, v in env.items()) + b'\0\0'
    if len(body) > ENV_SIZE - 5:
        sys.exit('environment too large')
    body += b'\0' * (ENV_SIZE - 5 - len(body))
    write_data(f, off, struct.pack('<IB', zlib.crc32(body), gen) + body)


def active_env(f):
    """The copy U-Boot boots with: (generation, offset, {var: value})."""
    copies = [(load_env(f, off), off) for off in ENV_COPIES]
    valid = [(e[0], off, e[1]) for e, off in copies if e]
    if not valid:
        raise ValueError('no valid U-Boot environment')
    return max(valid, key=lambda v: v[0])


def read_env(nand):
    with open(nand, 'rb') as f:
        return active_env(f)[2]


def set_env(nand, values):
    """Set each var to its value; None or '' unsets it."""
    with open(nand, 'r+b') as f:
        gen, off, env = active_env(f)
        for k, v in values.items():
            if v:
                env[k] = v
            else:
                env.pop(k, None)
        save_env(f, off, gen, env)


BOOT_PAIR = re.compile(r"^run nandboot([01]) \|\| run nandboot([01])")


def set_root(nand, n):
    """Make bootcmd try root N first; return the root it tried before."""
    env = read_env(nand)
    bootcmd = env.get("bootcmd", "")
    m = BOOT_PAIR.match(bootcmd)
    if not m or m.group(1) == m.group(2):
        raise ValueError(f"bootcmd does not start with both nandboots: {bootcmd}")
    if m.group(1) != str(n):
        set_env(nand, {"bootcmd": BOOT_PAIR.sub(
            f"run nandboot{n} || run nandboot{1 - n}", bootcmd)})
    return int(m.group(1))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('nand')
    parser.add_argument('assign', nargs='*', help='var=value (empty value: unset)')
    parser.add_argument('--root', type=int, choices=(0, 1),
                        help='boot this root file system first')
    args = parser.parse_args()

    try:
        if args.root is not None:
            was = set_root(args.nand, args.root)
            print(f'root{args.root}' + ('' if was == args.root else f' (was root{was})'))
            if not args.assign:
                return
        if not args.assign:
            env = read_env(args.nand)
            print('\n'.join(f'{k}={v}' for k, v in env.items()))
            return
        set_env(args.nand, dict(a.partition('=')[::2] for a in args.assign))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
