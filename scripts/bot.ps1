param([string]$Action = "start")

$dir     = Split-Path -Parent $PSScriptRoot
$botFile = Join-Path $dir "bot.py"
$pidFile = Join-Path $dir "data\bot.pid"

function Stop-Bot {
    if (Test-Path $pidFile) {
        $botPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($botPid) {
            Stop-Process -Id $botPid -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}

function Start-Bot {
    $preferred = "C:\Users\Adam\AppData\Local\Python\pythoncore-3.14-64\python.exe"
    if (Test-Path $preferred) {
        $pythonw = $preferred
    } else {
        $pythonw = where.exe python 2>$null | Where-Object { $_ -notlike "*WindowsApps*" } | Select-Object -First 1
        if (-not $pythonw) { throw "Could not find Python executable." }
    }
    $p = Start-Process $pythonw -ArgumentList "`"$botFile`"" -WorkingDirectory $dir -WindowStyle Normal -PassThru
    $p.Id | Out-File -Encoding ascii $pidFile
}

switch ($Action) {
    "start" {
        Write-Host "Starting bot..."
        Start-Bot
        Write-Host "Bot started."
    }
    "stop" {
        Write-Host "Stopping bot..."
        Stop-Bot
        Write-Host "Bot stopped."
    }
    "restart" {
        Write-Host "Stopping bot..."
        Stop-Bot
        Start-Sleep -Seconds 2
        Write-Host "Starting bot..."
        Start-Bot
        Write-Host "Bot restarted."
    }
}
