@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
if errorlevel 1 (
  echo Shortcut could not be created. Check the message above.
  pause
  exit /b 1
)
pause
