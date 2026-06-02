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

echo Installing dependencies...
powershell -ExecutionPolicy Bypass -Command "& { $p = 'C:\Users\Adam\AppData\Local\Python\pythoncore-3.14-64\python.exe'; if (-not (Test-Path $p)) { $p = (where.exe python 2>$null | Where-Object { $_ -notlike '*WindowsApps*' } | Select-Object -First 1) }; & $p -m pip install -q -r '%~dp0requirements.txt' }"

echo Adding firewall exception for Python (requires admin)...
powershell -ExecutionPolicy Bypass -Command "& { $rules = Get-NetFirewallRule -DisplayName 'Catabot Python' -ErrorAction SilentlyContinue; if (-not $rules) { $py = (where.exe pythonw 2>$null | Where-Object { $_ -notlike '*WindowsApps*' } | Select-Object -First 1); if ($py) { New-NetFirewallRule -DisplayName 'Catabot Python' -Direction Outbound -Program $py -Action Allow -Profile Any | Out-Null; Write-Host 'Firewall rule added.' } } else { Write-Host 'Firewall rule already exists.' } }"

echo Starting watchdog (auto-restarts bot on crash)...
start "Catabot Watchdog" powershell -ExecutionPolicy Bypass -NoExit -File "%~dp0scripts\watchdog.ps1"
echo Watchdog launched. Close the watchdog window or run stop.bat to shut down.
pause
