param()

$dir      = Split-Path -Parent $PSScriptRoot
$botFile  = Join-Path $dir "bot.py"
$pidFile  = Join-Path $dir "data\bot.pid"
$stopFile = Join-Path $dir "data\bot.stop"

# Resolve Python executable
$preferred = "C:\Users\Adam\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if (Test-Path $preferred) {
    $pythonw = $preferred
} else {
    $pythonw = where.exe pythonw 2>$null |
               Where-Object { $_ -notlike "*WindowsApps*" } |
               Select-Object -First 1
    if (-not $pythonw) {
        $pythonw = where.exe python 2>$null |
                   Where-Object { $_ -notlike "*WindowsApps*" } |
                   Select-Object -First 1
    }
    if (-not $pythonw) { throw "Could not find Python executable." }
}

# Clear any stale stop file from a previous run
Remove-Item $stopFile -ErrorAction SilentlyContinue

Write-Host "=== Catabot Watchdog ===" -ForegroundColor Cyan
Write-Host "Python : $pythonw"
Write-Host "Bot    : $botFile"
Write-Host "Close this window or run stop.bat to shut down."
Write-Host ""

$restartDelay = 10

while ($true) {
    if (Test-Path $stopFile) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stop requested — watchdog exiting." -ForegroundColor Yellow
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        break
    }

    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting bot..." -ForegroundColor Green
    $p = Start-Process $pythonw `
        -ArgumentList "`"$botFile`"" `
        -WorkingDirectory $dir `
        -PassThru
    $p.Id | Out-File -Encoding ascii $pidFile
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Bot running (PID $($p.Id))"

    $p.WaitForExit()
    $code = $p.ExitCode

    if (Test-Path $stopFile) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stop requested — not restarting." -ForegroundColor Yellow
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        break
    }

    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Bot exited (code $code). Restarting in ${restartDelay}s..." -ForegroundColor Red
    Start-Sleep -Seconds $restartDelay
}

Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Watchdog stopped." -ForegroundColor Cyan
