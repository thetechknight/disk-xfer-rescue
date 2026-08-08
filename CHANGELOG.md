# Changelog

All notable changes to this fork are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Restore / clone-back (`WR.COM` + `rx.py --restore`).** The reverse of
  imaging: write an image back onto the DOS box's drive over the same serial
  link. `WR.COM` is the DOS-side writer (a mirror of `TX.COM`: it announces
  geometry, receives the worklist, then receives one DATA frame per sector and
  writes it with INT 13h AH=03). It is **bad-sector aware on write** - a sector
  the drive refuses is retried, then **stubbed** (skipped), counted, and marked
  with `B`; the good/bad totals come back in the EOT. The host prints a clear
  **"X bad sector(s) found during clone, drive may be unreliable"** alert when
  any write failed. Because this is destructive, both ends confirm: `WR.COM`
  requires a capital **Y** on the DOS box and `rx.py --restore` requires typing
  **YES**. `--range` works in restore too (write only part). The whole path was
  validated in the emulator end-to-end, including a **simulated lost ACK** to
  prove the writer's de-dup never double-writes or misaligns a sector.
  `WR.COM` ships prebuilt with a `MAKEWR.SCR` DEBUG recreate script, is pure
  8086, and is reproducible byte-for-byte.

- **Resume (`--resume`).** The host reads back the ddrescue-style mapfile and
  asks the sender to re-read only the runs still marked bad or untried,
  coalesced to at most `MAXR` ranges. A new `CMD` worklist frame carries those
  ranges to the DOS side, which images them in order. Turns a 40 MB re-dump
  into a few seconds of targeted retries.
- **Worklist protocol.** `SOH 'C' nranges [start end]... crc` replaces the old
  single-ACK reply to `INFO`. No command-level ack (see `docs/PROTOCOL.md`).
- **Verify (`--verify`, `--verify-only`).** Checks a captured image: map
  completeness, MBR `0x55AA`, partition table, FAT label, and whether any
  bad/untried sectors fall inside a partition. `--verify-only` needs no serial
  port. Percentage never rounds up to 100% while the image is incomplete.
- **Manual range override (`--range A:B[,C:D]`).** Image or re-image specific
  LBA ranges, overlaid onto an existing image.
- **Keyboard abort.** ESC or Ctrl-C on the DOS box cleanly aborts: the sender
  emits EOT, prints a message, and exits, and the host closes out normally.
  Implemented by draining the BIOS keyboard buffer directly (no INT 16h
  dependency).
- **Bad-block indicator.** The sender prints `B` for each unreadable sector as
  it goes.
- **Dev tools** (`tools/`): `check_8086.py` audits an emitted binary for any
  non-8086 opcode; `emu8086.py` is a small logic emulator that runs `TX.COM`
  off-hardware against a simulated disk and worklist.

### Fixed
- **The "hangs right after *Linked*" bug (root cause).** With the range loop in
  place, the assembler - running in 32-bit mode - compiled a far `jae all_done`
  to the **386-only** two-byte near form `0F 83`. That opcode is invalid on an
  8086/8088 and on the 80286, so the CPU faulted the instant it reached
  `next_range` (right after printing "Linked", before any disk access). It
  presented as a dead hang with no HDD light and hit-or-miss "Linked" text,
  and it was invisible to the logic emulator because the emulator implements
  386 jumps. Fixed by assembling the sender with **`.arch i8086`**, which forces
  the assembler to lower far conditional jumps to `short Jcc + near JMP` and to
  reject every newer instruction outright.

### Changed
- **Sender retargeted to strict 8086/8088.** `.arch i8086` in `tx.S` and `-0`
  in the OpenWatcom build. Four shift-by-immediate instructions (a 186/286
  feature) that had slipped into the geometry and worklist code were rewritten
  as repeated shift-by-1. The prebuilt `TX.COM` is verified pure-8086 by
  `tools/check_8086.py`. This is a hard project rule now: the tool exists for
  vintage hardware, so the binary must run on the oldest hardware.

### Verified
- Whole-disk imaging and `--resume` both confirmed on a Compaq LTE 286
  (40.6 MB drive, 20 physically bad sectors recovered/marked).
