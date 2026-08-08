/**
 * tx-rescue <-> rx-host wire protocol.
 *
 * Stop-and-wait, transmitter-driven. Every field is serialised little-endian,
 * byte by byte, so 16-bit Watcom and host Python agree with no struct packing
 * surprises. CRC-16/CCITT-FALSE covers TYPE + payload of each frame.
 *
 *   INFO  (tx->rx, once):  SOH 'I'  total_lba(4) cyls(2) heads(2) spt(2) bps(2)  crc(2)
 *   CMD   (rx->tx, once):  SOH 'C'  nranges(2) [start(4) end(4)]...            crc(2)
 *   DATA  (tx->rx, /sec):  SOH 'D'  lba(4) status(1) data(bps)                 crc(2)
 *   EOT   (tx->rx, once):  SOH 'E'  good(4) bad(4)                             crc(2)
 *
 *   rx->tx replies during the DATA phase are a single byte: ACK or NAK.
 *
 * Control flow: TX sends INFO; the host replies with a CMD worklist of LBA
 * ranges (the whole disk on a first pass, or just the bad/untried runs on
 * --resume). TX images those ranges in order. There is NO command-level ack:
 * if TX misses the worklist it re-sends INFO, and the host answers by
 * re-sending CMD. end_lba is exclusive; nranges <= MAXR.
 *
 * status: 0 = good sector data follows; 1 = BAD sector, data is the fill
 * pattern. rx writes the payload at lba*bps either way, so the image stays
 * byte-aligned, and records bad LBAs in a ddrescue-style mapfile.
 */
#ifndef PROTO_H
#define PROTO_H

#define SOH  0x01
#define ACK  0x06
#define NAK  0x15

#define TYPE_INFO 'I'
#define TYPE_CMD  'C'
#define TYPE_DATA 'D'
#define TYPE_EOT  'E'

#define STATUS_GOOD 0x00
#define STATUS_BAD  0x01

#define BPS        512      /* bytes per sector                             */
#define READ_TRIES 5        /* INT 13h attempts before a sector is "bad"    */
#define BAD_FILL   0x00     /* fill byte written for unreadable sectors      */
#define MAXR       256      /* max worklist ranges (must match rx.py)        */

/* Timeouts / retry budget for the serial link (BIOS ticks, ~55ms each). */
#define ACK_TIMEOUT_TICKS 36   /* ~2s to hear an ACK/NAK                     */
#define LINK_TRIES        8    /* frame resends before giving up on the link*/

#endif /* PROTO_H */
