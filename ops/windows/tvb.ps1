$env:TVBRIDGE_HOME = "C:\tvbridge-home"
$env:TVBRIDGE_WIN_GEOMETRY = "0,0,1024,728"
Set-Location C:\tvbridge
& "C:\Program Files\Python311\python.exe" -m tvbridge @args
exit $LASTEXITCODE
