/*
 *  TARGET: Intel 8086/8088.  Built with  .arch i8086  so the assembler
 *  refuses any 186/286/386 instruction (e.g. shift-by-immediate, near Jcc,
 *  push-immediate).  This is deliberate: the tool exists for vintage iron.
 *  Do NOT raise the arch to i286/i386 to silence an error - fix the code to
 *  use 8086-only instructions instead (repeat shl/shr by 1, load imm to a reg
 *  before push, etc.).  A single stray 286+ opcode hangs an 8086/286 hard.
 tx.S - disk-xfer-rescue MS-DOS sender (.COM), 16-bit real mode, 8086/286.
 *
 * Reads hard disk 0x80 sector-by-sector via INT 13h and streams each sector to
 * the host using the same framed protocol the tested rx.py expects:
 *   INFO  SOH 'I' total(4) cyls(2) heads(2) spt(2) bps(2) crc(2)
 *   DATA  SOH 'D' lba(4) status(1) data(512) crc(2)   -> host ACK/NAK
 *   EOT   SOH 'E' good(4) bad(4) crc(2)
 * CRC-16/CCITT-FALSE. Unreadable sectors are retried then STUBBED (status=1,
 * zero-filled) so imaging never aborts. READ ONLY on the source disk.
 *
 * Usage on DOS:  TX [port] [baudcode]
 *   port     1..4  -> COM1..COM4     (default 1)
 *   baudcode 1..7  -> baud           (default 1 = 9600, safe for a 286)
 *       1=9600 2=19200 3=38400 4=57600 5=115200 6=4800 7=2400
 *   e.g.  TX 1 2   = COM1 @ 19200
 *
 * Assembled with GNU as/ld to a flat .COM image, then delivered as a DEBUG
 * script (MAKETX.SCR) so no toolchain is needed on the target.
 */
    .code16
    .arch i8086
    .intel_syntax noprefix
    .text
    .org 0

/* ---- constants ---- */
    .equ SOH, 0x01
    .equ ACK, 0x06
    .equ NAK, 0x15
    .equ BPS, 512
    .equ READ_TRIES, 5
    .equ LINK_TRIES, 16
    .equ TMO_TICKS, 36
    .equ MAXR, 256
    .equ HUNT_MAX, 300          /* ~2s at 18.2 Hz */

_start:
    /* COM: CS=DS=ES=SS on entry. Set up a stack near top of segment. */
    cli
    mov sp, 0xFFFE
    sti
    cld

    /* ---------- parse command tail (defaults: COM1, 9600) ---------- */
    mov word ptr [portbase], 0x3F8
    mov word ptr [divisor], 12
    mov si, 0x81
    mov cl, [0x80]
    xor ch, ch
    /* port digit */
    call next_digit
    jc  parsed
    sub al, '0'
    cmp al, 1
    jb  parse_baud
    cmp al, 4
    ja  parse_baud
    dec al
    mov bl, al
    xor bh, bh
    shl bx, 1
    mov ax, [bases + bx]
    mov [portbase], ax
parse_baud:
    call next_digit
    jc  parsed
    sub al, '0'
    cmp al, 1
    jb  parsed
    cmp al, 7
    ja  parsed
    dec al
    mov bl, al
    xor bh, bh
    shl bx, 1
    mov ax, [divtab + bx]
    mov [divisor], ax
parsed:

    call uart_init

    /* ---------- geometry ---------- */
    call get_geom
    jnc  geom_ok
    mov dx, offset msg_nogeo
    mov ah, 0x09
    int 0x21
    mov ax, 0x4C01
    int 0x21
geom_ok:

    mov dx, offset msg_wait
    mov ah, 0x09
    int 0x21

    /* ---------- INFO handshake (retry until ACK) ---------- */
    call handshake

    mov dx, offset msg_link
    mov ah, 0x09
    int 0x21

    /* ---------- image each range in the received worklist ---------- */
    xor ax, ax
    mov [good_lo], ax
    mov [good_hi], ax
    mov [bad_lo], ax
    mov [bad_hi], ax
    mov [ri], ax

