$J = "C:\tvbridge-setup\jobs"
New-Item -ItemType Directory -Force $J | Out-Null
while ($true) {
    Get-ChildItem $J -Filter *.job.ps1 | ForEach-Object {
        $n = $_.Name -replace '\.job\.ps1$', ''
        $run = Join-Path $J "$n.running.ps1"
        Move-Item $_.FullName $run -Force
        & powershell -NoProfile -ExecutionPolicy Bypass -File $run *> (Join-Path $J "$n.log")
        "EXIT=$LASTEXITCODE" | Add-Content (Join-Path $J "$n.log")
        Move-Item $run (Join-Path $J "$n.done.ps1") -Force
    }
    Start-Sleep -Milliseconds 700
}
