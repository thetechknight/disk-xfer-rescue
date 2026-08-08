#!/usr/bin/env python3
"""
rx.py - host side of a ddrescue-style whole-disk serial imager.

Receives sectors from the MS-DOS `tx` program over a serial port and writes a
byte-exact disk image. Each sector carries its own LBA, so every sector lands
at its correct offset in the image (seek + write). Sectors the DOS side could
not read arrive flagged BAD and filled with a pattern; they are still written
(so the image stays aligned) and their locations are recorded in a
GNU ddrescue-compatible mapfile plus a plain bad-blocks list.

  Start this FIRST, then run TX on the DOS box.

Usage:
  python3 rx.py disk.img --port /dev/ttyUSB0 --baud 115200
  python3 rx.py disk.img --port COM3 --baud 57600 --resume   # 2nd pass

Options:
  --port     serial device (e.g. /dev/ttyUSB0, /dev/ttyS0, COM3)
  --baud     must match TX (default 115200)
  --mapfile  ddrescue-style mapfile path (default: <image>.map)
  --resume   open an existing image and overwrite only received sectors
             (do NOT truncate) - use for multi-pass recovery of bad areas
"""

import argparse
import os
import sys
import time

serial = None
list_ports = None

def _need_serial():
    """Import pyserial on demand (verification paths don't need it)."""
    global serial, list_ports
    if serial is None:
        try:
            import serial as _serial
            from serial.tools import list_ports as _lp
        except ImportError:
            sys.exit("This needs pyserial:  pip install pyserial")
        serial, list_ports = _serial, _lp
    return serial


def print_ports():
    _need_serial()
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Plug in your USB-serial adapter and retry.")
        return
    print("Available serial ports:")
    for p in ports:
        print(f"  {p.device:<12} {p.description}")

# ---- protocol constants (must match proto.h on the DOS side) --------------
SOH, ACK, NAK = 0x01, 0x06, 0x15
TYPE_INFO, TYPE_DATA, TYPE_EOT = ord('I'), ord('D'), ord('E')
TYPE_CMD = ord('C')            # host -> TX worklist, sent in reply to INFO
STATUS_GOOD, STATUS_BAD = 0x00, 0x01
MAXR = 256                     # max ranges TX can hold (must match proto.h)

# status bytemap values
UNTRIED, GOOD, BAD = 0, 1, 2


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, identical to crc16_update() on the DOS side."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


class Link:
    def __init__(self, ser):
        self.ser = ser

    def read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.ser.read(n - len(buf))
            if not chunk:
                raise TimeoutError("serial timeout")
            buf += chunk
        return bytes(buf)

    def hunt_soh(self):
        while True:
            b = self.ser.read(1)
            if not b:
                raise TimeoutError("serial timeout (no SOH)")
            if b[0] == SOH:
                return

    def ack(self):
        self.ser.write(bytes([ACK]))

    def nak(self):
        self.ser.reset_input_buffer()
        self.ser.write(bytes([NAK]))

    def send_frame(self, type_byte, payload):
        """SOH + type + payload + CRC16(type+payload)."""
        body = bytes([type_byte]) + payload
        crc = crc16(body)
        self.ser.write(bytes([SOH]) + body + crc.to_bytes(2, "little"))

    def read_reply(self, timeout_s):
        """Read one byte within timeout_s. Returns the byte or None."""
        old = self.ser.timeout
        self.ser.timeout = timeout_s
        try:
            b = self.ser.read(1)
        finally:
            self.ser.timeout = old
        return b[0] if b else None


def fmt_time(sec):
    if sec is None or sec < 0 or sec > 359999:
        return "--:--"
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def draw_bar(received, expected, bad, bps, start_time, width=28):
    now = time.time()
    elapsed = max(now - start_time, 1e-6)
    mb = received * bps / (1024 * 1024)
    rate = (received * bps) / elapsed / (1024 * 1024)  # MB/s
    if expected:
        frac = min(received / expected, 1.0)
        filled = int(frac * width)
        bar = "#" * filled + "-" * (width - filled)
        total_mb = expected * bps / (1024 * 1024)
        remain = (expected - received) / (received / elapsed) if received else None
        line = (f"\r[{bar}] {frac*100:5.1f}%  "
                f"{mb:7.1f}/{total_mb:.1f} MB  {rate:5.2f} MB/s  "
                f"bad {bad}  ETA {fmt_time(remain)}   ")
    else:
        line = (f"\rsector {received}  {mb:7.1f} MB  {rate:5.2f} MB/s  "
                f"bad {bad}   ")
    sys.stdout.write(line)
    sys.stdout.flush()


