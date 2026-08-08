/**
 * CRC-16/CCITT-FALSE  (poly 0x1021, init 0xFFFF, no reflection, xorout 0).
 * Same algorithm on both ends so frames verify identically.
 */
#ifndef CRC16_H
#define CRC16_H

unsigned int crc16_update(unsigned int crc, unsigned char b);

#endif /* CRC16_H */
