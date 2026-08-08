# disk-xfer-rescue

A ddrescue-style, **serial-port whole-disk imager for old MS-DOS machines** —
for the case where the serial port is the only working way off the box and the
drive has bad sectors you don't want to give up on.

It images the first BIOS hard disk over a plain null-modem cable, retries and
then stubs unreadable sectors instead of aborting, records them in a
ddrescue-compatible mapfile, and can **resume** a later pass that re-reads only
the bad/untried sectors.

```
  DOS box  -- COMx -->  null-modem cable  -->  COM / USB-serial  --  modern host
   TX.COM                                                            rx.py
   reads HD0 via INT 13h                                             writes disk.img (+ .map)
```

> **Just want to use it?** Read **[`docs/WINDOWS-QUICKSTART.md`](docs/WINDOWS-QUICKSTART.md)**.
> Nothing to build: **`tx-msdos/TX.COM`** is the ready-to-run DOS sender (or
> recreate it on the DOS box from **`tx-msdos/MAKETX.SCR`** with `DEBUG`), and
> **`rx-host/rx.py`** is the host receiver.

---

## Build target: Intel 8086/8088 (non-negotiable)

**If a build errors on an instruction, fix the instruction -- never
raise the arch to silence it.**

---

## Features

- **Never aborts on a bad sector.** Each sector is retried (with a controller
  reset between tries); if it still can't be read it is **stubbed** -- a fill
  pattern is written at the correct offset, the sector is flagged bad, and
  imaging continues. `B` is printed for each bad sector.
- **ddrescue-compatible mapfile** (`.map`: `+` good, `-` bad, `?` untried) plus a
  plain `.badblocks` list you can hand to `e2fsck -l`, etc.
- **Resume (`--resume`).** A second pass re-reads only the bad/untried runs
  instead of re-dumping the whole disk -- seconds instead of an hour.
- **Verify (`--verify` / `--verify-only`).** Inspects a captured image: map
  completeness, MBR signature, partition table, FAT label, and whether any
  bad/untried sectors land inside a partition. `--verify-only` needs no port.
- **Manual range override (`--range A:B[,C:D]`)** for targeted re-reads.
- **Clean abort.** `ESC` / `Ctrl-C` on the DOS box (or just resetting it -- the
  drive is only ever read) stops safely; the host keeps what's imaged.
- **Live progress on both ends** -- position, good/bad counts, throughput, ETA.

Transport is a plain serial port only (no parallel, no FOSSIL driver). The DOS
side drives an 8250/16550 UART directly, polled. See
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the wire format.

## Repository layout

```
rx-host/
  rx.py                Python 3 host receiver (pyserial): image + mapfile + verify + progress
tx-msdos/
  TX.COM               prebuilt, ready-to-run DOS sender (pure 8086)
  MAKETX.SCR           DEBUG script that recreates TX.COM on the DOS box (no toolchain needed)
  Makefile, build.bat  OpenWatcom build of the C sender (TX.EXE)
  src/
    tx.S               the shipped sender, GNU as, .arch i8086   [primary, battle-tested]
    main.c             C sender: rescue loop, framing, ACK/NAK, progress
    int13.c/.h         BIOS geometry + sector read (+ reset)     [upstream logic kept]
    serial.c/.h        direct 8250/16550 UART, polled
    crc16.c/.h         CRC-16/CCITT-FALSE
    proto.h            shared wire-protocol constants
docs/
  WINDOWS-QUICKSTART.md   no-toolchain walkthrough (start here)
  COMPILE-WITH-WATCOM.md  build TX.EXE with a free compiler
  PROTOCOL.md             wire-protocol specification
tools/
  check_8086.py        audit a binary for non-8086 opcodes (belt-and-suspenders build check)
  emu8086.py           tiny logic emulator: run TX.COM off-hardware against a simulated disk
CHANGELOG.md
```

Two senders are provided and are interchangeable on the wire:

- **`TX.COM`** (from `src/tx.S`) -- the primary, hardware-verified binary. Ships
  prebuilt; needs no toolchain on the DOS box.
- **`TX.EXE`** (from the C sources) -- an alternative you can build with free
  tools if you'd rather work in C. Same protocol; less battle-tested.

## Wiring

A **null-modem** cable (TX<->RX crossed, grounds common) between the DOS COM port
and the host. A USB-to-serial adapter on the host is fine. Only TX, RX, and GND
are used -- there is no hardware flow control.

## Quick start

**Start the receiver first**, then start the sender.

Host:
```
python3 rx-host/rx.py disk.img --port /dev/ttyUSB0 --baud 115200
# Windows:  python rx-host\rx.py disk.img --port COM3 --baud 115200
```

DOS (positional `port baudcode`; e.g. COM1 @ 115200 = `1 5`):
```
TX 1 5
```

Baud codes: `1`=9600 `2`=19200 `3`=38400 `4`=57600 `5`=115200 `6`=4800 `7`=2400.
The baud must match on both ends. Neither sender takes an LBA range -- the host
sends the worklist (the whole disk normally, or just the unfinished runs on
`--resume`).

### Resume a pass

```
python3 rx-host/rx.py disk.img --port /dev/ttyUSB0 --baud 115200 --resume
```
Reads the existing `disk.img.map` and asks the sender to re-read only the runs
still marked bad or untried. Repeat on a cold drive to claw back marginal
sectors.

### Verify an image (no cable needed)

```
python3 rx-host/rx.py disk.img --verify-only
```

## Host options

```
--port         serial device (/dev/ttyUSB0, /dev/ttyS0, COMx)   [required to capture]
--baud         must match TX                                     (default 115200)
--mapfile      ddrescue mapfile path                             (default <image>.map)
--resume       keep an existing image; image only bad/untried runs (no truncate)
--range A:B    image specific LBA range(s), comma-separated; overlaid onto the image
--verify       after capture, inspect MBR/partitions and flag bad sectors inside them
--verify-only  inspect an existing image + map, no transfer (no --port needed)
```

## Building the sender

You don't need to build anything to use the tool. If you want to:

**Assembly (`TX.COM`)** -- GNU binutils:
```
as --32 tx-msdos/src/tx.S -o tx.o
ld -m elf_i386 -Ttext=0x100 --oformat=binary -e _start tx.o -o TX.COM
python3 tools/check_8086.py TX.COM        # verify pure-8086
python3 tools/emu8086.py TX.COM           # verify logic off-hardware
```

**C (`TX.EXE`)** -- [OpenWatcom](https://github.com/open-watcom/open-watcom-v2)
v1.9/2.0, real-mode, 8086 (`-0`): `cd tx-msdos && wmake` (or `build.bat`). See
[`docs/COMPILE-WITH-WATCOM.md`](docs/COMPILE-WITH-WATCOM.md).

## Limitations

- First BIOS hard disk (`0x80`) only, via CHS INT 13h -- so drives the BIOS can
  describe in CHS (<= 1024 cylinders).
- 512-byte sectors assumed.
- No hardware flow control; if you see overruns, drop the baud.

## Credits & license

This is a reworked fork of
[tschak909/disk-xfer](https://github.com/tschak909/disk-xfer). It keeps the
upstream's hardware-correct INT 13h geometry/CHS logic and replaces the
transport and control flow (rescue loop, CRC framing, worklist/resume, verify,
ddrescue mapfile) and retargets the sender to strict 8086.

Licensed under **GPLv3**, the same as upstream. If you forked upstream on GitHub
you already have its `COPYING`/`LICENSE`; otherwise add the GPLv3 text from
<https://www.gnu.org/licenses/gpl-3.0.txt>. Modifications in this fork are
offered under the same terms.