next_range:
    mov ax, [ri]
    cmp ax, [nranges]
    jae all_done
    mov bx, ax
    shl bx, 1
    shl bx, 1
    shl bx, 1
    add bx, offset ranges
    mov ax, [bx]
    mov [lba_lo], ax
    mov ax, [bx+2]
    mov [lba_hi], ax
    mov ax, [bx+4]
    mov [end_lo], ax
    mov ax, [bx+6]
    mov [end_hi], ax

range_loop:
    /* if lba >= end, advance to next range */
    mov ax, [lba_hi]
    cmp ax, [end_hi]
    jb  do_sector
    ja  range_next
    mov ax, [lba_lo]
    cmp ax, [end_lo]
    jb  do_sector
    jmp range_next

do_sector:
    call check_abort
    call lba_to_chs
    call read_sector            /* sets [status]; fills buf (data or stub)   */

    cmp byte ptr [status], 0
    jne is_bad
    add word ptr [good_lo], 1
    adc word ptr [good_hi], 0
    jmp send_it
is_bad:
    add word ptr [bad_lo], 1
    adc word ptr [bad_hi], 0
    mov dl, 'B'
    mov ah, 0x02
    int 0x21
send_it:
    call send_data              /* aborts program if the link is truly dead   */

    /* advance lba (32-bit) */
    add word ptr [lba_lo], 1
    adc word ptr [lba_hi], 0

    /* crude progress: '.' every 256 sectors */
    mov ax, [lba_lo]
    test al, al
    jnz range_loop
    mov dl, '.'
    mov ah, 0x02
    int 0x21
    jmp range_loop

range_next:
    inc word ptr [ri]
    jmp next_range
all_done:
finish:
    call send_eot
    mov dx, offset msg_done
    mov ah, 0x09
    int 0x21
    mov ax, 0x4C00
    int 0x21


/* check_abort (direct BIOS kbd buffer): abort on ESC or Ctrl-C, no INT */
check_abort:
    push ax
    push bx
    push es
    mov ax, 0x40
    mov es, ax
    mov bx, es:[0x1A]
    cmp bx, es:[0x1C]
    je  ca_ret
    mov ax, es:[bx]
    mov bx, es:[0x1C]
    mov es:[0x1A], bx
    cmp al, 0x1B
    je  ca_abort
    cmp al, 0x03
    je  ca_abort
ca_ret:
    pop es
    pop bx
    pop ax
    ret
ca_abort:
    pop es
    pop bx
    pop ax
    call send_eot
    mov dx, offset msg_abort
    mov ah, 0x09
    int 0x21
    mov ax, 0x4C03
    int 0x21

/* ================= subroutines ================= */

/* next_digit: scan [si..], cx bytes remaining, for next ASCII digit.
 * Returns CF=0, AL=digit char, si/cx advanced; CF=1 if none left. */
next_digit:
    jcxz nd_none
    mov al, [si]
    inc si
    dec cx
    cmp al, '0'
    jb  next_digit
    cmp al, '9'
    ja  next_digit
    clc
    ret
nd_none:
    stc
    ret

/* uart_init: 8N1, divisor from [divisor], FIFO on, DTR/RTS/OUT2. */
uart_init:
    mov dx, [portbase]
    add dx, 3
    mov al, 0x80                /* DLAB */
    out dx, al
    mov dx, [portbase]
    mov ax, [divisor]
    out dx, al                  /* DLL = divisor low */
    mov dx, [portbase]
    inc dx
    mov al, ah                  /* DLM = divisor high */
    out dx, al
    mov dx, [portbase]
    add dx, 3
    mov al, 0x03                /* 8N1, DLAB off */
    out dx, al
    mov dx, [portbase]
    add dx, 2
    mov al, 0xC7                /* enable+clear FIFOs */
    out dx, al
    mov dx, [portbase]
    add dx, 4
    mov al, 0x0B                /* DTR|RTS|OUT2 */
    out dx, al
    call flush_input
    ret

