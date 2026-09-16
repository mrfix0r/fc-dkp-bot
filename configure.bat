@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" configure.py
set "DKP_EXIT=%errorlevel%"
pause
exit /b %DKP_EXIT%
