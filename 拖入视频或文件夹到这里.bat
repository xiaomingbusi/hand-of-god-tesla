@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" "%~1"
echo.
pause