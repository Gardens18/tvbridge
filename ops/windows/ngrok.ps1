# Public TradingView webhook address -> the bridge on this server. Restarts ngrok if it exits.
while ($true) {
    & C:\ngrok\ngrok.exe http 127.0.0.1:8789 --url https://rewrap-punk-landside.ngrok-free.dev --config C:\ngrok\ngrok.yml --log stdout *>> C:\ngrok\ngrok.out
    Start-Sleep 10
}
