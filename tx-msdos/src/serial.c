/**
 * tx-rescue - Direct 8250/16550 UART serial routines (polled).
 * Licensed under GPL Version 3.0
 */
#include <i86.h>
#include <conio.h>
#include "serial.h"

/* UART register offsets from the port base */
#define REG_RBR 0  /* Receiver Buffer (read),  DLL when DLAB=1  */
#define REG_THR 0  /* Transmit Holding (write)                  */
#define REG_IER 1  /* Interrupt Enable,        DLM when DLAB=1  */
#define REG_FCR 2  /* FIFO Control (write)                      */
#define REG_LCR 3  /* Line Control                              */
#define REG_MCR 4  /* Modem Control                             */
#define REG_LSR 5  /* Line Status                               */

#define LSR_DR   0x01  /* Data Ready                            */
#define LSR_THRE 0x20  /* Transmit Holding Register Empty       */

static unsigned int uart = COM1_BASE;

/* BIOS 18.2 Hz tick counter at 0040:006C (one dword). */
static unsigned long far* const BIOS_TICKS =
    (unsigned long far*) MK_FP(0x0040, 0x006C);

void serial_init(unsigned int base, unsigned long baud)
{
    unsigned int divisor;

    uart = base;
    if (baud == 0) baud = 115200L;
    divisor = (unsigned int)(115200L / baud);
    if (divisor == 0) divisor = 1;

    outp(uart + REG_IER, 0x00);            /* no interrupts, we poll         */
    outp(uart + REG_LCR, 0x80);            /* DLAB=1 to set baud divisor     */
    outp(uart + REG_RBR, divisor & 0xFF);  /* divisor low                    */
    outp(uart + REG_IER, (divisor >> 8) & 0xFF); /* divisor high             */
    outp(uart + REG_LCR, 0x03);            /* DLAB=0, 8N1                     */
    /* Enable & clear FIFOs, 14-byte trigger (ignored by plain 8250).        */
    outp(uart + REG_FCR, 0xC7);
    outp(uart + REG_MCR, 0x0B);            /* DTR, RTS, OUT2                  */
    serial_flush_input();
}

void serial_send(unsigned char b)
{
    while ((inp(uart + REG_LSR) & LSR_THRE) == 0)
        ;                                  /* wait for THR empty             */
    outp(uart + REG_THR, b);
}

int serial_recv(unsigned char* out, unsigned int timeout_ticks)
{
    unsigned long start = *BIOS_TICKS;

    for (;;) {
        if (inp(uart + REG_LSR) & LSR_DR) {
            *out = (unsigned char) inp(uart + REG_RBR);
            return 0;
        }
        /* Coarse timeout using the BIOS tick counter (~55ms each). */
        if (timeout_ticks && (*BIOS_TICKS - start) >= timeout_ticks)
            return 1;
    }
}

void serial_flush_input(void)
{
    while (inp(uart + REG_LSR) & LSR_DR)
        (void) inp(uart + REG_RBR);
}