def build_runs(statusmap, bps):
    """Yield (pos_bytes, size_bytes, status_char) runs for the mapfile."""
    charmap = {UNTRIED: '?', GOOD: '+', BAD: '-'}
    n = len(statusmap)
    i = 0
    while i < n:
        j = i
        cur = statusmap[i]
        while j < n and statusmap[j] == cur:
            j += 1
        yield (i * bps, (j - i) * bps, charmap[cur])
        i = j


def write_mapfile(path, statusmap, bps, total):
    done = all(s != UNTRIED for s in statusmap)
    with open(path, "w") as f:
        f.write("# Mapfile. Created by rx.py (disk-xfer-rescue).\n")
        f.write("# Compatible with GNU ddrescue: + finished, - bad, ? non-tried.\n")
        f.write(f"# sector size: {bps}   total sectors: {total}\n")
        f.write(f"0x{total*bps:08X}     {'+' if done else '?'}\n")
        for pos, size, ch in build_runs(statusmap, bps):
            f.write(f"0x{pos:08X}  0x{size:08X}  {ch}\n")


def write_badlist(path, statusmap):
    with open(path, "w") as f:
        f.write("# Unreadable sectors (LBA, decimal).\n")
        for lba, s in enumerate(statusmap):
            if s == BAD:
                f.write(f"{lba}\n")


PART_TYPES = {
    0x00: "empty", 0x01: "FAT12", 0x04: "FAT16 <32M", 0x05: "extended",
    0x06: "FAT16", 0x07: "NTFS/HPFS/exFAT", 0x0B: "FAT32 (CHS)",
    0x0C: "FAT32 (LBA)", 0x0E: "FAT16 (LBA)", 0x0F: "extended (LBA)",
    0x82: "Linux swap", 0x83: "Linux", 0xDE: "Dell utility",
    0xEE: "GPT protective", 0xEF: "EFI System",
}


def _range_status(statusmap, start, end, total):
    """Count good/bad/untried sectors in [start,end) clamped to the image."""
    s = max(0, start); e = min(total, end)
    g = b = u = 0
    for lba in range(s, e):
        v = statusmap[lba]
        if v == GOOD: g += 1
        elif v == BAD: b += 1
        else: u += 1
    return g, b, u


def parse_partitions(mbr):
    """Return the 4 primary partition entries from a 512-byte MBR."""
    parts = []
    for i in range(4):
        off = 0x1BE + i * 16
        e = mbr[off:off + 16]
        boot = e[0]
        ptype = e[4]
        start = int.from_bytes(e[8:12], "little")
        size = int.from_bytes(e[12:16], "little")
        parts.append({"boot": boot, "type": ptype, "start": start, "size": size})
    return parts


def fs_label(sector):
    """Best-effort filesystem label from a FAT/NTFS boot sector."""
    if len(sector) < 512:
        return None
    oem = sector[3:11].decode("latin-1", "replace").strip()
    for off in (0x36, 0x52):                      # FAT12/16 and FAT32 type fields
        tag = sector[off:off + 8].decode("latin-1", "replace").strip()
        if tag and all(32 <= ord(c) < 127 for c in tag):
            if tag.startswith(("FAT", "NTFS")):
                return tag + (f" (OEM '{oem}')" if oem else "")
    if sector[510:512] == b"\x55\xAA":
        return f"boot sector present (OEM '{oem}')" if oem else "boot sector present"
    return None