flush_input:
    mov dx, [portbase]
    add dx, 5
fi_l:
    in  al, dx
    test al, 0x01
    jz  fi_done
    mov dx, [portbase]
    in  al, dx
    mov dx, [portbase]
    add dx, 5
    jmp fi_l
fi_done:
    ret

/* send: AL = byte, blocking on THRE. */
send:
    push bx
    push dx
    mov bl, al
    mov dx, [portbase]
    add dx, 5
snd_w:
    in  al, dx
    test al, 0x20
    jz  snd_w
    mov dx, [portbase]
    mov al, bl
    out dx, al
    pop dx
    pop bx
    ret

/* recv_ack: wait for one byte, ~2s timeout via BIOS tick 0040:006C.
 * CF=0, AL=byte on success; CF=1 on timeout. */
recv_ack:
    push bx
    push cx
    push dx
    push es
    xor ax, ax
    mov es, ax
    mov bx, es:[0x6c]           /* start tick (low word) */
ra_l:
    mov dx, [portbase]
    add dx, 5
    in  al, dx
    test al, 0x01
    jnz ra_got
    mov cx, es:[0x6c]
    sub cx, bx
    cmp cx, TMO_TICKS
    jae ra_tmo
    jmp ra_l
ra_got:
    mov dx, [portbase]
    in  al, dx
    clc
    jmp ra_done
ra_tmo:
    stc
ra_done:
    pop es
    pop dx
    pop cx
    pop bx
    ret

/* crc_byte: AL folded into [crc] (CRC-16/CCITT-FALSE). */
crc_byte:
    push cx
    push bx
    mov bx, [crc]
    xor bh, al
    mov cx, 8
cb_l:
    test bx, 0x8000
    jz  cb_nx
    shl bx, 1
    xor bx, 0x1021
    jmp cb_next
cb_nx:
    shl bx, 1
cb_next:
    loop cb_l
    mov [crc], bx
    pop bx
    pop cx
    ret

/* put: AL -> transmit + fold into crc */
put:
    push ax
    call send
    pop ax
    call crc_byte
    ret

/* put_word: AX (LE) -> two put's */
put_word:
    push ax
    call put                    /* low */
    pop ax
    mov al, ah
    call put                    /* high */
    ret

/* send_word_raw: AX (LE) -> two send's (no crc), used for the CRC field */
send_word_raw:
    push ax
    call send
    pop ax
    mov al, ah
    call send
    ret

/* send_info: retry until ACK (lets you start RX after TX). */

/* handshake: send INFO, receive worklist (into ranges[]); retry until ok. */
handshake:
hs_retry:
    call tx_info
    call recv_command
    jc  hs_retry
    ret

/* tx_info: send one INFO frame, no ack wait. */
tx_info:
    call flush_input
    mov word ptr [crc], 0xFFFF
    mov al, SOH
    call send
    mov al, 'I'
    call put
    mov ax, [total_lo]
    call put_word
    mov ax, [total_hi]
    call put_word
    mov ax, [cyls]
    call put_word
    mov ax, [heads]
    call put_word
    mov ax, [spt]
    call put_word
    mov ax, BPS
    call put_word
    mov ax, [crc]
    call send_word_raw
    ret

/* recv_command: receive a CMD worklist frame into ranges[]/[nranges].
 * CF=0 ok, CF=1 fail. (For TX3 the worklist is received but not used.) */
recv_command:
    mov cx, HUNT_MAX
rc_hunt:
    call recv_ack
    jc  rc_fail
    cmp al, SOH
    je  rc_soh
    loop rc_hunt
    jmp rc_fail
rc_soh:
    call recv_ack
    jc  rc_fail
    cmp al, 'C'
    jne rc_fail
    mov word ptr [crc], 0xFFFF
    mov al, 'C'
    call crc_byte
    call recv_ack
    jc  rc_fail
    mov bl, al
    call crc_byte
    call recv_ack
    jc  rc_fail
    mov bh, al
    call crc_byte
    cmp bx, MAXR
    ja  rc_fail
    mov [nranges], bx
    mov cx, bx
    shl cx, 1
    shl cx, 1
    shl cx, 1
    mov di, offset ranges
