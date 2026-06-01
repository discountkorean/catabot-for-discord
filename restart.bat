@echo off
echo Syncing data before restart...
cd /d "%~dp0data"
git add . >nul 2>&1
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    git commit -m "restart sync" >nul 2>&1
    git push >nul 2>&1
    echo Data synced.
) else (
    echo No data changes to sync.
)
cd /d "%~dp0"
echo Restarting bot...
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action restart
echo Done.
pause
