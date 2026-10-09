# Runs as SYSTEM whenever a remote desktop session disconnects: any disconnected user session is
# attached to the server's console so its desktop keeps being drawn for the bridge.
Start-Sleep -Seconds 2
$lines = (& qwinsta.exe) 2>$null
foreach ($l in $lines) {
    if ($l -match '^\s+(\S+)?\s+Administrator\s+(\d+)\s+Disc') {
        & tscon.exe $Matches[2] /dest:console
        "$(Get-Date -Format s) attached session $($Matches[2]) to console (exit $LASTEXITCODE)" | Add-Content C:\tvbridge-setup\keepconsole.log
    }
}
