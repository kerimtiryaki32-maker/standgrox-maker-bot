@echo off
cd /d "%~dp0"
echo Starting Standgrox Maker Bot from main.py in this folder...
echo.
py "%~dp0main.py"
if errorlevel 1 (
    echo.
    echo Startup failed. Read the error above; do not share your .env file.
    pause
)
