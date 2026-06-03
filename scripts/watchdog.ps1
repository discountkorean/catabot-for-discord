param()

$dir      = Split-Path -Parent $PSScriptRoot
$botFile  = Join-Path $dir "bot.py"
$pidFile  = Join-Path $dir "data\bot.pid"
$stopFile = Join-Path $dir "data\bot.stop"

# ── Scheduled restart times (24h hours, local time) ──────────────────────────
$restartHours = @(4, 16)   # 04:00 and 16:00

# Resolve Python executable
$preferred = "C:\Users\Adam\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (Test-Path $preferred) {
    $pythonw = $preferred
} else {
    $pythonw = where.exe python 2>$null |
               Where-Object { $_ -notlike "*WindowsApps*" } |
               Select-Object -First 1
    if (-not $pythonw) { throw "Could not find Python executable." }
}

# Clear any stale stop file from a previous run
Remove-Item $stopFile -ErrorAction SilentlyContinue

Write-Host "=== Catabot Watchdog ===" -ForegroundColor Cyan
Write-Host "Python         : $pythonw"
Write-Host "Bot            : $botFile"
Write-Host "Scheduled flush: $($restartHours | ForEach-Object { '{0:D2}:00' -f $_ }) daily"
Write-Host "Close this window or run stop.bat to shut down."
Write-Host ""

$crashDelay       = 10   # seconds to wait after an unexpected crash
$socketBackoff    = 60   # extra wait when bot dies in <30s (likely socket exhaustion)
$lastRestartHour  = -1   # tracks which hour we last did a scheduled restart

while ($true) {
    if (Test-Path $stopFile) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stop requested — watchdog exiting." -ForegroundColor Yellow
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        break
    }

    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting bot..." -ForegroundColor Green
    $startTime = Get-Date
    $p = Start-Process $pythonw `
        -ArgumentList "`"$botFile`"" `
        -WorkingDirectory $dir `
        -WindowStyle Normal `
        -PassThru
    $p.Id | Out-File -Encoding ascii $pidFile
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Bot running (PID $($p.Id))"

    # Monitor loop — checks every 30s for a scheduled restart or stop request
    $scheduledRestart = $false
    while (-not $p.HasExited) {
        Start-Sleep -Seconds 30

        if (Test-Path $stopFile) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stop requested — shutting down bot." -ForegroundColor Yellow
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            break
        }

        $now = Get-Date
        if (($now.Hour -in $restartHours) -and
            ($now.Hour -ne $lastRestartHour) -and
            ($now.Minute -lt 5)) {
            $lastRestartHour  = $now.Hour
            $scheduledRestart = $true
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Scheduled restart at $($now.ToString('HH:mm')) — flushing bot..." -ForegroundColor Cyan
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            break
        }
    }

    $p.WaitForExit()
    $code = $p.ExitCode

    if (Test-Path $stopFile) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stop confirmed — not restarting." -ForegroundColor Yellow
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        break
    }

    if ($scheduledRestart) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Scheduled restart — back up in 3s..." -ForegroundColor Cyan
        Start-Sleep -Seconds 3
    } else {
        $uptime = ((Get-Date) - $startTime).TotalSeconds
        if ($uptime -lt 30) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Bot died after $([int]$uptime)s (possible socket exhaustion) — waiting ${socketBackoff}s for ports to drain..." -ForegroundColor Red
            Start-Sleep -Seconds $socketBackoff
        } else {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Bot exited (code $code) — restarting in ${crashDelay}s..." -ForegroundColor Red
            Start-Sleep -Seconds $crashDelay
        }
    }
}

Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Watchdog stopped." -ForegroundColor Cyan
