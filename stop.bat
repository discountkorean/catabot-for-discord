@echo off
echo Stopping bot...
echo. > "%~dp0data\bot.stop"
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action stop
echo Done.
pause
