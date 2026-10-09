$names = @("tvb-engine","tvb-runner","tvb-ngrok","tvb-keep-console","tvb-mt5")
foreach ($n in $names) {
    $t = Get-ScheduledTask -TaskName $n
    "$n before: limit=" + $t.Settings.ExecutionTimeLimit + " multi=" + $t.Settings.MultipleInstances
    $s = $t.Settings
    $s.ExecutionTimeLimit = "PT0S"          # no 3-day kill
    $s.DisallowStartIfOnBatteries = $false
    $s.StopIfGoingOnBatteries = $false
    Set-ScheduledTask -TaskName $n -Settings $s | Out-Null
    "$n after:  limit=" + (Get-ScheduledTask -TaskName $n).Settings.ExecutionTimeLimit
}
# watchdog: every 5 min start any tvb task that is not running
$wd = @'
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
'@
Set-Content -Path C:\tvbridge-setup\watchdog.ps1 -Value $wd
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\tvbridge-setup\watchdog.ps1"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Unregister-ScheduledTask -TaskName tvb-watchdog -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName tvb-watchdog -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
"watchdog registered: " + (Get-ScheduledTask tvb-watchdog).State
Start-ScheduledTask tvb-watchdog; Start-Sleep 8
"watchdog log: "; Get-Content C:\tvbridge-setup\watchdog.log -ErrorAction SilentlyContinue | Select-Object -Last 3
Get-ScheduledTask tvb-* | ForEach-Object { $_.TaskName + " " + $_.State + " limit=" + $_.Settings.ExecutionTimeLimit }
