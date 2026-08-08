@echo off
chcp 65001 >nul
REM Start the USB-Switch local bridge service (keep this window open).
python "%~dp0bridge.py"
pause
