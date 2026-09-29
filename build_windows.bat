@echo off
cd /d "%~dp0"
echo Building Standgrox Maker Bot for Windows...
py -m pip install -r requirements.txt
if errorlevel 1 goto :failed
py -m pip install pyinstaller
if errorlevel 1 goto :failed
py -m PyInstaller --noconfirm --clean --onefile --windowed --name "Standgrox Maker Bot" --icon "icon.ico" --add-data "icon.png;." --add-data "icon.ico;." main.py
if errorlevel 1 goto :failed
echo.
echo Done. The app is in the dist folder.
echo Keep .env beside the .exe; do not share it.
pause
exit /b 0
:failed
echo Build failed. Read the error above.
pause
exit /b 1
