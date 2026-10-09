# tvbridge on a Windows PC (new account, e.g. The5ers)

This is the recipe that runs the Hantec account on the AWS Windows server since 2026-10-05.
A Claude Code session on the Windows PC follows it top to bottom. Everything in
`ops/windows/` is a copy of the server's working scripts; adjust paths/names as noted.

Facts that matter (learned the hard way, see git log):
- MT5 is driven through its GUI only (no EA, no MT5 API): the engine types into the New
  Order window, verifies it, clicks once, and reads the Toolbox. The PC must stay logged in
  with the screen unlocked and never sleep; MT5 must be a normal window at 0,0 1024x728.
- MT5 hides unused Market Watch symbols after a while and silently keeps the previous symbol
  in the order window; the executor repairs that (SYMBOL_NOT_SELECTED, Symbols window), but
  give each traded symbol an open chart anyway.
- Task Scheduler kills tasks after 3 days unless ExecutionTimeLimit is PT0S (`fixtasks.ps1`).
- The alert hub is the AWS server (`https://rewrap-punk-landside.ngrok-free.dev/webhook`);
  it forwards every alert to the other engines over loopback ports (`server.forward_to`).
  A new engine joins by exposing a loopback port on the server through a reverse SSH tunnel.

## 0. What you need from the user before starting
- MT5 login, password (typed by the user, never by Claude), server name, account size,
  the firm's daily-loss and max-loss limits in percent, and whether a daily reference is
  "balance/equity at 00:00 server time" (default) or the initial balance.
- The Telegram bot token + chat id (copy from an existing config.json; never commit them).
- The webhook secret of the TradingView alerts (same as in the other configs).
- The SSH key `hantec-pilot.pem` for the AWS server (reverse tunnel), or agree on a
  separate ngrok domain and a second TradingView alert pointing at this PC instead.

## 1. Install
1. Python 3.11 (python.org installer, "Add to PATH", all users): the scripts expect
   `C:\Program Files\Python311\python.exe`. Then `python -m pip install pillow winrt-runtime
   winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Globalization
   winrt-Windows.Graphics.Imaging winrt-Windows.Media.Ocr winrt-Windows.Storage
   winrt-Windows.Storage.Streams`.
2. Tesseract OCR (UB Mannheim build) to `C:\Program Files\Tesseract-OCR\tesseract.exe`.
3. Git (or download the repo zip). Clone `https://github.com/Gardens18/tvbridge` to
   `C:\tvbridge`. Run the tests once: `cd C:\tvbridge; python -m unittest discover -s tests -t .`
4. MT5 from the prop firm's download page, installed to `C:\Program Files\MetaTrader 5`
   (a second MT5 on the same PC goes to its own folder and is started with `/portable`).
   Log in once with "Save password". View > Symbols: show every symbol to trade. Open one
   chart per traded symbol. Toolbox on the Trade tab. Turn off "One Click Trading".
5. Windows: Settings > Power: screen never off, never sleep. Autologon only if the PC may
   reboot unattended (Sysinternals Autologon; the user types the password).

