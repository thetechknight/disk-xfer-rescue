#!/usr/bin/env python3
"""
check_8086.py - audit a DOS .COM/.EXE binary for instructions that do NOT
exist on an Intel 8086/8088.

Why this exists
---------------
This project targets real vintage hardware. A single 186/286/386-only opcode
is an *invalid opcode* on an 8086/8088 (and the 386-only near-Jcc form is
invalid on a 286 too), and it hangs the machine hard with no diagnostic.

That is exactly the bug that plagued the resume feature for a long time: the
assembler, in 32-bit mode, compiled a far `jae` to the 386-only two-byte form
`0F 83`, which faulted the moment the 286 reached it - right after printing
"Linked", before any disk access. The emulator ran it fine (it implements 386
jumps), so nothing caught it until this audit.

Keep this in your build/CI: `.arch i8086` in the assembler and `-0` in
OpenWatcom are the front-line guards; this is the belt-and-suspenders check on
the actual emitted bytes.

Usage
-----
    python3 tools/check_8086.py tx-msdos/TX.COM
    python3 tools/check_8086.py --load-addr 0x100 path/to/file.com

Exit status is non-zero if any suspect opcode is found, so it drops straight
into a Makefile or CI step.

Requires `objdump` (binutils) on PATH.
"""
import argparse
import re
import shutil
import subprocess
import sys

# First-byte opcode families that are NOT valid on an 8086/8088.
# (value -> short human explanation)
SUSPECT = {
    "0f": "two-byte opcode (near Jcc/MOVZX/etc.) - 386+ here, invalid on 8086/286",
    "66": "operand-size prefix - 386+",
    "67": "address-size prefix - 386+",
    "c0": "shift/rotate r/m8, imm8 - 186/286+",
    "c1": "shift/rotate r/m16, imm8 - 186/286+",
    "68": "push imm16 - 186/286+",
    "6a": "push imm8 - 186/286+",
    "69": "imul r16, r/m16, imm16 - 186/286+",
    "6b": "imul r16, r/m16, imm8 - 186/286+",
    "60": "pusha - 186/286+",
    "61": "popa - 186/286+",
    "c8": "enter - 186/286+",
    "c9": "leave - 186/286+",
    "6c": "insb - 186/286+",
    "6d": "insw - 186/286+",
    "6e": "outsb - 186/286+",
    "6f": "outsw - 186/286+",
}

LINE = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2} )+)\s*(\S.*)?$")


def find_code_end(data):
    """Heuristic: the code ends where the '$'-terminated message block begins.
    Look for the earliest 12-byte window that is entirely printable ASCII and
    contains at least 8 alphabetic characters - i.e. real English text, not a
    coincidental run of immediates. Falls back to end-of-file."""
    def alpha(b):
        return (0x41 <= b <= 0x5a) or (0x61 <= b <= 0x7a)
    W = 12
    for i in range(0, max(0, len(data) - W)):
        win = data[i:i + W]
        if all(0x20 <= b < 0x7f for b in win) and sum(alpha(b) for b in win) >= 8:
            return i
    return len(data)


def main():
    ap = argparse.ArgumentParser(description="Audit a .COM/.EXE for non-8086 opcodes.")
    ap.add_argument("binary")
    ap.add_argument("--load-addr", default="0x100",
                    help="link/load address of the first byte (default 0x100 for .COM)")
    ap.add_argument("--all", action="store_true",
                    help="scan the whole file, not just the detected code region")
    args = ap.parse_args()

    if not shutil.which("objdump"):
        sys.exit("error: objdump not found on PATH (install binutils)")

    data = open(args.binary, "rb").read()
    load = int(args.load_addr, 16)
    code_end = len(data) if args.all else find_code_end(data)

    dis = subprocess.run(
        ["objdump", "-D", "-b", "binary", "-mi386",
         "-Maddr16,data16,intel", args.binary],
        capture_output=True, text=True).stdout

    hits = []
    for line in dis.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        off = int(m.group(1), 16)          # objdump address == file offset for -b binary
        if off >= code_end:
            continue
        first = m.group(2).split()[0].lower()
        if first in SUSPECT:
            mem = load + off
            hits.append((off, mem, m.group(3) or "", SUSPECT[first]))

    name = args.binary
    if not hits:
        print(f"OK: {name} is pure 8086 "
              f"(scanned code region: 0x0..0x{code_end:x}, "
              f"{'whole file' if args.all else 'auto-detected'}).")
        return 0

    print(f"FAIL: {name} contains {len(hits)} non-8086 instruction(s):\n")
    for off, mem, mnem, why in hits:
        print(f"  file 0x{off:04x}  mem 0x{mem:04x}  {mnem:<28} <- {why}")
    print("\nFix the instruction(s) to 8086-only forms; do NOT raise the "
          "assembler arch to silence this.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
