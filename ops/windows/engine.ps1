$env:TVBRIDGE_HOME = "C:\tvbridge-home"
$env:TVBRIDGE_WIN_GEOMETRY = "0,0,1024,728"
Set-Location C:\tvbridge
while ($true) {
    & "C:\Program Files\Python311\python.exe" -m tvbridge run *>> C:\tvbridge-home\engine.out
    "$(Get-Date -Format s) engine exited with $LASTEXITCODE; restarting in 10 s" | Add-Content C:\tvbridge-home\engine.out
    Start-Sleep 10
}
