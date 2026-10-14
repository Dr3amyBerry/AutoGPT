@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-agent.ps1" -StartOnly
exit /b %errorlevel%
