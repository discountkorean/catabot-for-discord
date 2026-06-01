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
    $pythonw = "C:\Users\adam.kim\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
    $p = Start-Process $pythonw -ArgumentList "`"$botFile`"" -WorkingDirectory $dir -PassThru
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