def verify_image(path, statusmap, bps, total):
    print("\n--- verify ---")
    good = sum(1 for s in statusmap if s == GOOD)
    bad  = sum(1 for s in statusmap if s == BAD)
    unt  = sum(1 for s in statusmap if s == UNTRIED)
    if total == 0:
        pct = 0.0
    elif good == total:
        pct = 100.0
    else:
        pct = min(99.99, 100.0 * good / total)   # never round up to 100% here
    dp = 1 if good == total else 2
    print(f"Sectors: {good} good, {bad} bad, {unt} untried "
          f"of {total} ({pct:.{dp}f}% good)")

    if total == 0:
        return
    with open(path, "rb") as f:
        f.seek(0)
        mbr = f.read(bps)

        if statusmap[0] != GOOD:
            print("WARNING: LBA 0 (MBR/boot sector) was not read cleanly - "
                  "partition info below may be unreliable.")

        if len(mbr) >= 512 and mbr[510] == 0x55 and mbr[511] == 0xAA:
            print("MBR signature 0x55AA: present")
            parts = parse_partitions(mbr)
            if not any(p["type"] for p in parts):
                print("Partition table: empty (no primary partitions).")
            for i, p in enumerate(parts, 1):
                if p["type"] == 0:
                    continue
                name = PART_TYPES.get(p["type"], f"type 0x{p['type']:02X}")
                mb = p["size"] * bps / 1024 / 1024
                flag = " [boot]" if p["boot"] == 0x80 else ""
                print(f"  Partition {i}: {name}{flag}  start LBA {p['start']}, "
                      f"{p['size']} sectors ({mb:.1f} MB)")
                end = p["start"] + p["size"]
                if end > total:
                    print(f"    note: extends past the image "
                          f"(needs {end}, image has {total} sectors)")
                g, b, u = _range_status(statusmap, p["start"], end, total)
                if b or u:
                    print(f"    integrity: {b} bad, {u} untried "
                          f"sector(s) inside this partition")
                else:
                    print("    integrity: all sectors good")
                # peek the partition's own boot sector if we captured it cleanly
                if p["start"] < total and statusmap[p["start"]] == GOOD:
                    f.seek(p["start"] * bps)
                    lbl = fs_label(f.read(bps))
                    if lbl:
                        print(f"    filesystem: {lbl}")
        else:
            print("MBR signature: NOT found. The image may be unpartitioned "
                  "(superfloppy/whole-disk filesystem), non-DOS, or LBA 0 was "
                  "unreadable.")
            lbl = fs_label(mbr) if statusmap[0] == GOOD else None
            if lbl:
                print(f"LBA 0 looks like a filesystem boot sector: {lbl}")

    if bad or unt:
        print("Verify: image is INCOMPLETE - re-run with --resume to retry the "
              "bad/untried sectors.")
    else:
        print("Verify: image is complete and every sector read good.")


def load_mapfile(path, total, bps):
    """Parse an existing ddrescue-style mapfile into a statusmap (bytearray).
    Unknown/missing regions stay UNTRIED. Only +, -, ? are recognized; any other
    ddrescue status char (/, *) is treated as 'still needs work' (UNTRIED)."""
    statusmap = bytearray(total)   # UNTRIED
    charval = {'+': GOOD, '-': BAD, '?': UNTRIED}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue                      # skip the status/current-pos line
            try:
                pos = int(parts[0], 16); size = int(parts[1], 16)
            except ValueError:
                continue
            ch = parts[2]
            val = charval.get(ch, UNTRIED)
            lba0 = pos // bps
            lba1 = (pos + size) // bps
            for lba in range(max(0, lba0), min(total, lba1)):
                statusmap[lba] = val
    return statusmap


