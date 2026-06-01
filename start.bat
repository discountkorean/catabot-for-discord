@echo off
echo Pulling latest data...
cd /d "%~dp0data"
git pull --ff-only >nul 2>&1
if errorlevel 1 (
    echo Warning: data pull failed, starting with local data.
) else (
    echo Data up to date.
)
cd /d "%~dp0"
echo Starting bot...
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action start
echo Done.
pause
