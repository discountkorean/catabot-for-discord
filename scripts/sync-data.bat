@echo off
echo Syncing bot data to private repo...
cd /d "%~dp0..\data"
git add .
git diff --cached --quiet && (echo No changes to sync. & goto :bot) || git commit -m "data sync %date% %time%"
git push
:bot
echo.
echo Syncing bot code to main repo...
cd /d "%~dp0.."
git add .
git diff --cached --quiet && (echo No changes to sync.) || git commit -m "bot sync %date% %time%"
git push
echo.
echo Done.
pause
