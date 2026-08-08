# Building TX.EXE with OpenWatcom (the robust alternative)

`TX.COM` is ready to run and needs no compiler. Use this route only if you want
the C version — it speaks the exact same protocol and is easy to read and tweak.
OpenWatcom is a free, modern Windows installer that cross-builds real-mode DOS
executables, so you build on Windows and copy the result to the DOS machine.

## Install OpenWatcom (5 minutes)

1. Go to the OpenWatcom v2 releases:
   https://github.com/open-watcom/open-watcom-v2/releases
2. Download the Windows installer (named like
   `open-watcom-2_0-c-win-x64.exe` or the `-x86` variant).
3. Run it. Accept the defaults. When asked, let it set up environment variables
   (or you'll run `owsetenv.bat` yourself, below).

## Build

Open a new Command Prompt so the environment is loaded. If the installer didn't
set environment variables globally, run the setup script first (adjust the path
to where you installed it):

```
"C:\WATCOM\owsetenv.bat"
```

Then:

```
cd path\to\disk-xfer-rescue\tx-msdos
wmake
```

or, equivalently:

```
build.bat
```

You'll get **`TX.EXE`** in the `tx-msdos` folder. Copy it to the DOS machine the
same way as `TX.COM` (floppy, etc.).

## Usage (note: different flag style than TX.COM)

```
TX [-pPORT] [-bBAUD]
   -p1..4     COM port          (default 1)
   -b         baud, real number (default 9600)  e.g. -b38400
```

Examples:
```
TX                COM1, 9600, images whatever the host asks for
TX -p1 -b38400    COM1, 38400
```

There is no LBA-range flag anymore: the host decides what to image and sends the
sender a worklist over the link. For a full image just run `rx.py` normally; to
retry bad sectors, add `--resume` on the host and the sender automatically
re-reads only the bad/unfinished runs. The DOS command is the same either way.

The receiver command is identical to the quickstart:
```
python rx.py disk.img --port COM3 --baud 38400            (full image)
python rx.py disk.img --port COM3 --baud 38400 --resume   (retry bad sectors)
```

Everything else — bad-sector stubbing, mapfile, progress bar — works the
same as with `TX.COM`.

## If `wmake` isn't found

You didn't get the environment set. Either reopen the "Open Watcom … Build
Environment" shortcut the installer created, or run the `owsetenv.bat` line
above in your current Command Prompt, then retry `wmake`.
