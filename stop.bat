@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action stop
pause