rc_rd:
    jcxz rc_rdend
    call recv_ack
    jc  rc_fail
    mov [di], al
    inc di
    call crc_byte
    dec cx
    jmp rc_rd
rc_rdend:
    call recv_ack
    jc  rc_fail
    mov dl, al
    call recv_ack
    jc  rc_fail
    mov dh, al
    mov ax, [crc]
    cmp ax, dx
    jne rc_fail
    clc
    ret
rc_fail:
    stc
    ret

send_info:
si_try:
    call flush_input
    mov word ptr [crc], 0xFFFF
    mov al, SOH
    call send
    mov al, 'I'
    call put
    mov ax, [total_lo]
    call put_word
    mov ax, [total_hi]
    call put_word
    mov ax, [cyls]
    call put_word
    mov ax, [heads]
    call put_word
    mov ax, [spt]
    call put_word
    mov ax, BPS
    call put_word
    mov ax, [crc]
    call send_word_raw
    call recv_ack
    jc  si_try
    cmp al, ACK
    jne si_try
    ret

/* send_data: send DATA frame for current lba/status/buf, resend on NAK/timeout.
 * Gives up after LINK_TRIES and aborts the program. */
send_data:
    mov cx, LINK_TRIES
sd_try:
    push cx
    mov word ptr [crc], 0xFFFF
    mov al, SOH
    call send
    mov al, 'D'
    call put
    mov ax, [lba_lo]
    call put_word
    mov ax, [lba_hi]
    call put_word
    mov al, [status]
    call put
    mov si, offset buf
    mov cx, BPS
sd_data:
    lodsb
    call put
    loop sd_data
    mov ax, [crc]
    call send_word_raw
    call recv_ack
    pop cx
    jc  sd_retry
    cmp al, ACK
    je  sd_ok
sd_retry:
    loop sd_try
    /* link dead */
    mov dx, offset msg_lost
    mov ah, 0x09
    int 0x21
    mov ax, 0x4C02
    int 0x21
sd_ok:
    ret

/* send_eot: best-effort, a few tries. */
send_eot:
    mov cx, LINK_TRIES
se_try:
    push cx
    mov word ptr [crc], 0xFFFF
    mov al, SOH
    call send
    mov al, 'E'
    call put
    mov ax, [good_lo]
    call put_word
    mov ax, [good_hi]
    call put_word
    mov ax, [bad_lo]
    call put_word
    mov ax, [bad_hi]
    call put_word
    mov ax, [crc]
    call send_word_raw
    call recv_ack
    pop cx
    jc  se_retry
    cmp al, ACK
    je  se_ok
se_retry:
    loop se_try
se_ok:
    ret

/* get_geom: INT 13h AH=08h for drive 0x80. Fills cyls/heads/spt/total.
 * CF=1 on failure. */
get_geom:
    push es
    push di
    xor ax, ax
    mov es, ax
    xor di, di                  /* some BIOSes want ES:DI=0 */
    mov ah, 0x08
    mov dl, 0x80
    int 0x13
    jc  gg_err
    /* spt = CL & 0x3F */
    mov al, cl
    and al, 0x3F
    xor ah, ah
    mov [spt], ax
    /* maxcyl = CH | ((CL & 0xC0) << 2) ; cyls = maxcyl + 1 */
    mov al, ch
    xor ah, ah
    mov bl, cl
    and bl, 0xC0
    xor bh, bh
    shl bx, 1
    shl bx, 1
    or  ax, bx
    inc ax
    mov [cyls], ax
    /* heads = DH + 1 */
    mov al, dh
    xor ah, ah
    inc ax
    mov [heads], ax
    /* total = cyls*heads*spt (32-bit) */
    mov ax, [cyls]
    mul word ptr [heads]        /* dx:ax = cyls*heads */
    mov bx, ax                  /* low  */
    mov si, dx                  /* high */
    mov ax, bx
    mul word ptr [spt]          /* dx:ax = low*spt */
    mov [total_lo], ax
    mov di, dx
    mov ax, si
    mul word ptr [spt]          /* ax = high*spt (low 16) */
    add ax, di
    mov [total_hi], ax
    pop di
    pop es
    clc
    ret
