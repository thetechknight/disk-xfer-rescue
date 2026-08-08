#include "crc16.h"

/* Bitwise CRC-16/CCITT-FALSE. No 512-byte table, negligible cost per sector. */
unsigned int crc16_update(unsigned int crc, unsigned char b)
{
    int i;
    crc ^= (unsigned int)b << 8;
    for (i = 0; i < 8; i++)
        crc = (crc & 0x8000) ? (unsigned int)((crc << 1) ^ 0x1021)
                             : (unsigned int)(crc << 1);
    return crc;
}
