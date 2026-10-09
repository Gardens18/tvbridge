Start-Sleep 5
if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq "C:\Program Files\MetaTrader 5\terminal64.exe" })) { Start-Process "C:\Program Files\MetaTrader 5\terminal64.exe" }
