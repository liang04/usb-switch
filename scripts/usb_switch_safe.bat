@echo off
chcp 65001 >nul
REM USB-Switch safe switch: eject USB drive first, switch only on success.
REM Usage: usb_switch_safe.bat a ^| b ^| x ^| list
"D:\Program Files\Python312\python.exe" "%~dp0usb_switch_safe.py" %*
pause
