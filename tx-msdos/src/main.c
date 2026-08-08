/**
 * tx-rescue - MS-DOS side of a ddrescue-style whole-disk serial imager.
 *
 * Reads the first hard disk (0x80) sector by sector over INT 13h and streams
 * each sector to the host. Unreadable sectors are retried, then STUBBED (a
 * fill pattern is sent, flagged bad) so imaging never aborts on a bad block.
 *
 * The host decides what to image: after we send INFO, it replies with a CMD
 * worklist of LBA ranges (the whole disk on a first pass, or just the bad/
 * untried runs on --resume). We image those ranges in order. No command-level
 * ack: if we miss the worklist we re-send INFO and the host re-sends CMD.
 *
 * This C build is protocol-identical to the ready-made TX.COM (see tx.S); the
 * shared protocol is validated by the rx.py co-simulation. Build with
 * OpenWatcom:  wmake   (see Makefile) or  build.bat
 *
 * Usage:  TX [-pPORT] [-bBAUD]
 *   -p1..4   COM port   (default 1)
 *   -b       baud       (default 9600; raise once the link is proven)
 *
 * Licensed under GPL Version 3.0
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <conio.h>
#include <i86.h>

#include "int13.h"
#include "serial.h"
#include "crc16.h"
#include "proto.h"

static char sector_buf[BPS];
static unsigned long range_start[MAXR];
static unsigned long range_end[MAXR];
static unsigned int  nranges;

static unsigned long far* const BIOS_TICKS =
    (unsigned long far*) MK_FP(0x0040, 0x006C);

/* --- one framed byte: send it and fold it into the running CRC ----------- */
static unsigned int put(unsigned int crc, unsigned char b)
{
    serial_send(b);
    return crc16_update(crc, b);
}

/* Wait for a single ACK/NAK. Returns ACK, NAK, or 0 on timeout. */
static unsigned char wait_reply(void)
{
    unsigned char r;
    if (serial_recv(&r, ACK_TIMEOUT_TICKS) != 0)
        return 0;
    return r;
}

/* Receive one byte with timeout. 1 on success (into *b), 0 on timeout. */
static int recv_byte(unsigned char* b)
{
    return serial_recv(b, ACK_TIMEOUT_TICKS) == 0;
}

/* Transmit one INFO frame (no ack wait). */
static void tx_info(unsigned long total, unsigned int cyls,
                    unsigned int heads, unsigned int spt)
{
    unsigned int crc = 0xFFFF;
    serial_flush_input();
    serial_send(SOH);
    crc = put(crc, TYPE_INFO);
    crc = put(crc, (unsigned char)(total        & 0xFF));
    crc = put(crc, (unsigned char)((total >>  8)& 0xFF));
    crc = put(crc, (unsigned char)((total >> 16)& 0xFF));
    crc = put(crc, (unsigned char)((total >> 24)& 0xFF));
    crc = put(crc, (unsigned char)(cyls  & 0xFF));
    crc = put(crc, (unsigned char)(cyls  >> 8));
    crc = put(crc, (unsigned char)(heads & 0xFF));
    crc = put(crc, (unsigned char)(heads >> 8));
    crc = put(crc, (unsigned char)(spt   & 0xFF));
    crc = put(crc, (unsigned char)(spt   >> 8));
    crc = put(crc, (unsigned char)(BPS   & 0xFF));
    crc = put(crc, (unsigned char)(BPS   >> 8));
    serial_send((unsigned char)(crc & 0xFF));
    serial_send((unsigned char)((crc >> 8) & 0xFF));
}

