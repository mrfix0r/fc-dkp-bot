@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
title FC DKP - Windows Autostart
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1" -Action Status
set "DKP_EXIT=%errorlevel%"
pause
exit /b %DKP_EXIT%
