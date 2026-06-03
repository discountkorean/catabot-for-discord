@echo off
echo Restarting bot...
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action restart
echo Done.
pause
