# Windows quickstart (no toolchain needed)

You need three things, all included:

- **`tx-msdos/TX.COM`** — the DOS sender, ready to run. (Nothing to build.)
- **`tx-msdos/MAKETX.SCR`** — recreates `TX.COM` on the DOS box using DOS's own
  `DEBUG`, in case you can only move *text* to the old machine, not a binary.
- **`rx-host/rx.py`** — the receiver, runs on your Windows PC.

The DOS sender only ever **reads** the source disk (INT 13h read + geometry +
controller reset). It never writes to it, so it's safe to try.

> `TX.COM` is a pure-8086 binary, verified end-to-end on a Compaq LTE 286
> (whole-disk imaging and `--resume`). It's safe to run at 115200. If you're on
> unknown hardware or a flaky cable, starting at **9600 baud** is a good way to
> confirm the link before raising the speed. A C build (`TX.EXE`) is also
> available -- see `COMPILE-WITH-WATCOM.md`.
>
> The examples below run `python rx.py ...` -- run them from the `rx-host`
> folder, or use the full path `python rx-host\rx.py ...`.

## 1. Set up the Windows receiver

1. Install Python 3 from https://www.python.org/downloads/ — tick **"Add
   python.exe to PATH"** during install.
2. Open Command Prompt (or PowerShell) and install the serial library:
   ```
   pip install pyserial
   ```
3. Find your serial port (plug in the USB-to-serial adapter first):
   ```
   python rx.py --list
   ```
   You'll see something like `COM3  USB Serial Port`. Note the `COMx` name.

## 2. Wire the two machines

Use a **null-modem** serial cable between the DOS machine's COM port and your
Windows PC (a USB-to-serial adapter on the PC side is fine). Only TX, RX, and
GND are used — no hardware flow control required. A straight-through cable will
*not* work; it must be null-modem (TX and RX crossed).

## 3. Get TX onto the DOS machine

**If the DOS machine has a floppy drive (usual case):** copy `TX.COM` onto a
floppy on your Windows PC (a USB floppy drive works), then copy it to the DOS
machine's hard disk or just run it from the floppy.

**If you can only send text** (e.g. you have a terminal program on the DOS side
and can paste into a file, or you copy `MAKETX.SCR` via floppy): put
`MAKETX.SCR` on the DOS machine, then run:
```
DEBUG < MAKETX.SCR
```
That writes `TX.COM` to the current directory. (`DEBUG.EXE` ships with MS-DOS
and PC-DOS. FreeDOS has it too.)

## 4. Image the disk

**Start the receiver first** on Windows, then start TX on DOS.

Windows (pick your COM port; baud must match the DOS side):
```
python rx.py disk.img --port COM3 --baud 9600
```

DOS (`TX  port  baudcode`):
```
TX 1 1
```
`TX 1 1` = COM1, 9600 baud. Baud codes:

| code | baud   |
|------|--------|
| 1    | 9600   |
| 2    | 19200  |
| 3    | 38400  |
| 4    | 57600  |
| 5    | 115200 |
| 6    | 4800   |
| 7    | 2400   |

So COM1 @ 38400 is `TX 1 3`, and on Windows `--baud 38400`. The port digit is
the DOS COM number (1–4); the Windows `--port` is whatever `--list` showed.

Once a short run at 9600 clearly works, cancel and re-run both sides at a higher
speed. A 286's UART transmits fine at any rate; if a faster setting produces
lots of retries or a stalled progress bar, drop back down.

**To stop TX at any time, press `ESC` (or `Ctrl-C`) on the DOS machine.** It
finishes the sector in flight, tells the host it's stopping, and exits cleanly to
the DOS prompt. Whatever was imaged so far is kept, and the un-imaged sectors are
recorded as "not yet done" — so a later `--resume` continues from where you left
off. (Resetting the 286 also aborts safely; it's booted from floppy and the hard
disk is only ever read.)

## 5. What you get

- `disk.img` — the byte-for-byte disk image.
- `disk.img.map` — a GNU ddrescue-compatible map (`+` good, `-` bad, `?` not
  received).
- `disk.img.badblocks` — plain list of unreadable sector numbers.

Unreadable sectors don't stop the run: TX retries each a few times, then writes
a zero-filled **stub** so the image stays aligned, and the receiver records the
location. On the DOS console you'll see a `.` for every 256 sectors imaged and a
`B` printed the moment each bad sector is hit — so a cluster of `B`s marks a bad
patch, and the dots slowing down there is the retry logic grinding, not a hang.
The Windows side shows a live progress bar (percent, MB, MB/s, bad count, ETA).

## 6. Retrying bad sectors (ddrescue-style, now automatic)

Bad sectors sometimes read on a later try. Just re-run with `--resume` and the
host figures out what still needs work — it reads the existing `disk.img.map`,
and tells the DOS side to re-image **only** the bad/unfinished runs:

Windows:
```
python rx.py disk.img --port COM3 --baud 19200 --resume
```
DOS — start the sender exactly as before (no range arguments needed):
```
TX 1 2
```
The host sends the sender a worklist of just the ranges to retry, so a resume
pass streams only the bad regions instead of the whole disk. Recovered sectors
flip from `-` to `+` in the map, and stubborn ones stay `-`. Repeat the pair of
commands as many times as you like; each pass narrows the bad list. When the map
is all `+`, `rx.py` prints "Nothing to do."

This works with the ready-made `TX.COM` and the Watcom-built `TX.EXE` alike —
range selection lives entirely on the host now, so the DOS command never changes.

## 7. Checking the image (`--verify`)

Add `--verify` to have the host inspect the image the moment the transfer
finishes:
```
python rx.py disk.img --port COM3 --baud 115200 --verify
```
It reports the good/bad/untried sector counts, checks for a valid MBR, lists the
partitions (type, size, boot flag), reads each partition's boot sector to confirm
the filesystem (e.g. `FAT16`), and — the useful part for a failing drive — tells
you whether any bad or untried sectors landed *inside* a partition versus in
unused space. It ends with a plain complete/incomplete verdict.

You can also verify an image you already captured, with no cable attached:
```
python rx.py disk.img --verify-only
```
(It reads `disk.img` and `disk.img.map`; no `--port` needed.)

## Time estimate

Roughly: bytes ÷ (baud ÷ 10) ÷ 0.98. A 120 MB disk is about 35 h at 9600,
9 h at 38400, 6 h at 57600. Faster is better if the link is clean.

## Limits

First hard disk (BIOS `0x80`) via CHS INT 13h — fine for the small MFM/IDE
drives these machines shipped with (up to 1024 cylinders / ~504 MB–8 GB
depending on BIOS). It's a raw sector image, so afterward you can mount it,
run `fsck`, carve files with PhotoRec, etc.
