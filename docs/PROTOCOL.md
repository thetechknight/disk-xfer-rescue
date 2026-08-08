# Wire protocol

A stop-and-wait, transmitter-driven protocol between the DOS **sender**
(`TX.COM`, running on the vintage box) and the **host** receiver (`rx.py`,
running on the modern machine). Every multi-byte field is little-endian and
sent byte by byte, so 16-bit code and host Python agree with no struct-packing
surprises. A CRC-16/CCITT-FALSE covers the TYPE byte plus the payload of every
framed message.

## Constants

| Name | Value | Meaning |
|------|-------|---------|
| `SOH` | `0x01` | start of a framed message |
| `ACK` | `0x06` | host accepted the last DATA frame |
| `NAK` | `0x15` | host rejected it (CRC mismatch); TX resends |
| `TYPE_INFO` | `'I'` | geometry announcement |
| `TYPE_CMD`  | `'C'` | worklist of LBA ranges |
| `TYPE_DATA` | `'D'` | one sector |
| `TYPE_EOT`  | `'E'` | end of transfer |
| `STATUS_GOOD` | `0x00` | sector read OK; real data follows |
| `STATUS_BAD`  | `0x01` | sector unreadable; fill pattern follows |
| `BPS` | `512` | bytes per sector |
| `READ_TRIES` | `5` | INT 13h attempts before a sector is declared bad |
| `MAXR` | `256` | maximum worklist ranges (host and TX must agree) |

## CRC-16/CCITT-FALSE

Polynomial `0x1021`, initial value `0xFFFF`, no reflection, no final XOR.
Check value: the ASCII string `123456789` yields `0x29B1`. The CRC is computed
over the TYPE byte and every payload byte of a frame (not the `SOH`, not the
CRC field itself).

## Frames

```
INFO   (TX -> host, once)
  SOH  'I'  total_lba(4)  cyls(2)  heads(2)  spt(2)  bps(2)  crc(2)

CMD    (host -> TX, once, in reply to INFO)
  SOH  'C'  nranges(2)  [ start_lba(4)  end_lba(4) ] * nranges  crc(2)
           (end_lba is EXCLUSIVE; nranges <= MAXR)

DATA   (TX -> host, once per sector)
  SOH  'D'  lba(4)  status(1)  data(BPS)  crc(2)
           -> host replies with a single byte: ACK or NAK

EOT    (TX -> host, once)
  SOH  'E'  good(4)  bad(4)  crc(2)
```

## Control flow

1. TX reads geometry via INT 13h AH=08 and sends **INFO**.
2. The host replies with a **CMD** worklist: the whole disk `[(0, total)]` on a
   first pass, or just the bad/untried runs on `--resume`.
3. TX images each range in order, sending a **DATA** frame per sector. The host
   writes the payload at `lba * BPS` (so the image stays byte-aligned whether
   the sector was good or bad) and answers each frame with ACK/NAK.
4. When every range is done, TX sends **EOT** with the good/bad totals.

There is **no command-level ACK**, which sidesteps the classic two-army
lost-ack problem: if TX never sees the worklist it simply re-sends INFO, and
the host, seeing INFO again during its receive loop, re-sends CMD. The first
DATA frame is TX's implicit acknowledgement that it got the worklist.

## Host-side bookkeeping

The host writes a ddrescue-compatible mapfile alongside the image: `+` for
recovered sectors, `-` for bad, `?` for untried. `--resume` reads that mapfile
back and asks only for the runs still marked bad or untried, coalescing them to
at most `MAXR` ranges by merging across the smallest gaps. A companion
`.badblocks` file lists the bad LBAs.

## Restore (write) mode

Restore reuses the same frames with the DATA direction reversed. The DOS-side
writer `WR.COM` plays the role `TX.COM` does for reads:

1. `WR.COM` reads the target geometry and sends **INFO** (same frame).
2. The host (`rx.py --restore`) replies with a **CMD** worklist.
3. Now **DATA flows host -> DOS**: the host reads each sector from the image and
   sends a DATA frame; `WR.COM` CRC-checks it, writes it with INT 13h AH=03
   (retry then stub on failure), and replies ACK once the write is done (or NAK
   on a CRC error, which makes the host resend). ACK here therefore means
   "received and written", so the host allows a generous timeout for it - a
   failing sector spends several retry cycles before `WR.COM` gives up and acks.
4. When the worklist is exhausted, `WR.COM` sends **EOT** with the count of
   sectors written and sectors it failed to write. Any non-zero bad count is a
   *target-drive* write failure and the host raises the "drive may be
   unreliable" alert.

**Lost-ACK de-dup.** If a `WR.COM` ACK is lost, the host resends the same DATA
frame. `WR.COM` compares each frame's LBA to the sector it currently expects; a
frame for the already-written previous LBA is recognised as a resend and simply
re-ACKed, never written again. This keeps writes idempotent and correctly
aligned, the same property the read path gets for free by writing to the frame's
LBA on the host.

## Notes for reimplementers

- Keep `MAXR` identical on both ends.
- `bps` is negotiated only in the sense that the host trusts the INFO value; the
  reference host assumes 512 and the sender sends 512.
- The sender targets the first BIOS hard disk (`0x80`) through CHS INT 13h, so
  it is limited to drives the BIOS can describe in CHS (<= 1024 cylinders).