## 2. Files and folders
- `C:\tvbridge` source (never edit here except by git pull / scp).
- `C:\tvbridge-home` data: `config.json`, `calibration.json`, `tvbridge.db`, `engine.out`, `shots\`.
- `C:\tvbridge-setup` scripts: copy `ops\windows\*.ps1` and `*.py` there, plus `jobs\`.
  Edit `mt5-hantec.ps1` -> the MT5 exe path of this account (rename to mt5-<firm>.ps1).

## 3. config.json
Start from `config.example.json`. Set:
- `account`: name, `initial_balance`, `account_login`, `server_name` (both must appear in
  the MT5 window title), `server_utc_offset_hours` (read the Market Watch clock).
- `server`: `port` 8791 (or another free loopback port), `secret`, `forward_to` empty.
- `risk`: the firm's `daily_loss_pct` / `max_loss_pct`, buffers as on the other accounts,
  `max_risk_per_trade_pct` ~1.0, `max_total_open_risk_pct` ~3.0, `max_open_positions` 3,
  `max_lots` LOW until the broker's gold margin is measured (first fill: margin / lots),
  `stale_reference_buffer_pct` 2.0, `resume_after_verified_fill` true.
- `mirror`: `enabled` true, `units_per_lot` = strategy units per lot as on Hantec
  (compare with the other configs), per-symbol stop/tp distances copied from Hantec
  (`stop_distance_by_symbol`, `tp_distance_by_symbol`), `fan_out` for a gold cross if the
  firm lists one (needs `quote_usd_by_symbol` and `max_price_gap_pct_by_symbol` 2.5),
  `reenter_after_stop` true with `reenter_cooldown_s` 660 and `reenter_max` 1 unless the
  firm forbids re-entries (check its rules: "same trade idea", "loss chasing").
- `symbols`: specs with the exact MT5 names and suffixes (contract size, digits, min lot,
  lot step, min stop points, commission).
- `executor`: `mode` "rehearsal" first; `gui.owner_names` ["terminal64"],
  `gui.order_dialog_title_contains` ["Order"], `lock_timeout_s` 90.
- `notify`: Telegram token/chat id, `min_level` "info".

## 4. Calibrate and rehearse
```
$env:TVBRIDGE_HOME="C:\tvbridge-home"; $env:TVBRIDGE_WIN_GEOMETRY="0,0,1024,728"
cd C:\tvbridge; python -m tvbridge calibrate        # MT5 visible, order window closed
python -m tvbridge rehearse --symbol XAUUSD --side buy --sl 4000 --tp 4300 --lots 0.01
python -m tvbridge status
```
Rehearse every traded symbol once (REHEARSED: verified ...). Fix the Toolbox size if the
account line / position rows are not readable (see `ops/windows/tradetab.py`, `symprobe.py`).

## 5. Tasks (run as the logged-in user, Interactive; see `fixtasks.ps1` for settings)
- `tvb-mt5`: at logon, starts MT5 (`mt5-<firm>.ps1`).
- `tvb-engine`: at logon, `engine.ps1` (loops `python -m tvbridge run`, log engine.out).
- `tvb-runner`: at logon, `runner.ps1` (runs `C:\tvbridge-setup\jobs\*.job.ps1` on the desktop).
- `tvb-watchdog`: SYSTEM, every 5 min, `watchdog.ps1` (restarts anything stopped).
- `tvb-keep-console`: SYSTEM, on RDP disconnect, `keepconsole.ps1` (only if RDP is used).
After registering: run `fixtasks.ps1` (ExecutionTimeLimit PT0S), then start them and check
`status.ps1` shows "engine: running" and "untracked: none".

## 6. Join the alert hub
On this PC, a persistent reverse tunnel to the server (scheduled task at logon, retry loop):
```
ssh -i C:\keys\hantec-pilot.pem -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes `
    -R 127.0.0.1:18791:127.0.0.1:8791 Administrator@16.192.38.115
```
Then on the server add `http://127.0.0.1:18791/webhook` to `server.forward_to` in
`C:\tvbridge-home\config.json` and restart its engine (kill the `python -m tvbridge run`
process; engine.ps1 restarts it). Test: `tvbridge status` on the PC shows the next alert.
Alternative without the server: own ngrok domain + a second TradingView alert.

## 7. Go live
`go-live.ps1` flips `executor.mode` to live (user's decision). First live fill: read the
margin MT5 shows for the position (`status` account line / snapshots) and set `risk.max_lots`
so that three positions fit in the free margin. Watch the first full round trip in the log.

## 8. Daily checks
`status.ps1`: engine running, entries allowed, untracked none, reference source. Telegram
messages arrive. After any MT5 restart: Toolbox on the Trade tab, symbols listed, charts open.