/* Receive the CMD worklist. 1 on success (fills ranges/nranges), 0 on fail. */
static int recv_command(void)
{
    unsigned char b, lo, hi;
    unsigned int  crc, hunt, count, i;

    for (hunt = 0; hunt < 300; hunt++) {
        if (!recv_byte(&b)) return 0;
        if (b == SOH) break;
    }
    if (hunt >= 300) return 0;

    if (!recv_byte(&b) || b != (unsigned char)TYPE_CMD) return 0;
    crc = crc16_update(0xFFFF, (unsigned char)TYPE_CMD);

    if (!recv_byte(&lo)) return 0;  crc = crc16_update(crc, lo);
    if (!recv_byte(&hi)) return 0;  crc = crc16_update(crc, hi);
    count = (unsigned int)lo | ((unsigned int)hi << 8);
    if (count > MAXR) return 0;

    for (i = 0; i < count; i++) {
        unsigned char p[8];
        int k;
        unsigned long s = 0, e = 0;
        for (k = 0; k < 8; k++) {
            if (!recv_byte(&p[k])) return 0;
            crc = crc16_update(crc, p[k]);
        }
        s = (unsigned long)p[0] | ((unsigned long)p[1] << 8) |
            ((unsigned long)p[2] << 16) | ((unsigned long)p[3] << 24);
        e = (unsigned long)p[4] | ((unsigned long)p[5] << 8) |
            ((unsigned long)p[6] << 16) | ((unsigned long)p[7] << 24);
        range_start[i] = s;
        range_end[i]   = e;
    }

    if (!recv_byte(&lo)) return 0;
    if (!recv_byte(&hi)) return 0;
    if (((unsigned int)lo | ((unsigned int)hi << 8)) != crc) return 0;

    nranges = count;
    return 1;
}

/* Send INFO, receive worklist; retry until a valid CMD arrives. */
static void handshake(unsigned long total, unsigned int cyls,
                      unsigned int heads, unsigned int spt)
{
    do {
        tx_info(total, cyls, heads, spt);
    } while (!recv_command());
}

/* Send one DATA frame for lba and wait for ACK, resending on NAK/timeout. */
static int send_data(unsigned long lba, unsigned char status, const char* buf)
{
    int attempt;
    for (attempt = 0; attempt < LINK_TRIES; attempt++) {
        unsigned int crc = 0xFFFF;
        int i;
        serial_send(SOH);
        crc = put(crc, TYPE_DATA);
        crc = put(crc, (unsigned char)(lba        & 0xFF));
        crc = put(crc, (unsigned char)((lba >>  8)& 0xFF));
        crc = put(crc, (unsigned char)((lba >> 16)& 0xFF));
        crc = put(crc, (unsigned char)((lba >> 24)& 0xFF));
        crc = put(crc, status);
        for (i = 0; i < BPS; i++)
            crc = put(crc, (unsigned char)buf[i]);
        serial_send((unsigned char)(crc & 0xFF));
        serial_send((unsigned char)((crc >> 8) & 0xFF));

        if (wait_reply() == ACK) return 1;
    }
    return 0;
}

static int send_eot(unsigned long good, unsigned long bad)
{
    int attempt;
    for (attempt = 0; attempt < LINK_TRIES; attempt++) {
        unsigned int crc = 0xFFFF;
        serial_send(SOH);
        crc = put(crc, TYPE_EOT);
        crc = put(crc, (unsigned char)(good        & 0xFF));
        crc = put(crc, (unsigned char)((good >>  8)& 0xFF));
        crc = put(crc, (unsigned char)((good >> 16)& 0xFF));
        crc = put(crc, (unsigned char)((good >> 24)& 0xFF));
        crc = put(crc, (unsigned char)(bad         & 0xFF));
        crc = put(crc, (unsigned char)((bad >>  8) & 0xFF));
        crc = put(crc, (unsigned char)((bad >> 16) & 0xFF));
        crc = put(crc, (unsigned char)((bad >> 24) & 0xFF));
        serial_send((unsigned char)(crc & 0xFF));
        serial_send((unsigned char)((crc >> 8) & 0xFF));
        if (wait_reply() == ACK) return 1;
    }
    return 0;
}

/* Read one sector with retries + controller reset. Returns 0 if good. */
static unsigned char read_with_retries(short c, unsigned char h,
                                       unsigned char s)
{
    int t;
    unsigned char rc = 0xFF;
    for (t = 0; t < READ_TRIES; t++) {
        rc = int13_read_sector(c, h, s, (char far*)sector_buf);
        if (rc == 0) return 0;
        int13_reset();
    }
    return rc;
}

