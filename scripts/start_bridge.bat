@echo off
chcp 65001 >nul
REM Start the USB-Switch local bridge service (keep this window open).
"D:\Program Files\Python312\python.exe" "%~dp0bridge.py"
pause
