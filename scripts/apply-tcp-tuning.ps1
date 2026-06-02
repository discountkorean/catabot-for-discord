#Requires -RunAsAdministrator
<#
    apply-tcp-tuning.ps1  —  Run ONCE (as Administrator) on the bot server.

    Fixes WinError 10055 (socket buffer exhaustion) caused by TCP connections
    piling up in TIME_WAIT. Two registry changes:

      TcpTimedWaitDelay   — how long (seconds) a closed connection holds its
                            port in TIME_WAIT before the port is recycled.
                            Windows default: 240s.  We set it to 30s.

      MaxUserPort         — upper bound of the ephemeral port range.
                            Windows default: ~16 384 ports (49152–65535).
                            We expand it to the OS maximum: 65534.

    A reboot is required for both settings to take effect.
#>

$tcpKey = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"

Write-Host "Applying Windows TCP tuning..." -ForegroundColor Cyan

Set-ItemProperty -Path $tcpKey -Name "TcpTimedWaitDelay" -Value 30   -Type DWord
Write-Host "  TcpTimedWaitDelay  = 30s  (was 240s)"

Set-ItemProperty -Path $tcpKey -Name "MaxUserPort"       -Value 65534 -Type DWord
Write-Host "  MaxUserPort        = 65534 (was ~16384)"

Write-Host ""
Write-Host "Done. Reboot the server for changes to take effect." -ForegroundColor Green
