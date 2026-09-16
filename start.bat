@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
title FC DKP - Discord Bot
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
if not exist ".env" (
    echo Run configure.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -u bot.py
set "DKP_EXIT=%errorlevel%"
echo Bot stopped. Keep this window open while the bot is running.
pause
exit /b %DKP_EXIT%