def parse_ranges_spec(spec, total):
    """Parse '0:200,43301:43302' into [(0,200),(43301,43302)] (END exclusive)."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"bad range '{part}', expected START:END")
        a_s, b_s = part.split(":", 1)
        a, b = int(a_s, 0), int(b_s, 0)
        if a < 0 or b > total or a >= b:
            raise ValueError(f"range {a}:{b} out of bounds (0..{total}, START<END)")
        out.append((a, b))
    if len(out) > MAXR:
        raise ValueError(f"too many ranges ({len(out)} > MAXR={MAXR})")
    return out


def build_worklist(statusmap, total, resume):
    """Return a list of (start_lba, end_lba) half-open ranges to image.
    Fresh run: the whole disk. Resume: every BAD or UNTRIED run. Coalesced to
    at most MAXR ranges by merging across the smallest gaps."""
    if not resume:
        return [(0, total)]

    runs = []
    i = 0
    while i < total:
        if statusmap[i] in (BAD, UNTRIED):
            j = i
            while j < total and statusmap[j] in (BAD, UNTRIED):
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    if not runs:
        return []

    # Coalesce down to MAXR by repeatedly merging the pair with the smallest gap.
    while len(runs) > MAXR:
        best_k, best_gap = 0, None
        for k in range(len(runs) - 1):
            gap = runs[k + 1][0] - runs[k][1]
            if best_gap is None or gap < best_gap:
                best_gap, best_k = gap, k
        runs[best_k][1] = runs[best_k + 1][1]
        del runs[best_k + 1]
    return [(a, b) for a, b in runs]


def send_command(link, ranges, info_bytes):
    """Send the worklist to TX and return the frame payload. No command-level
    ack is used: once TX has the worklist it just starts streaming DATA, and if
    it missed the worklist it re-sends INFO, which the data loop answers by
    re-sending this same command. That keeps the handshake free of the
    lost-ack 'two-army' trap."""
    payload = len(ranges).to_bytes(2, "little")
    for a, b in ranges:
        payload += a.to_bytes(4, "little") + b.to_bytes(4, "little")
    link.send_frame(TYPE_CMD, payload)
    return payload


def main():
    ap = argparse.ArgumentParser(description="ddrescue-style serial disk receiver")
    ap.add_argument("image", nargs="?", help="output image file, e.g. disk.img")
    ap.add_argument("--port", help="serial device, e.g. COM3 (Windows) or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600,
                    help="must match the DOS side (default 9600; raise once it works)")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--mapfile", default=None)
    ap.add_argument("--badlist", default=None)
    ap.add_argument("--resume", action="store_true",
                    help="keep existing image, overwrite received sectors only")
    ap.add_argument("--verify", action="store_true",
                    help="after receiving, inspect the image (MBR, partitions, "
                         "and whether any bad/untried sectors land inside them)")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip the transfer; just verify an existing image + map")
    ap.add_argument("--range", default=None,
                    help="image specific LBA ranges instead of the auto worklist, "
                         "e.g. 0:200 or 100:150,43301:43302 (END is exclusive). "
                         "Overlays onto an existing image; for tests or manual retries.")
    args = ap.parse_args()

    if args.list:
        print_ports()
        return

    if args.verify_only:
        if not args.image or not os.path.exists(args.image):
            sys.exit("--verify-only needs an existing image file.\n"
                     "  python rx.py disk.img --verify-only")
        bps = 512
        total = os.path.getsize(args.image) // bps
        mf = args.mapfile or (args.image + ".map")
        if os.path.exists(mf):
            statusmap = load_mapfile(mf, total, bps)
        else:
            print("(no mapfile found; sector status unknown, assuming good)")
            statusmap = bytearray([GOOD]) * total
        verify_image(args.image, statusmap, bps, total)
        return

    if not args.port:
        print_ports()
        sys.exit("\nSpecify --port. Example:\n"
                 "  python rx.py disk.img --port COM3 --baud 9600")
    if not args.image:
        sys.exit("Specify an output image file. Example:\n"
                 "  python rx.py disk.img --port COM3 --baud 9600")

    mapfile = args.mapfile or (args.image + ".map")
    badlist = args.badlist or (args.image + ".badblocks")

    _need_serial()
    ser = serial.Serial(args.port, args.baud, timeout=3.0,
                        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE)
    link = Link(ser)

    print(f"Listening on {args.port} @ {args.baud} baud. Start TX on the DOS box now.")

    # ---- INFO handshake (do NOT ack; the worklist is our reply) ----
    total = cyls = heads = spt = bps = 0
    info_frame = None
    while True:
        try:
            link.hunt_soh()
            t = link.read_exact(1)[0]
            if t != TYPE_INFO:
                continue
            payload = link.read_exact(12)
            crc_rx = int.from_bytes(link.read_exact(2), "little")
            if crc16(bytes([t]) + payload) != crc_rx:
                continue
            total = int.from_bytes(payload[0:4], "little")
            cyls  = int.from_bytes(payload[4:6], "little")
            heads = int.from_bytes(payload[6:8], "little")
            spt   = int.from_bytes(payload[8:10], "little")
            bps   = int.from_bytes(payload[10:12], "little")
            info_frame = payload            # (kept for reference/debug)
            break
        except TimeoutError:
            continue

    disk_bytes = total * bps
    print(f"\nGeometry: {cyls}c x {heads}h x {spt}s = {total} sectors "
          f"({disk_bytes/1024/1024:.1f} MB), {bps} B/sector")
    print(f"Image:    {args.image}   mapfile: {mapfile}")

    # ---- decide what to image; open the image ----
    # --range and --resume both overlay onto an existing image (no truncate).
    overlay = (args.resume or args.range) and os.path.exists(args.image)
    if overlay:
        f = open(args.image, "r+b")
        if os.path.exists(mapfile):
            statusmap = load_mapfile(mapfile, total, bps)
            print("Keeping existing image; loaded existing map.")
        else:
            statusmap = bytearray(total)
            print("Keeping existing image; no map found.")
    else:
        statusmap = bytearray(total)
        f = open(args.image, "w+b")
        f.truncate(disk_bytes)              # pre-size so stubs/holes stay aligned

    if args.range:
        try:
            worklist = parse_ranges_spec(args.range, total)
        except ValueError as e:
            f.close(); sys.exit(f"--range error: {e}")
        print(f"Manual worklist ({len(worklist)} range(s)): "
              + ", ".join(f"{a}:{b}" for a, b in worklist))
    else:
        worklist = build_worklist(statusmap, total, args.resume)

    todo = sum(b - a for a, b in worklist)
    if args.resume and not args.range:
        print(f"Worklist: {len(worklist)} range(s), {todo} sectors to (re)read.")
    if todo == 0:
        print("Nothing to do - the map is already complete.")

    # ---- send the worklist; TX acks it, then streams those sectors ----
    print("Sending worklist to TX...")
    cmd_payload = send_command(link, worklist, info_frame)

    expected = todo if todo > 0 else None
    received = 0
    bad_count = sum(1 for s in statusmap if s == BAD)
    start_time = time.time()
    first_seen = False
    last_draw = 0.0

    try:
        while True:
            try:
                link.hunt_soh()
                t = link.read_exact(1)[0]

                if t == TYPE_DATA:
                    hdr = link.read_exact(5)              # lba(4) status(1)
                    data = link.read_exact(bps)
                    crc_rx = int.from_bytes(link.read_exact(2), "little")
                    if crc16(bytes([t]) + hdr + data) != crc_rx:
                        link.nak(); continue

                    lba = int.from_bytes(hdr[0:4], "little")
                    status = hdr[4]
                    if lba >= total:
                        link.nak(); continue

                    f.seek(lba * bps)
                    f.write(data)

                    prev = statusmap[lba]
                    statusmap[lba] = BAD if status == STATUS_BAD else GOOD
                    if prev == BAD and statusmap[lba] == GOOD:
                        bad_count -= 1                     # recovered on this pass
                    elif prev != BAD and statusmap[lba] == BAD:
                        bad_count += 1
                    received += 1

                    if not first_seen:
                        first_seen = True
                        start_time = time.time()

                    link.ack()

                    now = time.time()
                    if now - last_draw > 0.1:
                        draw_bar(received, expected, bad_count, bps, start_time)
                        last_draw = now

                elif t == TYPE_EOT:
                    payload = link.read_exact(8)          # good(4) bad(4)
                    crc_rx = int.from_bytes(link.read_exact(2), "little")
                    if crc16(bytes([t]) + payload) != crc_rx:
                        link.nak(); continue
                    link.ack()
                    tx_good = int.from_bytes(payload[0:4], "little")
                    tx_bad  = int.from_bytes(payload[4:8], "little")
                    draw_bar(received, expected, bad_count, bps, start_time)
                    print(f"\nEOT: TX reports {tx_good} good, {tx_bad} bad this pass.")
                    break

                elif t == TYPE_INFO:
                    # TX missed our worklist ack and is re-handshaking. Consume
                    # the rest of its INFO frame and resend the command.
                    link.read_exact(14)                   # 12 payload + 2 crc
                    link.send_frame(TYPE_CMD, cmd_payload)

                else:
                    link.nak()

            except TimeoutError:
                # No frame in time. Don't spam NAKs into a talking sender;
                # just keep listening. TX resends if it's waiting on us.
                continue

    except KeyboardInterrupt:
        print("\nInterrupted - flushing image and writing map so far.")

    finally:
        f.flush()
        os.fsync(f.fileno())
        f.close()
        write_mapfile(mapfile, statusmap, bps, total)
        write_badlist(badlist, statusmap)
        ser.close()

    good = sum(1 for s in statusmap if s == GOOD)
    bad = sum(1 for s in statusmap if s == BAD)
    untried = sum(1 for s in statusmap if s == UNTRIED)
    print(f"\nSummary: {good} good, {bad} bad, {untried} not received.")
    print(f"Image:    {args.image} ({disk_bytes} bytes)")
    print(f"Mapfile:  {mapfile}")
    print(f"Badlist:  {badlist}")
    if bad:
        sample = [str(i) for i, s in enumerate(statusmap) if s == BAD][:8]
        print(f"First bad LBAs: {', '.join(sample)}"
              + (" ..." if bad > len(sample) else ""))

    if args.verify:
        verify_image(args.image, statusmap, bps, total)


if __name__ == "__main__":
    main()