static void draw_progress(unsigned long done, unsigned long total,
                          short c, unsigned char h, unsigned char s,
                          unsigned long good, unsigned long bad,
                          unsigned long start_ticks)
{
    unsigned long ticks = *BIOS_TICKS - start_ticks;
    unsigned long secs  = (ticks * 10UL) / 182UL;
    unsigned long pct   = total ? (done * 100UL) / total : 0;
    unsigned long kbps  = 0, eta = 0, rate = 0;

    if (secs > 0) {
        kbps = (done / 2UL) / secs;
        rate = done / secs;
    }
    if (rate > 0 && total >= done)
        eta = (total - done) / rate;

    printf("\r%3lu%% %lu/%lu C=%u H=%u S=%u ok=%lu bad=%lu %luKB/s ETA %lu:%02lu  ",
           pct, done, total, (unsigned)c, (unsigned)h, (unsigned)s,
           good, bad, kbps, eta / 60UL, eta % 60UL);
    fflush(stdout);
}

int main(int argc, char** argv)
{
    DiskGeometry geo;
    unsigned int  port = 1, cyls, heads, spt, r;
    unsigned long baud = 9600L;
    unsigned long total, todo = 0, done = 0;
    unsigned long lba, good = 0, bad = 0, start_ticks;
    unsigned int  bases[5] = {0, COM1_BASE, COM2_BASE, COM3_BASE, COM4_BASE};
    int i;

    for (i = 1; i < argc; i++) {
        char* a = argv[i];
        if ((a[0] == '-' || a[0] == '/') && a[1]) {
            char opt = a[1];
            char* v  = a + 2;
            switch (opt) {
                case 'p': case 'P': port = (unsigned)atoi(v); break;
                case 'b': case 'B': baud = atol(v);           break;
                default: printf("Unknown option %s\n", a); return 1;
            }
        }
    }
    if (port < 1 || port > 4) { printf("Port must be 1..4\n"); return 1; }

    printf("tx-rescue: imaging HD0 (0x80) over COM%u @ %lu baud\n", port, baud);

    if (int13_disk_geometry(&geo)) {
        printf("ERROR: INT 13h AH=08h failed (no disk 0x80?)\n");
        return 1;
    }
    cyls  = (unsigned int)(geo.c + 1);
    heads = (unsigned int)(geo.h + 1);
    spt   = (unsigned int) geo.s;
    total = (unsigned long)cyls * heads * spt;

    printf("Geometry: %u cyl x %u head x %u sec = %lu sectors (%lu MB)\n",
           cyls, heads, spt, total, (total / 2048UL));
    if (cyls > 1024)
        printf("WARN: >1024 cylinders; CHS INT 13h can't reach them all.\n");

    serial_init(bases[port], baud);

    printf("Waiting for receiver... (start 'rx.py' on the host now)\n");
    handshake(total, cyls, heads, spt);

    for (r = 0; r < nranges; r++)
        todo += range_end[r] - range_start[r];
    printf("Linked. Worklist: %u range(s), %lu sectors.\n", nranges, todo);

    start_ticks = *BIOS_TICKS;

    for (r = 0; r < nranges; r++) {
        for (lba = range_start[r]; lba < range_end[r]; lba++) {
            unsigned char s = (unsigned char)(lba % spt) + 1;
            unsigned long t = lba / spt;
            unsigned char h = (unsigned char)(t % heads);
            short         c = (short)(t / heads);
            unsigned char status = STATUS_GOOD;

            if (kbhit()) {                       /* ESC or Ctrl-C aborts */
                int key = getch();
                if (key == 0x1B || key == 0x03) {
                    send_eot(good, bad);         /* let the host finalize */
                    printf("\nAborted by user. Exiting to DOS.\n");
                    return 0;
                }
            }

            if (read_with_retries(c, h, s) != 0) {
                memset(sector_buf, BAD_FILL, BPS);
                status = STATUS_BAD;
                bad++;
                putchar('B'); fflush(stdout);   /* flag bad block on console */
            } else {
                good++;
            }

            if (!send_data(lba, status, sector_buf)) {
                printf("\nERROR: serial link lost at LBA %lu. Aborting.\n", lba);
                return 3;
            }

            done++;
            if (status == STATUS_BAD || (done & 0x3F) == 0)
                draw_progress(done, todo, c, h, s, good, bad, start_ticks);
        }
    }

    draw_progress(done, todo, 0, 0, 0, good, bad, start_ticks);
    send_eot(good, bad);

    printf("\nDone. %lu good, %lu bad (%lu this pass).\n", good, bad, good + bad);
    if (bad)
        printf("Bad sectors were stubbed; see the host mapfile for locations.\n");
    return 0;
}
