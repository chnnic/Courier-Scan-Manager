@echo off
cd /d %~dp0
python -m pip install --upgrade pip
python -m pip install pyinstaller==6.21.0
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
pyinstaller --noconsole --onefile --runtime-tmpdir .courier_runtime --name CourierScanManager app.py
echo.
echo Build complete. EXE path:
echo %cd%\dist\CourierScanManager.exe
echo.
echo After first run, the app will create:
echo - courier_config.db
echo - monthly databases like courier_2026_07.db
echo in the same folder as CourierScanManager.exe
pause
