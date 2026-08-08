/**
 * tx-rescue - INT 13h (BIOS disk) routines
 *
 * Original geometry/CHS logic: Thomas Cherryhomes <thom.cherryhomes@gmail.com>
 * (tschak909/disk-xfer), including the cyl-high fix discussed on VCFED.
 * Additions here: int13_reset(), and int86x so the read buffer's segment is
 * passed in ES explicitly (works in small OR large model).
 *
 * Licensed under GPL Version 3.0
 */
#include <i86.h>
#include "int13.h"

static union REGS  regs;
static struct SREGS sregs;

/**
 * Get disk geometry for the first hard disk (0x80).
 */
unsigned char int13_disk_geometry(DiskGeometry* geometry)
{
    regs.h.ah = AH_GET_DRIVE_PARAMETERS;
    regs.h.dl = 0x80;
    int86(0x13, &regs, &regs);

    /* Unpack disk geometry. */
    geometry->c  = regs.h.ch;                 /* low 8 bits of max cylinder   */
    geometry->c |= ((regs.h.cl) & 0xC0) << 2; /* high 2 bits of max cylinder  */
    geometry->h  = regs.h.dh;                 /* max head number              */
    geometry->s  = regs.h.cl & 0x3F;          /* sectors per track (1-based)  */

    return regs.x.cflag;                      /* 0 = ok, 1 = fail             */
}

/**
 * Read one sector given CHS into a (far) buffer.
 */
unsigned char int13_read_sector(short c, unsigned char h, unsigned char s,
                                char far* buf)
{
    segread(&sregs);                 /* start from current segment regs      */

    regs.h.ah = AH_READ_DISK_SECTORS;
    regs.h.al = 1;                   /* one sector                            */
    regs.h.dh = h;
    regs.h.dl = 0x80;                /* first hard disk                       */
    regs.x.bx = FP_OFF(buf);         /* ES:BX = buffer                        */
    sregs.es  = FP_SEG(buf);
    regs.h.ch = c & 0xFF;            /* cyl low                               */
    regs.h.cl = s;                   /* sector (bits 5-0)                     */
    regs.h.cl |= ((c >> 2) & 0xC0);  /* cyl high (bits 7-6)                   */

    int86x(0x13, &regs, &regs, &sregs);

    /* On error AH holds a status code; return it (non-zero) so callers can
     * log it. On success cflag is clear and AH is 0. */
    return regs.x.cflag ? (regs.h.ah ? regs.h.ah : 0xFF) : 0;
}

/**
 * Reset / recalibrate the disk controller for drive 0x80.
 */
unsigned char int13_reset(void)
{
    regs.h.ah = AH_RESET_DISK;
    regs.h.dl = 0x80;
    int86(0x13, &regs, &regs);
    return regs.x.cflag;
}
