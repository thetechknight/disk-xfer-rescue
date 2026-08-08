@echo off
rem Build TX.EXE with OpenWatcom (one-liner alternative to wmake).
rem Run after setting up the Watcom environment (owsetenv).
wcl -0 -ml -bcl=dos -fe=tx.exe src\main.c src\int13.c src\serial.c src\crc16.c
if errorlevel 1 goto err
echo.
echo Built tx.exe
goto end
:err
echo.
echo Build FAILED.
:end
