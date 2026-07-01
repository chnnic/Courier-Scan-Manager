@echo off
cd /d %~dp0
if not exist dist\CourierScanManager.exe (
  echo File not found: dist\CourierScanManager.exe
  echo Please run build_exe.bat first.
  pause
  exit /b 1
)
echo SHA256 for dist\CourierScanManager.exe:
certutil -hashfile dist\CourierScanManager.exe SHA256
pause
