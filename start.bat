@echo off
cd /d "%~dp0"
echo Installing dependencies...
powershell -ExecutionPolicy Bypass -Command "& { $p = 'C:\Users\Adam\AppData\Local\Python\pythoncore-3.14-64\python.exe'; if (-not (Test-Path $p)) { $p = (where.exe python 2>$null | Where-Object { $_ -notlike '*WindowsApps*' } | Select-Object -First 1) }; & $p -m pip install -q -r '%~dp0requirements.txt' }"
echo Starting bot...
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bot.ps1" -Action start
echo Done.
pause
