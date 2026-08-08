/**
 * tx-rescue - Direct 8250/16550 UART serial routines (polled, no FOSSIL).
 *
 * Polled programmed I/O so no driver (X00/BNU) is required. The 16550 FIFO
 * is enabled when present. TX is pure THRE polling (safe at any baud); RX is
 * only ever one ACK/NAK byte per frame, so overruns are a non-issue.
 */
#ifndef SERIAL_H
#define SERIAL_H

/* Standard COM port I/O bases. Pick one with serial_init(). */
#define COM1_BASE 0x3F8
#define COM2_BASE 0x2F8
#define COM3_BASE 0x3E8
#define COM4_BASE 0x2E8

/* Initialise the UART: 8 data bits, no parity, 1 stop bit, given baud.
 * baud is a real rate (e.g. 115200L, 57600L, 38400L, 19200L, 9600L). */
void serial_init(unsigned int base, unsigned long baud);

/* Block until the transmitter holding register is empty, then send one byte. */
void serial_send(unsigned char b);

/* Receive one byte with a timeout in ~55ms BIOS ticks.
 * Returns 0 on success (byte in *out), 1 on timeout. */
int  serial_recv(unsigned char* out, unsigned int timeout_ticks);

/* Drain any pending input (used to resync before a fresh handshake). */
void serial_flush_input(void);

#endif /* SERIAL_H */
