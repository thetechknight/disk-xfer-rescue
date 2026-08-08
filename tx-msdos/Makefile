# OpenWatcom wmake - build TX.EXE (real-mode DOS, 8086, large model)
#
#   wmake            build tx.exe
#   wmake clean      remove objects and exe
#
# Requires OpenWatcom (set up by owsetenv.bat / . owsetenv.sh).

CC     = wcc
CFLAGS = -0 -ml -oxt -zq
LINK   = wcl
LFLAGS = -0 -ml -bcl=dos

OBJS = main.obj int13.obj serial.obj crc16.obj

tx.exe : $(OBJS)
	$(LINK) $(LFLAGS) -fe=tx.exe $(OBJS)

main.obj  : src\main.c  src\int13.h src\serial.h src\crc16.h src\proto.h
	$(CC) $(CFLAGS) src\main.c
int13.obj : src\int13.c src\int13.h
	$(CC) $(CFLAGS) src\int13.c
serial.obj: src\serial.c src\serial.h
	$(CC) $(CFLAGS) src\serial.c
crc16.obj : src\crc16.c src\crc16.h
	$(CC) $(CFLAGS) src\crc16.c

clean : .SYMBOLIC
	@if exist *.obj del *.obj
	@if exist tx.exe del tx.exe
