# Operating the Hantec pilot server from a cloud session

Written 2026-10-06. No secrets in this file: the SSH private key and AWS keys live in the
cloud environment's secrets (see "Secrets" below).

## The server
- AWS account 010724907020 (riazmohamed2026@gmail.com), region eu-north-1 (Stockholm).
- Instance `hantec-pilot` = i-0303e091d02e9ed48, t3.small, Windows Server 2022, fixed address
  16.192.38.115 (Elastic IP eipalloc-0fefa4db43555c562). Security group sg-081ec461b02b7ce62.
- SSH: `ssh -i <key> Administrator@16.192.38.115` (key-only; the shell is PowerShell).
  SSH runs in session 0 and cannot see the desktop. Anything that needs the screen is run
  by dropping a PowerShell file at `C:\tvbridge-setup\jobs\<name>.job.ps1`; the `tvb-runner`
  task executes it inside the logged-in desktop and writes `<name>.log`, then renames the
  job to `<name>.done.ps1`.
- Layout on the server: bridge source `C:\tvbridge`; data `C:\tvbridge-home` (config.json,
  calibration.json, tvbridge.db, engine.out, heartbeat.json); helper scripts in
  `C:\tvbridge-setup` (tvb.ps1 <command>, status.ps1, go-live.ps1, go-rehearsal.ps1,
  setmode.py, runner.ps1, engine.ps1, keepconsole.ps1, mt5-hantec.ps1); ngrok in C:\ngrok.
- Scheduled tasks: tvb-engine, tvb-runner, tvb-mt5 (all at logon, interactive), tvb-ngrok
  (at start, SYSTEM), tvb-keep-console (re-attaches a disconnected RDP session to the
  console), tvb-mt5-5ers (DISABLED until The5ers is added). Autologon is enabled, so the
  server recovers from a reboot on its own.
- MT5 main window is held at 0,0 1024x728 (env TVBRIDGE_WIN_GEOMETRY) because the console
  is 1024x768. Do not maximise it.

## Common commands (over SSH)
- Status: `C:\tvbridge-setup\status.ps1`
- Pause / resume entries: `C:\tvbridge-setup\tvb.ps1 pause` / `... resume`
- Flatten everything: `C:\tvbridge-setup\tvb.ps1 flatten` (asks for confirmation)
- Switch mode (restarts the engine): `C:\tvbridge-setup\go-live.ps1` / `go-rehearsal.ps1`
- Deploy code: copy the `tvbridge/` package to `C:\tvbridge\tvbridge`, then stop the engine's
  python process (pid in heartbeat.json); engine.ps1 restarts it within 10 s.
- Database: `C:\tvbridge-home\tvbridge.db` (sqlite: signals, positions, events).

## Alert routing
- TradingView alerts -> https://rewrap-punk-landside.ngrok-free.dev/webhook -> ngrok on the
  SERVER -> bridge on 127.0.0.1:8789. Never run ngrok with this domain anywhere else.
- The server forwards every alert to the Mac's FundingPips (127.0.0.1:18787) and Audacity
  (127.0.0.1:18788) engines through a reverse SSH tunnel the Mac keeps open (launchd agent
  com.tvbridge.aws-tunnel). When the Mac is off those forwards fail harmlessly.

## Accounts
- Hantec Endurance 50k (8089020, HantecMarketsMU-MT5): LIVE on the server, three assets:
  XAGUSD.h, XAUUSD.h and XAUEUR.h (gold-euro is a fan-out copy of the gold alert, converted
  with mirror.quote_usd_by_symbol.XAUEUR = 1.1215 and a 2.5 % price-gap allowance; update the
  rate if EURUSD drifts and entries start failing with PRICE_GAP).
- FundingPips and Audacity run on the user's Mac; not a priority while travelling.
- The5ers: deferred (needs the 4 GB size = AWS paid plan; account has little loss room).

## Secrets the cloud environment needs
- `HANTEC_SSH_KEY`: contents of the Mac's ~/.ssh/hantec-pilot.pem (RSA private key).
  Write it to a file with mode 600 before use.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (+ `AWS_DEFAULT_REGION=eu-north-1`): an IAM
  user limited to EC2 (security groups, instance start/stop/describe). Needed only to change
  the firewall or restart the instance.
- Network: the environment must be allowed to open SSH (port 22) to 16.192.38.115 and HTTPS
  to the ngrok domain. The server's SSH rule must admit the environment's address range
  (0.0.0.0/0 with key-only auth is the practical setting).

## Known open items
- Position-row reading on Windows is fixed but was not yet proven on a live position when
  this was written. If the engine reports TOOLBOX_INCOMPLETE / POSITIONS_UNKNOWN, fetch
  `C:\tvbridge-home\shots\<date>\account_latest.png` and look at the Trade tab.
- A stray "Order: ..." window on the server is closed with a WM_CLOSE request
  (WinDriver.close_window); `_dismiss_stray_dialogs` does this automatically before entries.
