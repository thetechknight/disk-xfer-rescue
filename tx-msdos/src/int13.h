/**
 * tx-rescue - INT 13h (BIOS disk) routines
 *
 * Based on tschak909/disk-xfer (GPLv3), with the CHS packing fix from the
 * VCFED thread and additions for bad-sector-tolerant imaging.
 */
#ifndef INT13_H
#define INT13_H

#define AH_GET_DRIVE_PARAMETERS 0x08
#define AH_READ_DISK_SECTORS    0x02
#define AH_RESET_DISK           0x00

/* Raw geometry as returned by INT 13h AH=08h.
 * NOTE: c and h are MAX (0-based) values; s is the sector COUNT (1-based). */
typedef struct {
    short         c;   /* max cylinder number (0-based)   */
    unsigned char h;   /* max head number     (0-based)   */
    unsigned char s;   /* sectors per track   (count)     */
} DiskGeometry;

/* Fill *geometry for drive 0x80. Returns 0 on success, 1 on failure. */
unsigned char int13_disk_geometry(DiskGeometry* geometry);

/* Read ONE sector at C/H/S into buf (far so it works in any memory model).
 * Returns 0 on success, non-zero (BIOS AH error code) on failure. */
unsigned char int13_read_sector(short c, unsigned char h, unsigned char s,
                                char far* buf);

/* Recalibrate/reset drive 0x80 (INT 13h AH=00h). Call between read retries. */
unsigned char int13_reset(void);

#endif /* INT13_H */
