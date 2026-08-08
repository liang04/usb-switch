@echo off
chcp 65001 >nul
REM USB-Switch BLE control (no eject).
REM Usage: usb_switch.bat a ^| b ^| x ^| s
python "%~dp0usb_switch.py" %*
