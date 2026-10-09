$log = "C:\tvbridge-setup\watchdog.log"
foreach ($n in @("tvb-ngrok","tvb-engine","tvb-runner","tvb-keep-console")) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($t -and $t.State -ne "Running" -and $t.State -ne "Disabled") {
        Start-ScheduledTask -TaskName $n
        "$(Get-Date -Format s) started $n (was $($t.State))" | Add-Content $log
    }
}
if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName tvb-mt5
    "$(Get-Date -Format s) MT5 not running: started tvb-mt5" | Add-Content $log
}
