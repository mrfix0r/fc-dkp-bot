@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    set "DKP_PY=py -3"
) else (
    set "DKP_PY=python"
)
%DKP_PY% -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,15) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Install Python 3.11-3.14 from python.org first. Recommended: Python 3.12.
    echo Enable Add Python to PATH, then run install.bat again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    %DKP_PY% -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto failed
".venv\Scripts\python.exe" check.py
if errorlevel 1 goto failed
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1" -Action Check
if errorlevel 1 goto failed
if exist ".env" (
    echo Installation complete. Existing settings preserved.
    echo Run start.bat, or run autostart_on.bat normally, without administrator privileges for background mode.
) else (
    echo Installation complete. Run configure.bat, then start.bat.
)
pause
exit /b 0
:failed
echo Installation or self-check failed. Read the output above.
pause
exit /b 1