gg_err:
    pop di
    pop es
    stc
    ret

/* lba_to_chs: from lba_lo/hi + spt/heads -> cur_c/cur_h/cur_s */
lba_to_chs:
    mov ax, [lba_lo]
    mov [num_lo], ax
    mov ax, [lba_hi]
    mov [num_hi], ax
    mov cx, [spt]
    call ldiv                   /* num = lba/spt ; ax = lba%spt */
    inc ax
    mov [cur_s], al             /* sector = rem + 1 */
    mov cx, [heads]
    call ldiv                   /* num = temp/heads = cyl ; ax = head */
    mov [cur_h], al
    mov ax, [num_lo]
    mov [cur_c], ax
    ret

/* ldiv: [num_hi:num_lo] / cx -> quotient back in num, remainder in AX (16). */
ldiv:
    mov ax, [num_hi]
    xor dx, dx
    div cx
    mov [num_hi], ax
    mov ax, [num_lo]
    div cx                      /* dx (rem from high) : ax -> quotient/rem */
    mov [num_lo], ax
    mov ax, dx
    ret

/* read_sector: read cur_c/h/s into buf with retries; set [status] 0/1;
 * on failure fill buf with 0 (stub). */
read_sector:
    mov byte ptr [trycnt], READ_TRIES
rs_try:
    mov ax, cs
    mov es, ax
    mov bx, offset buf
    mov dx, [cur_c]
    mov ch, dl                  /* cyl low */
    mov cl, [cur_s]             /* sector */
    mov ax, [cur_c]
    shr ax, 1
    shr ax, 1
    and al, 0xC0
    or  cl, al                  /* cyl high bits into CL 7-6 */
    mov dh, [cur_h]
    mov dl, 0x80
    mov ax, 0x0201              /* AH=02 read, AL=01 one sector */
    int 0x13
    jnc rs_ok
    /* error: reset then retry */
    xor ah, ah
    mov dl, 0x80
    int 0x13
    dec byte ptr [trycnt]
    jnz rs_try
    /* give up: stub */
    mov byte ptr [status], 1
    mov ax, cs
    mov es, ax
    mov di, offset buf
    mov cx, BPS
    xor al, al
    rep stosb
    ret
rs_ok:
    mov byte ptr [status], 0
    ret

/* ---------------- data ---------------- */
    .align 2
bases:      .word 0x3F8, 0x2F8, 0x3E8, 0x2E8
divtab:     .word 12, 6, 3, 2, 1, 24, 48

portbase:   .word 0
divisor:    .word 0
cyls:       .word 0
heads:      .word 0
spt:        .word 0
total_lo:   .word 0
total_hi:   .word 0
lba_lo:     .word 0
lba_hi:     .word 0
num_lo:     .word 0
num_hi:     .word 0
good_lo:    .word 0
good_hi:    .word 0
bad_lo:     .word 0
bad_hi:     .word 0
crc:        .word 0
cur_c:      .word 0
cur_h:      .byte 0
cur_s:      .byte 0
status:     .byte 0
trycnt:     .byte 0

msg_wait:   .ascii "disk-xfer-rescue TX. Waiting for receiver...\r\n$"
msg_link:   .ascii "Linked. Imaging (. = 256 sectors, B = bad)\r\n$"
msg_abort:  .ascii "\r\nAborted by user.\r\n$"
msg_done:   .ascii "\r\nDone.\r\n$"
msg_lost:   .ascii "\r\nLink lost. Aborting.\r\n$"
msg_nogeo:  .ascii "INT13h geometry failed (no HD0?).\r\n$"

nranges:    .word 0
ri:         .word 0
end_lo:     .word 0
end_hi:     .word 0
    .align 2
ranges:     .space MAXR*8
buf:        .space BPS
