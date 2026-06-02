@echo off
echo Syncing data before shutdown...
cd /d "%~dp0data"
git add . >nul 2>&1
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    git commit -m "shutdown sync" >nul 2>&1
    git push >nul 2>&1
    echo Data synced.
) else (
    echo No data changes to sync.
)
cd /d "%~dp0"
echo Stopping bot...
echo. > "%~dp0data\bot.stop"
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action stop
echo Done.
pause
