# tvbridge

TradingView alerts in, MetaTrader 5 orders out: a webhook listener on your Mac, a risk guard
built for a prop-firm account, and a deterministic clicker that places orders in the
MetaTrader 5 macOS app and checks each one with OCR.

Built for a **Hantec Trader "Endurance" 3-step $50,000** challenge account (4% daily loss,
8% static max loss), MT5 server `HantecMarketsMU-MT5`, symbols with the `.h` suffix, running
on an always-on Mac Studio.

> **Read [Limitations and failure modes](#11-limitations-and-failure-modes) before you go
> live.** GUI automation is more fragile than a broker API. tvbridge is built to *fail
> closed*: when it is unsure, it does not open a trade. It will sometimes skip trades your
> strategy wanted. That is deliberate.

## Contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [No AI in the execution path](#3-no-ai-in-the-execution-path)
4. [Modes: paper, rehearsal, live](#4-modes-paper-rehearsal-live)
5. [Quick start on the Mac Studio](#5-quick-start-on-the-mac-studio)
6. [macOS settings you must change yourself](#6-macos-settings-you-must-change-yourself)
7. [Risk model ($50,000 example)](#7-risk-model-50000-example)
8. [Hantec rules to respect](#8-hantec-rules-to-respect)
9. [Configuration reference](#9-configuration-reference)
10. [CLI reference](#10-cli-reference)
11. [Limitations and failure modes](#11-limitations-and-failure-modes)
12. [Operations](#12-operations)
13. [Troubleshooting by reason code](#13-troubleshooting-by-reason-code)
14. [Mirror mode](#14-mirror-mode)

---

## 1. What it does

1. **Receives** TradingView webhook alerts (JSON) through an ngrok HTTPS tunnel on a
   listener bound to `127.0.0.1:8787`.
2. **Authenticates and validates** each alert: shared secret, TradingView source-IP
   allowlist, rate limit, schema, symbol allowlist, and freshness (entries older than
   120 s are refused; a late *exit* is still executed up to 15 minutes when nothing newer
   was opened on that symbol, and every refused exit sends you a critical alert).
3. **Queues** every accepted alert in a local SQLite database *before* answering
   TradingView, so a crash or restart never loses or double-executes an alert
   (duplicates are recognised by id).
4. **Checks risk** for every entry: daily and max-loss floors with safety buffers, position
   size from the stop distance, total open risk, position and trade-count limits, trading
   hours, and a required stop-loss. Closes are always allowed.
5. **Executes** in MetaTrader 5 by driving its window the way a careful human would: it
   opens the New Order ticket (F9), types symbol, volume, SL and TP, takes a screenshot,
   reads it back with macOS Vision OCR, and clicks Buy or Sell **only if every field reads
   back exactly** and the button under the cursor carries the right label. It then reads
   the result and closes the ticket.
6. **Watches the account**: every 15 s it reads balance, equity and open positions from the
   MT5 Toolbox, checks that the position list it read is complete, reconciles it with its
   own ledger, and flattens everything (kill switch) if equity gets close to the firm's loss
   limit.
7. **Notifies** you (macOS Notification Center, ntfy, Telegram) about fills, rejections,
   halts and anything needing a human.

## 2. Architecture

```
  TradingView alert (Pine strategy, indicator or manual)
        |
        |  HTTPS POST, JSON body containing your secret
        v
  ngrok edge:  https://<your-domain>.ngrok-free.app/webhook
        |
        |  tunnel  (LaunchAgent com.tvbridge.ngrok)
        v
  listener   127.0.0.1:8787            tvbridge/server.py
        |    secret, IP allowlist, rate limit, JSON schema, freshness
        |    insert into the queue first, then answer 200 (duplicates ignored)
        v
  SQLite queue  ~/.tvbridge/tvbridge.db   signals, ledger, snapshots, events
        |
        |  one executor thread; priority: flatten > close > open > account poll
        v
  risk guard                            tvbridge/risk.py  (pure functions)
        |    floors + buffers, per-trade and total risk, limits, hours, SL required
        |    approved plan: lots, SL, TP (+ opposite positions to close first)
        v
  MT5 GUI executor                      tvbridge/executors/mt5gui.py
        |    F9 -> type fields -> screenshot -> Vision OCR -> verify every field
        |    -> check the button label -> ONE click -> OCR the result -> close ticket
        v
  MetaTrader 5 (macOS app)  <---->  Hantec server  (server-side SL/TP on every order)

  Side loops (engine scheduler, 1 s tick):
    account poll every 15 s (OCR of the Toolbox) -> reconcile ledger -> kill switch
    daily rollover at 00:00 server time -> new daily reference and floors
    heartbeat file every 10 s, screenshot cleanup, notifications
```

Two LaunchAgents keep this running in your logged-in session:
`com.tvbridge.engine` (`python -m tvbridge run`: listener + queue + risk + executor) and
`com.tvbridge.ngrok` (the tunnel). They restart automatically if they exit, and start at
login.

## 3. No AI in the execution path

Everything between the webhook and the click is plain, deterministic Python that you run on
your own Mac. No language model, "AI agent" or remote service decides, sizes, modifies or
places trades. The same alert with the same account state always produces the same
decision.

**You switch modes yourself.** tvbridge never promotes itself from paper to rehearsal to
live, never retries an entry, and after any uncertain execution it halts new entries until
*you* run `tvbridge resume`.

## 4. Modes: paper, rehearsal, live

`executor.mode` in `~/.tvbridge/config.json` selects what happens to an approved entry:

| Mode | What an approved buy/sell does | Touches MT5? |
|---|---|---|
| `paper` (default) | Simulated fill at the alert price in a local paper account. SL/TP are simulated only from prices carried by later alerts, so P/L is approximate. | No |
| `rehearsal` | The full GUI path: opens the order ticket, types symbol/volume/SL/TP, OCR-verifies everything, then **presses Escape instead of Buy/Sell**. Closes open the position dialog, find the close button, then Escape. Account polling reads the real Toolbox. Signal status: `rehearsed`. | Yes, but never clicks Buy, Sell or Close |
| `live` | Same as rehearsal, plus the single click on Buy/Sell (or Close) and verification of the result. | Yes |

**Go in this order, and do not skip steps:**

1. **paper** proves the plumbing: TradingView -> ngrok -> listener -> queue -> risk.
   You see whether your alert payloads are valid, how sizing comes out and how often your
   strategy fires. No macOS permissions and no MT5 needed.
2. **rehearsal on a DEMO account** proves the GUI path on *your* screen: window detection,
   calibration, typing, OCR verification, account reading. Rehearsal still types into the
   real order ticket, so run it against an MT5 **demo** login first (log the MT5 app into a
   Hantec demo or any MT5 demo account). Let it rehearse real alerts for at least a few
   days. Then rehearse once more on the challenge account itself (with
   `account.account_login` set) to confirm it reads that account correctly.
3. **live** only after rehearsals pass consistently. Consider starting with
   `risk.risk_per_trade_pct` at 0.25.

To switch: edit `executor.mode`, restart the engine
(`launchctl kickstart -k gui/$(id -u)/com.tvbridge.engine`) and confirm with
`tvbridge status`. The engine reads `config.json` only when it starts.

## 5. Quick start on the Mac Studio

Prerequisites: macOS user account that stays logged in; MetaTrader 5 for macOS installed and
logged in; TradingView **Essential** plan or higher (webhooks need a paid plan) with 2FA
enabled; a free ngrok account with its static domain (dashboard.ngrok.com -> Domains).

**Running commands.** tvbridge is not installed system-wide; every command is
`.venv/bin/python -m tvbridge <command>` run from the app folder. This README writes
`tvbridge <command>`. To make that work, add this to `~/.zshrc`:

```bash
tvbridge() { ( cd "$HOME/tvbridge" && .venv/bin/python -m tvbridge "$@" ); }
```

### Step 1: copy the folder

Copy the `tvbridge` folder to the Mac Studio, e.g. to `~/tvbridge` (leave out `.venv`; the
installer builds a fresh one):

```bash
rsync -a --exclude .venv ./tvbridge/ "studio.local:tvbridge/"
```

Do **not** put it in `~/Desktop`, `~/Documents` or `~/Downloads`: those folders are
privacy-protected (and may be in iCloud), and a background LaunchAgent can be denied access
to them. Spaces in the path are fine.

### Step 2: install

```bash
cd ~/tvbridge
./install.sh            # options: --no-launchd, --no-ngrok
```

This creates `.venv` (with `/usr/bin/python3`, installs pyobjc), runs `tvbridge init`
(creates `~/.tvbridge/config.json` with a random webhook secret, chmod 600), downloads ngrok
into `~/.tvbridge/bin/`, and loads the engine LaunchAgent in **paper** mode. It never uses
sudo, never touches MT5 and is safe to re-run. If Apple's Command Line Tools are missing it
tells you to run `xcode-select --install` first.

### Step 3: edit the config

Open `~/.tvbridge/config.json` (`open -e ~/.tvbridge/config.json`) and set:

- `ngrok.authtoken` (dashboard.ngrok.com -> Your Authtoken) and `ngrok.domain` (your static
  domain, host name only, e.g. `calm-fox-123.ngrok-free.app`).
- `account.account_login` (your MT5 login number) and `account.server_name`
  (`HantecMarketsMU-MT5`). Both must appear in the MT5 main window title; this guards against
  trading the wrong account and tells the MT5 terminal apart from other Wine windows
  (MetaEditor, MT4, a second terminal). **Live mode refuses to start without
  `account_login`** (`ACCOUNT_LOGIN_REQUIRED`). While you rehearse on a demo, set them to the
  demo's values.
- Check `symbols.specs` against Hantec's contract specifications (and the MT5 Specification
  window of each symbol). Position sizing depends on `contract_size`, `digits`, `point`,
  `lot_step` and `min_lot`.
- Leave `executor.mode` as `"paper"`.

Then run `./install.sh` again: it writes `~/.tvbridge/ngrok.yml` (via `tvbridge ngrok-config`)
and loads the ngrok LaunchAgent. It prints your TradingView webhook URL,
`https://<your-domain>/webhook`.

### Step 4: doctor and permissions

```bash
tvbridge doctor --prompt
```

`doctor` checks the config, the secret, the mode, macOS permissions, MT5 windows,
calibration, ngrok, the local listener (`/health`) and the engine heartbeat, and exits
non-zero if anything fails. `--prompt` makes macOS show its permission prompts.

Grant **Accessibility** and **Screen Recording** (System Settings -> Privacy & Security ->
Accessibility, and -> Screen & System Audio Recording):

1. Click **+**, press **Cmd+Shift+G**, paste the exact python path that `doctor` prints, and
   switch it on. Do this in both lists.
2. That path is a small launcher. Apple's Python re-executes
   `.../Python3.framework/Versions/3.9/Resources/Python.app`, and that is the process macOS
   actually checks. If a permission still shows as missing after step 1, add `Python.app`
   from that same `Resources` folder to both lists as well.
3. Commands you type in Terminal (`doctor`, `calibrate`, `rehearse`, `read-account`) run
   under **Terminal's** permissions, so add Terminal (or iTerm) to both lists too. The
   engine under launchd has no Terminal parent and uses the python grant.
4. Restart the engine so it picks up the grants:
   `launchctl kickstart -k gui/$(id -u)/com.tvbridge.engine`.

Grants are tied to the binary's path and signature. After an Xcode or Command Line Tools
update, run `doctor` again.

### Step 5: calibrate (on a DEMO account)

Log MT5 into a **demo** account. Put the main window where it will stay (see
[section 6](#6-macos-settings-you-must-change-yourself)), with the Toolbox Trade tab
visible. Then:

```bash
tvbridge calibrate
```

The wizard never clicks anything in MT5; you only hover the mouse. It asks you to press F9
in MT5 to open the New Order ticket, records the ticket's size, then asks you to hover over
the symbol, volume, stop-loss and take-profit fields and the Sell and Buy buttons (3-second
countdown each). It then checks with OCR that the Buy/Sell points sit on their buttons and
that the Volume, Stop Loss and Take Profit points each sit right of their own label (swapped
or misplaced points are refused). Close MetaEditor, MT4 and any second terminal first, or
set `account_login`: if several windows could be the MT5 main window the wizard stops. After you close the ticket (Escape in MT5) it records a safe focus point
(the title bar), the Toolbox area and, optionally, the Trade tab label. It finishes by
reading balance and equity from the Toolbox and saves `~/.tvbridge/calibration.json`.
**Do not click Buy or Sell during calibration.**

### Step 6: rehearse

```bash
tvbridge rehearse --symbol EURUSD --side buy --sl 1.08000 --tp 1.09500
```

This fills a real order ticket, verifies it with OCR and cancels it with Escape. It never
clicks Buy or Sell. Optional: `--lots L`, `--price P`. Look at the screenshots in
`~/.tvbridge/shots/<date>/` to see exactly what was read.

### Step 7: test end to end in paper mode

Create a TradingView alert as described in [tradingview/ALERTS.md](tradingview/ALERTS.md)
(test it with email notifications first), or simulate one locally:

```bash
tvbridge send-test --action buy --symbol EURUSD --price 1.08500 --sl 1.08200 --tp 1.09100
tvbridge status
tvbridge events --limit 20
```

`send-test` goes through the real pipeline in the **current** mode. In live mode it would
place a real trade.

### Step 8: set the mode yourself

paper -> rehearsal (demo, then challenge account) -> live: edit `executor.mode`, then
`launchctl kickstart -k gui/$(id -u)/com.tvbridge.engine`, then `tvbridge status`.

## 6. macOS settings you must change yourself

tvbridge does **not** change system settings. These are yours to set on the Mac Studio:

- **Energy** (System Settings -> Energy): turn on *Prevent automatic sleeping when the
  display is off* and *Start up automatically after a power failure*. While it runs, the
  engine also holds a `caffeinate -dimsu` assertion, but do not rely on that alone.
- **Lock Screen**: set screen saver and display-off to *Never* if you can, and *Require
  password after screen saver begins or display is turned off* to *Never*. A locked screen
  blocks clicks and screenshots; entries then fail closed.
- **Automatic login and FileVault** (a trade-off you must choose): with FileVault on,
  macOS cannot log in automatically. After a power cut or restart the Mac waits at the
  FileVault unlock screen and **nothing runs** until someone types the password (open
  positions keep their server-side SL/TP). With FileVault off you can enable automatic
  login (Users & Groups) and the Mac recovers unattended, but the disk is not encrypted and
  anyone with physical access gets in. Decide for your situation.
- **Focus / Do Not Disturb**: notification banners appear on top of other windows and can
  cover the MT5 ticket or Toolbox. Schedule *Do Not Disturb* to be on all day. This also
  silences tvbridge's own macOS notifications, so set up ntfy or Telegram for your phone
  (`notify` section).
- **Login Items** (System Settings -> General -> Login Items): add **MetaTrader 5** so it
  starts after a reboot, and tick *Save password* in MT5's login dialog.
- **MT5 window**: keep it open and **un-minimized on the main display**, not in full-screen
  mode and not on another Space. Keep the **Toolbox -> Trade** tab visible and tall enough
  to show `max_open_positions + 2` rows (the balance line, the header and every position),
  scrolled to the top, with the default columns (keep the **Swap** column shown). Do not
  resize the window or drag the Toolbox splitter, change MT5's language (OCR expects English
  labels), font or display scaling after calibrating. tvbridge checks this on every read: a
  main window that is not its calibrated size (`MAIN_LAYOUT_CHANGED`), a Trade-list header it
  cannot see, a gap where a row was not read, or equity that does not match the rows' profit
  make the position list "unknown" (`TOOLBOX_INCOMPLETE`): entries are refused and closes
  report that positions may still be open. Display changes (monitor off, Screen Sharing) can
  resize the window by themselves; `tvbridge doctor` and `tvbridge read-account` show it.
- **Keyboard input source**: keep *ABC* or *U.S.* selected. tvbridge types with US key codes.
- **Hands off**: do not use the Mac's mouse or keyboard while the engine may be trading.
  Your input can collide with a click sequence. Use `tvbridge pause` first.
- **Screen Recording re-approval**: macOS Sequoia periodically asks you to re-confirm apps
  that record the screen. Until you answer, screenshots can fail. The system fails closed
  (entries are blocked with `NO_SNAPSHOT`/`SNAPSHOT_STALE`/`ACCOUNT_UNREADABLE`) and sends an
  alert. Check the Mac (or connect with Screen Sharing) when that happens.
- **Software updates**: turn off automatic installation of macOS updates (General ->
  Software Update -> Automatic updates). An overnight restart stops trading until someone
  logs in.
- **Date & Time**: keep *Set time and date automatically* on. Alert freshness checks
  depend on an accurate clock.

## 7. Risk model ($50,000 example)

**Daily reference.** Hantec's daily loss limit is measured from the *higher of balance and
equity at the end of the previous day*, i.e. at 00:00 server time (GMT+3,
`account.server_utc_offset_hours`). tvbridge estimates this reference at its daily rollover
from its own account snapshots (the last one before server midnight plus today's so far),
always taking the highest candidate; the reads of the first minute after midnight can still
raise it (a TP filled in the last seconds before midnight), never lower it. A higher
reference means a tighter floor, so errors lean toward caution. A last pre-midnight read
that jumps more than 3% from the read before it is treated as an OCR misread and ignored.

If the last read before midnight is old (account reads failing over midnight, or the Mac or
engine off), tvbridge waits for a fresh read, stores the result as a **stale estimate**,
sends a critical alert and refuses new entries (`NO_DAY_REFERENCE`) until you compare it
with the Hantec dashboard and run `tvbridge set-reference VALUE`. (When no position was open
before and after and the balance did not change, the estimate is exact and nothing is
blocked.) A reference more than 5% away from the previous day's is flagged in the alert.

With a reference of $50,000 and the default config:

| Level | Formula | Value | What happens there |
|---|---|---|---|
| Hard daily floor (Hantec 4% daily) | reference x 96% | **$48,000** | Firm breach |
| Static max floor (Hantec 8% max) | initial balance x 92% | **$46,000** | Firm breach (never moves) |
| Internal daily floor | reference x 97% (4% - 1% buffer) | $48,500 | New entries stop (3% down on the day) |
| Internal max floor | initial x 93% (8% - 1% buffer) | $46,500 | New entries stop (7% down overall) |
| Entry floor | higher of the two internal floors | $48,500 | No entry may risk going below this |
| Kill floor | higher hard floor + 0.3% of initial | **$48,150** | Kill switch: flatten all, halt |
| Risk per trade | 0.5% of min(balance, equity); cap 1% | $250 (cap $500) | Position size |
| Total open risk | 2% of min(balance, equity) | $1,000 | Sum of all open trades' worst-case loss |

Example with a different day: the previous day ended with balance $50,600 and equity
$51,000. The reference is $51,000, so the hard daily floor is $48,960, the internal daily
floor $49,470 and the kill floor $49,110.

**Position sizing.** loss per lot = |entry - SL| x contract size x (quote->USD rate) +
commission per lot. lots = (risk target / (loss per lot x 1.15)), rounded **down** to the
lot step (the 15% is `slippage_buffer_pct`). Example: EURUSD entry 1.10000, SL 1.09800
(20 pips): loss per lot = 0.00200 x 100,000 x 1 + $5 = $205; $250 / ($205 x 1.15) = 1.06 lots;
booked risk = 1.06 x $235.75 = $249.90.

**Worst-case check.** An entry is refused if, after this trade's SL and every other open
trade's SL were hit, balance would end below the entry floor, or if equity minus this
trade's risk would. Example: equity $48,700, entry floor $48,500: a $243 trade would leave
$48,457, so it is refused (`WORST_CASE`).

**Server-side SL on every order.** An entry without `sl` is refused (`SL_MISSING`). The SL
(and TP if given) is typed into the order ticket, so it lives on Hantec's server and
protects the position even if the Mac, the internet or tvbridge dies.

**Kill switch (best effort).** Every account poll (15 s) compares equity with the kill
floor. At or below it, tvbridge halts new entries, closes every position through the GUI
and sends a critical alert. If positions are still open afterwards (a close failed, rows
not readable, margin still in use), it flattens again after 15 s, then 30 s, 60 s and 120 s,
and every 5 minutes once five attempts in a row failed (e.g. market closed); the
"FLATTEN FAILED" alert repeats at most every 5 minutes. A flatten that finds no position rows
while the ledger or MT5's margin says positions are open is a failure, never "flat". A
kill-level equity read that is implausible (more than 3% away from the previous read, or
below half the balance) is re-read once before acting. This is a last line of defence, not
a guarantee: polling has a delay, each close takes seconds, and a fast market or gap can go
straight through the floor. Your real protection is the stop-loss on every trade plus sizing
that keeps the worst case above the entry floor.

**Booked risk follows the fill.** After a fill, the risk booked for the position is the
larger of the planned risk and the loss at the stop-loss from the actual fill price (spread
and slippage included); a fill worse than the slippage buffer sends an alert (critical if
the worst case now reaches the kill floor). The worst-case checks use the lower of the last
two balance/equity reads, so a single OCR misread upwards never approves an entry.

**Positions tvbridge cannot see.** A ledger position that MT5 no longer shows is only
dropped when the balance (or margin) proves it was closed. Otherwise its risk stays booked
and entries are refused (`POSITIONS_UNCERTAIN`) until it shows again, the balance confirms
the close, or you check MT5 and run `tvbridge resume` (which then treats it as closed). A
position whose stop-loss MT5 shows as removed blocks entries (`SL_MISSING_ON_SERVER`); a
stop-loss moved further away raises its booked risk.

Weekends: `friday_cutoff_server` (22:00) stops new entries late on Friday, but tvbridge
does **not** close positions before the weekend. Weekend gaps can jump past a stop.

## 8. Hantec rules to respect

These are the rules as understood when this README was written. Prop-firm rules change:
read the current Hantec Trader terms and FAQ yourself. **Complying with them is your
responsibility, not tvbridge's.**

- **Automation is allowed on Endurance, but the strategy must be your own.** EAs and
  automation tools may be used, but third-party, purchased or marketed strategies used to
  pass the challenge are prohibited. tvbridge is only plumbing. The trading logic in your
  Pine script must be yours. The example in `tradingview/` is a placeholder, not a strategy.
- **Scalping rule**: trades held for less than 3 minutes that make up 30% or more of your
  profit are a problem. Setting `risk.min_hold_s_for_signal_close` to 180 delays
  close alerts until a position is 3 minutes old (a delayed close never closes a position
  opened after that close alert fired). It does **not** delay reversal closes (an entry
  against an open position closes it at once) or server-side SL/TP hits, so keep targets
  realistic.
- **All-or-nothing risk** (staking a large part of the allowed loss on one trade or one
  idea) is prohibited. The defaults (0.5% per trade, 2% total open risk) are far from that.
  Do not raise the caps to gamble.
- **30-day inactivity breach**: an account with no trade for 30 days is breached.
  tvbridge only trades when your alerts fire. If your strategy is quiet, place a trade
  yourself in time.
- **Funded stage**: maximum 3% floating loss at any time. Keep `max_total_open_risk_pct` at
  2 or below, and remember that gaps and slippage can exceed booked risk. **No trading
  within 3 minutes of red-folder (high-impact) news** unless you bought the add-on.
  tvbridge has **no news calendar**: run `tvbridge pause --reason news` before and
  `tvbridge resume` after, or make your strategy stay out of those windows. Note that a
  server-side SL/TP filling in that window may also count.

## 9. Configuration reference

File: `~/.tvbridge/config.json` (`$TVBRIDGE_HOME/config.json`; chmod 600). It is deep-merged
over built-in defaults, so you only need the keys you change. Keys starting with `_` are
comments. Any other unknown key is an error (this catches typos). **Restart the engine after
every change.** `tvbridge doctor` validates the file.

### server

| Key | Default | Meaning |
|---|---|---|
| `host` | `"127.0.0.1"` | Listener bind address. Keep it on loopback; ngrok connects locally. |
| `port` | `8787` | Listener port (also used by the ngrok LaunchAgent). |
| `path` | `"/webhook"` | Webhook path. A trailing `/` is also accepted. |
| `secret` | generated by `init` | Shared secret; at least 16 characters; must equal `secret` in every alert. |
| `enforce_ip_allowlist` | `true` | Requests arriving through ngrok (they carry `X-Forwarded-For`) must come from `tradingview_ips`. |
| `tradingview_ips` | 4 IPs | TradingView's published webhook source addresses. |
| `allow_local_requests` | `true` | Allow requests from this Mac without `X-Forwarded-For` (used by `send-test`). Best effort: a local process can add the header. `X-Forwarded-For` is only honoured from a loopback peer (the ngrok agent). |
| `max_body_bytes` | `8192` | Larger bodies get HTTP 413. |
| `max_signal_age_s` | `120` | Entries older than this on arrival are refused (`STALE`). Exits: up to 15 minutes, if no position on that symbol/side was opened after the alert fired. |
| `max_future_skew_s` | `30` | Entries dated further in the future are refused (`FUTURE`); future-dated exits are accepted. |
| `rate_limit_per_min` | `30` | Authenticated entry alerts per 60 s window before HTTP 429. Requests with bad JSON or a wrong secret have their own budget of the same size; authenticated close/close_all alerts are never rate-limited. |

### account

| Key | Default | Meaning |
|---|---|---|
| `name` | `"Hantec Endurance 50k"` | Label used in messages. |
| `initial_balance` | `50000.0` | Starting balance: basis of the static max floor and the default paper balance. |
| `currency` | `"USD"` | Account currency. |
| `server_utc_offset_hours` | `3.0` | MT5 server clock offset from UTC. Defines "day" for the daily limit and the trading window. A number, or `"auto"` for brokers whose day ends at the New York close: UTC+3 while US daylight saving time is in effect, UTC+2 otherwise (it switches with the US clocks, e.g. on 1 Nov 2026). Check with the Market Watch clock; `tvbridge doctor` and `tvbridge status` print the server time tvbridge uses. |
| `account_login` | `""` | MT5 login: the main window title must contain it as a whole number (`WRONG_ACCOUNT` otherwise); windows without it (MetaEditor, MT4) are never picked. **Required in live mode** (`ACCOUNT_LOGIN_REQUIRED`). |
| `server_name` | `""` | If set, the MT5 main window title must contain it, e.g. `HantecMarketsMU-MT5`. |

### risk

| Key | Default | Meaning |
|---|---|---|
| `daily_loss_pct` | `4.0` | Firm's daily loss limit, % of the daily reference. |
| `max_loss_pct` | `8.0` | Firm's static max loss, % of `initial_balance`. |
| `daily_buffer_pct` | `1.0` | Entries stop this far above the daily limit (at 3% down). |
| `max_buffer_pct` | `1.0` | Entries stop this far above the max limit (at 7% down). |
| `kill_buffer_pct` | `0.3` | Kill switch at the higher hard floor + this % of `initial_balance`. |
| `risk_per_trade_pct` | `0.5` | Default risk per trade, % of min(balance, equity). |
| `max_risk_per_trade_pct` | `1.0` | Cap for per-alert `risk_pct` overrides (validation: at most 3). |
| `max_total_open_risk_pct` | `2.0` | Cap on the summed worst-case loss of all open trades. |
| `max_open_positions` | `3` | Maximum simultaneous positions. |
| `max_trades_per_day` | `8` | Maximum entries per server day. |
| `max_lots` | `5.0` | Hard cap on the size of any order. |
| `commission_per_lot_usd` | `5.0` | Commission per lot used in sizing. Check Hantec's actual commission. |
| `slippage_buffer_pct` | `15.0` | Extra margin on the stop distance when sizing. |
| `equity_max_age_s` | `90` | Entries need an account snapshot at most this old (`SNAPSHOT_STALE`). |
| `entry_max_delay_s` | `45` | Entries older than this when their turn comes are dropped (`STALE_SIGNAL`). |
| `reverse_on_opposite` | `true` | An entry against an open position closes that position first (reversal) -- even when the entry itself is then refused (paused, halted, stale, no SL, outside the window) -- unless the alert fired more than 15 minutes ago (the same limit as late close alerts), in which case nothing is closed and you get a warning. `false`: such entries are refused (`OPPOSITE_OPEN`). |
| `allow_pyramiding` | `false` | Allow adding to a same-side position on the same symbol. |
| `block_untracked_positions` | `true` | Block entries while MT5 shows positions tvbridge did not open (also on symbols without a spec). `false`: they count toward open risk when their SL is readable; otherwise entries are refused (`TOTAL_OPEN_RISK`). |
| `trading_start_server` | `"00:05"` | No entries before this server time. |
| `trading_end_server` | `"23:50"` | No entries after this server time. |
| `trading_days_server` | `[0,1,2,3,4]` | Server weekdays with entries allowed (Mon=0 .. Sun=6). |
| `friday_cutoff_server` | `"22:00"` | No entries on Friday after this server time; `null` disables it. |
| `min_hold_s_for_signal_close` | `0` | Delay alert-driven closes until a position is this old (180 helps with the scalping rule). |

### symbols

| Key | Default | Meaning |
|---|---|---|
| `suffix` | `".h"` | Appended to the TradingView symbol to get the MT5 name (`EURUSD` -> `EURUSD.h`). |
| `map` | `{}` | Exceptions: TradingView symbol -> exact MT5 name, e.g. `{"XAUUSD": "GOLD.h"}`. |
| `allowed` | `[]` | Symbols that may trade. Empty = every symbol in `specs`. |
| `specs` | 8 symbols | Per-symbol contract data keyed by TradingView symbol. Set a symbol to `null` to remove a default. |

TradingView symbols are normalised: `OANDA:EURUSD`, `EUR/USD` and `EURUSD.h` all become
`EURUSD`. Each entry in `specs`:

| Key | EURUSD default | Meaning |
|---|---|---|
| `contract_size` | `100000.0` | Units per 1.0 lot (XAUUSD default: 100). **Check against Hantec.** |
| `quote` | `"USD"` | Quote currency (USDJPY: `"JPY"`). Used to convert the stop distance into USD. |
| `digits` | `5` | Price decimals typed into the ticket (USDJPY 3, XAUUSD 2). |
| `point` | `0.00001` | Size of one point. |
| `lot_step` | `0.01` | Volume step; sizes are rounded down to it. |
| `min_lot` | `0.01` | Smallest volume; below it the entry is refused (`SIZE_TOO_SMALL`). |
| `min_sl_points` | `50` | Minimum stop distance in points (`SL_TOO_TIGHT`). XAUUSD default: 100. |
| `commission_per_lot_usd` | `null` | Round-trip commission per 1.00 lot of this symbol in USD, used in sizing and open-risk figures instead of `risk.commission_per_lot_usd` (`null` = use that one). Set it for a contract with a different lot size, e.g. a micro. |

Close alerts keep working for a symbol removed from `allowed` while it still has a spec or an
open ledger position; otherwise they are refused (`SYMBOL_NOT_ALLOWED`, with a critical
alert). Positions on symbols without a spec are still read from MT5 (by their row and
ticket), reported as untracked, and closed by `flatten`, close_all and the kill switch.

Defaults exist for EURUSD, GBPUSD, AUDUSD, NZDUSD, USDJPY, USDCAD, USDCHF and XAUUSD. For a
cross such as EURGBP (quote not USD, not starting with USD) the alert must carry
`quote_usd` (see ALERTS.md), otherwise the entry is refused with `NO_FX_RATE`.

### executor

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"paper"` | `paper`, `rehearsal` or `live`. See [section 4](#4-modes-paper-rehearsal-live). |
| `paper_start_balance` | `null` | Paper account start balance; `null` = `account.initial_balance`. |

### executor.gui

| Key | Default | Meaning |
|---|---|---|
| `owner_names` | MT5/Wine process names | Window owners treated as MetaTrader 5. |
| `main_title_contains` | `""` | Extra text the main window title must contain. |
| `order_dialog_title_contains` | `["Order"]` | Title text identifying the New Order ticket. |
| `position_dialog_title_contains` | `["Position","Order"]` | Title text identifying a position's dialog. |
| `require_dialog_text` | `["Market"]` | Text that must be read in the ticket before clicking (market execution). |
| `dialog_timeout_s` | `4.0` | Wait for a dialog to open or close. |
| `result_timeout_s` | `8.0` | Wait for the order result after the click. |
| `action_delay_s` | `0.15` | Pause between GUI actions. |
| `size_tolerance_px` | `12` | Allowed ticket size difference from calibration (`DIALOG_LAYOUT_CHANGED`). |
| `ocr_min_confidence` | `0.3` | OCR results below this confidence are ignored. |
| `account_poll_s` | `15` | Account read interval. |
| `keep_screenshots_days` | `14` | Screenshots older than this are deleted. |
| `volume_field_offset_px` | `85` | Partial close: the volume field of the position window is clicked this many points right of its "Volume" label. |

### mirror

See [section 14](#14-mirror-mode).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Accept `sync` alerts. While false they are refused (`MIRROR_DISABLED`, warning). |
| `units_per_lot` | `100.0` | Strategy units per 1.00 MT5 lot (gold: 100 oz, so 28 oz = 0.28 lots). |
| `stop_distance` | `8.5` | Broker-side protective stop, in price units, measured from the MT5 ticket's own quote. Must be > 0. |
| `tp_distance` | `0` | Take-profit distance from the same quote; `0` = none. |
| `size_tolerance_lots` | `0.005` | Lot differences up to this count as in sync. |
| `max_price_gap_pct` | `0.5` | An entry is refused (`PRICE_GAP`) when the MT5 quote is further than this from the alert price. |
| `allow_adds` | `false` | `false`: never add to an open position (`MIRROR_ADD_REFUSED`). |
| `idea_risk_pct` | `0.0` | `> 0`: the stop distance is `account.initial_balance` x this % / the strategy's units (never wider than `stop_distance`), so a bigger size gets a tighter stop. `0` = always `stop_distance`. |
| `fan_out` | `{}` | Mirror one alert symbol onto several MT5 symbols: `{"XAUUSD": ["XAUUSD", "XAUUSDMICRO"]}`. Key: the alert's symbol; values: `symbols.specs` keys or MT5 names (each needs a spec, no duplicates, at least one). See [mirror fan-out](#mirror-fan-out). |
| `units_per_lot_by_symbol` | `{}` | Per-symbol override of `units_per_lot`, e.g. `{"XAUUSDMICRO": 10}` for a micro contract of 10 oz per lot (28 oz = 2.80 lots). Each key needs a `symbols.specs` entry; values > 0. |

### notify

| Key | Default | Meaning |
|---|---|---|
| `macos` | `true` | macOS Notification Center banners. |
| `ntfy_url` | `""` | e.g. `https://ntfy.sh/<long-random-topic>` for phone push. |
| `telegram_bot_token` | `""` | Telegram bot token (with `telegram_chat_id`). |
| `telegram_chat_id` | `""` | Telegram chat to send to. |
| `min_level` | `"info"` | `debug`, `info`, `warn` or `critical`. |

### ngrok

| Key | Default | Meaning |
|---|---|---|
| `authtoken` | `""` | Your ngrok agent authtoken. Written to `~/.tvbridge/ngrok.yml` (chmod 600). |
| `domain` | `""` | Your static ngrok domain, host name only (no `https://`). |

## 10. CLI reference

Run from the app folder as `.venv/bin/python -m tvbridge <command>` (or `tvbridge <command>`
with the shell function from [section 5](#5-quick-start-on-the-mac-studio)).

| Command | What it does |
|---|---|
| `tvbridge init` | Creates `~/.tvbridge` (0700) and, if absent, `config.json` from `config.example.json` with a random `server.secret` (chmod 600). Prints the paths and, if `ngrok.domain` is set, the webhook URL. Never overwrites an existing config. |
| `tvbridge run` | Runs the engine in the foreground: listener, queue, risk, executor, scheduler; logs to `~/Library/Logs/tvbridge/` and stderr; keeps the Mac awake with `caffeinate`. Normally started by the LaunchAgent; do not run a second copy while the agent runs. |
| `tvbridge doctor [--prompt]` | Health checks (config, secret, mode, python binary, permissions, MT5 windows, calibration, ngrok, `/health`, heartbeat). Exits non-zero on any failure. `--prompt` triggers the macOS permission prompts. |
| `tvbridge calibrate` | Interactive calibration wizard (you hover, it never clicks). Use a DEMO account. |
| `tvbridge status [--json]` | Mode, paused/halted, latest account snapshot and its age, today's floors, open ledger positions, untracked positions, queue size, trades today. |
| `tvbridge read-account` | One read of the MT5 Toolbox: balance, equity, margin, positions as tvbridge sees them (`UNKNOWN (TOOLBOX_INCOMPLETE: ...)` when the list cannot be verified as complete). May click the Trade tab label if calibrated and the first read fails. |
| `tvbridge rehearse --symbol S --side buy\|sell --sl X [--tp Y] [--lots L] [--price P]` | Fills one order ticket, verifies it with OCR and cancels with Escape. Never clicks Buy/Sell. |
| `tvbridge send-test --action A --symbol S [--price P --sl X --tp Y --url U]` | Posts a correctly signed alert to the local listener (or to `--url`). It runs through the full pipeline in the **current mode**; in live mode every entry asks you to type `LIVE` first (also with `--url`). Through the public ngrok URL it is expected to get HTTP 403 from the IP allowlist, which proves the allowlist works. |
| `tvbridge pause [--reason R]` | Blocks new entries. Closes, flatten and the kill switch keep working. |
| `tvbridge resume [--force]` | Clears pause **and** halt, and confirms that ledger positions MT5 no longer shows (`POSITIONS_UNCERTAIN`) are closed. Only run it after checking MT5 matches `tvbridge status`. A kill-switch halt is only cleared when a fresh account read shows equity above the kill floor (`--force` overrides). |
| `tvbridge flatten [--yes]` | Asks the running engine to close every position. Asks you to type `FLATTEN` unless `--yes`. |
| `tvbridge set-reference VALUE [--date YYYY-MM-DD] [--force]` | Overrides the daily reference (default: today's server date), e.g. with the value shown on the Hantec dashboard. Lowering it, or a value more than 10% away from the last balance/equity, prints the old and new floors and asks you to type the value again (refused without a terminal unless `--force`). |
| `tvbridge events [--limit N]` | Recent events (signals, rejections, fills, warnings). |
| `tvbridge ngrok-config` | Writes `~/.tvbridge/ngrok.yml` from `ngrok.authtoken` (chmod 600) and prints `https://<domain>/webhook`. Called by `install.sh`. |

## 11. Limitations and failure modes

Stated plainly, so you can decide whether this is acceptable for your money:

- **GUI automation is fragile.** tvbridge clicks at calibrated positions in a Wine-based
  app. An MT5 **LiveUpdate** can change the order ticket's layout or size; tvbridge then
  refuses with `DIALOG_LAYOUT_CHANGED` and you must re-run `tvbridge calibrate` and
  rehearse again. Changes it cannot detect by size are caught by OCR verification and the
  button-label check, which refuse rather than guess.
- **OCR misreads** make verification fail (`VERIFY_FAILED`) and the trade is skipped, never
  retried. You will occasionally miss trades.
- **A locked screen, screen saver, login window or permission prompt** blocks clicks and
  screenshots. Entries are blocked until the desktop is usable again.
- **The Mac and its internet connection are single points of failure.** Power, Wi-Fi, ISP,
  ngrok or TradingView outages stop new trades. Open positions keep their server-side SL/TP.
  Alerts that arrive late are refused as stale. That is deliberate: no trade is better than
  a late one.
- **TradingView webhooks are not guaranteed.** A webhook can occasionally fail or arrive
  late, and TradingView does not queue it for you. Alerts on Essential/Plus plans **expire
  after about 2 months** and then stop silently. Note the expiry date and recreate them in
  time.
- **Uncertain executions.** If the ticket disappears after the click, or MT5 answers with a
  timeout, "no connection" or an error, tvbridge cannot know whether the order filled. It
  marks the signal failed, halts entries and alerts you. If the position shows up in MT5
  within 10 minutes (on the read right after, or a later one) tvbridge adopts it into its
  ledger; otherwise it stays an untracked position that blocks entries until you close it.
  You must check MT5 and run `tvbridge resume`. Executor errors **before** the click
  (`VERIFY_FAILED`, `MT5_NOT_FOUND`, `ORDER_DIALOG_NOT_OPENED`, ...) do not halt: nothing was
  sent, so only that signal fails (warning; critical after 3 in a row).
- **The kill switch is best effort** (15 s polling plus GUI closes that take seconds).
- **The daily reference is an estimate** from tvbridge's own snapshots (`set-reference` to
  correct it).
- **Fills differ from alert prices.** Sizing uses the alert's price; MT5 fills at market,
  plus spread and slippage. `slippage_buffer_pct` covers part of that, not all; the booked
  risk is raised to the loss from the actual fill price.
- **Close signals close what MT5 shows** for that symbol (and side), including positions
  you opened by hand.
- **One MT5 window.** tvbridge picks the window whose title has your `account_login`; if
  several windows qualify it refuses (`AMBIGUOUS_MAIN_WINDOW`) rather than guess.
- **Server time offset** is a fixed number in the config unless you set it to `"auto"` (US DST
  rule); with a fixed number, adjust it yourself when your broker changes its clock.
- **Symbols without a `symbols.specs` entry** cannot be traded by tvbridge, but a position you
  open by hand on one is read (its row needs the Ticket column), reported as untracked
  (blocking entries by default) and closed by flatten, close_all and the kill switch.
- **No news filter, no weekend flattening, no automatic re-arming** after a halt.

## 12. Operations

| Task | How |
|---|---|
| Overall state | `tvbridge status` (or `--json`) |
| Recent activity | `tvbridge events --limit 50` |
| Stop new entries (news, maintenance, using the Mac) | `tvbridge pause --reason "NFP"`, later `tvbridge resume` |
| Close everything now | `tvbridge pause --reason flat`, then `tvbridge flatten` (type `FLATTEN`): flatten alone does not stop new entries |
| Restart the engine (after config edits) | `launchctl kickstart -k gui/$(id -u)/com.tvbridge.engine` |
| Stop the engine until next login | `launchctl bootout gui/$(id -u)/com.tvbridge.engine` |
| Start it again | `./install.sh` (or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tvbridge.engine.plist`) |
| Restart the tunnel | `launchctl kickstart -k gui/$(id -u)/com.tvbridge.ngrok` |
| Uninstall | `./uninstall.sh` (removes both LaunchAgents, keeps all data; does not close positions) |
| Upgrade tvbridge | `tvbridge pause`, wait until nothing is executing, copy the new files, `./install.sh`, check `tvbridge status`, `tvbridge resume` |

**Logs** are in `~/Library/Logs/tvbridge/`: the engine's rotating log `tvbridge.log` (5 x 5 MB),
plus `engine.stdout.log` / `engine.stderr.log` (whatever launchd captured: warnings, errors and
crash tracebacks only, since the full log goes to the rotating file; not rotated, so truncate
them occasionally) and `ngrok.log`. Follow live with
`tail -f ~/Library/Logs/tvbridge/*.log`.

**Screenshots** of every GUI step are in `~/.tvbridge/shots/YYYYMMDD/`, named by time and
step. They are the evidence for every order and are deleted after `keep_screenshots_days`
(checked hourly); when the folders exceed 2 GB in total the oldest days go first (today's
are kept). Failed account reads keep only the first screenshot of a failure streak plus
`account_failed_latest.png`. The engine alerts you when the disk has less than 2 GB free.

**Other files** in `~/.tvbridge`: `tvbridge.db` (queue, ledger, snapshots, events; the
engine writes to it all the time, even when paused, so back it up online with
`sqlite3 ~/.tvbridge/tvbridge.db ".backup '/path/to/tvbridge-backup.db'"`, or stop the engine
and copy `tvbridge.db` together with any `tvbridge.db-wal`/`-shm` files), `calibration.json`,
`heartbeat.json` (updated every 10 s while the engine runs; it also records a failed start
and a stalled executor), `ngrok.yml`, `bin/ngrok`.

**Watchdog.** If the executor thread dies or a task runs far longer than any GUI sequence
(5 minutes for an entry, close or flatten, 3 for an account read), the engine sends a critical alert, marks the
heartbeat `executor_stalled` and exits, so launchd restarts it. A start that fails (bad
calibration, port in use, database error) is written to the heartbeat and alerted at most
every 15 minutes while launchd keeps retrying.

**ngrok** runs with request inspection off (`--inspect=false`), so webhook bodies (with your
secret) are not kept on its local API at 127.0.0.1:4040.

**A good daily routine**: glance at `tvbridge status` (mode, halted?, snapshot age, floors);
compare the daily reference with the Hantec dashboard; check MT5 is connected; after any MT5
update, re-calibrate and rehearse before trading live again.

## 13. Troubleshooting by reason code

Rejections show up in `tvbridge status`, `tvbridge events` and notifications as
`CODE: explanation`.

### Webhook / alert (the HTTP answer TradingView gets)

| Code / status | Meaning | What to do |
|---|---|---|
| HTTP 401 `UNAUTHORIZED` | Wrong or missing `secret` | Copy `server.secret` into the alert again (no extra spaces or smart quotes). |
| HTTP 403 `FORBIDDEN` | Source IP not in `tradingview_ips` (event `ip_blocked`), or a local request with `allow_local_requests` false | Someone else is hitting your URL, or TradingView changed its IPs: check their docs and update the list. |
| HTTP 404 `NOT_FOUND` | Wrong path or method | Use `POST` to `https://<domain>/webhook`. |
| HTTP 411 `LENGTH_REQUIRED` / 413 `TOO_LARGE` / 400 `BAD_LENGTH` | No or invalid Content-Length / body over `max_body_bytes` | Shorten the alert message. |
| HTTP 429 `RATE_LIMITED` | More than `rate_limit_per_min` authenticated entry alerts in a minute, or that many requests with bad JSON / a wrong secret (separate budget; warn notification) | An alert is firing in a loop, or someone is probing your URL. Close/close_all alerts are never rate-limited. |
| HTTP 500 `INTERNAL` | Unexpected error in the listener | Check the engine log; report it. |
| HTTP 503 `STORE_ERROR` | Database error | Check disk space and the engine log. TradingView may retry. |
| `ID_REUSED` (HTTP 400) | An entry reused an explicit `id` of an earlier firing on the same symbol | Give every alert firing a unique `id`, or leave `id` out (never `{{strategy.order.id}}` or a fixed text). A reused id on an exit (or on another symbol) is processed under a new id with a warning. |
| `BAD_JSON` | The alert message is not a valid JSON object | Usually a strategy order without `alert_message` (gives `"order":}`), a missing quote, or smart quotes. Test with email notifications. |
| `BAD_ACTION` | `action` missing or unknown | Use buy/sell/close/close_all or an alias (ALERTS.md). |
| `PARTIAL_EXIT` (as `BAD_ACTION`) | A `sell` while `market_position` is still `long` (or `buy` while `short`): a partial exit | tvbridge cannot close part of a position; the position stays open (critical alert). Use explicit `alert_message` JSON (ALERTS.md section 4a). |
| `NO_SYMBOL` | No `symbol`/`ticker` | Add `"symbol":"{{ticker}}"` (only `close_all` may omit it). |
| `SYMBOL_NOT_ALLOWED` | Symbol not in `symbols.allowed` / `symbols.specs` (a close is still accepted while the symbol has a spec or an open ledger position) | Add a spec checked against Hantec, or fix `symbols.map`. A refused close sends a critical alert: close that position by hand. |
| `BAD_NUMBER` | price/sl/tp/risk_pct/quote_usd not numeric | Make sure placeholders produce plain numbers (no thousands separators). |
| `NO_TIME` | Missing or unreadable `time` | Add `"time":"{{timenow}}"`. |
| `BAD_POSITION` | A `sync` alert whose `position` is not `long`, `short` or `flat` | Use the mirror template from ALERTS.md section 10 unchanged. |
| `BAD_SIZE` | A `sync` alert with a long/short position but no positive numeric `size` | Same: `"size":{{strategy.market_position_size}}` without quotes. |
| `STALE` | Entry older than `max_signal_age_s` on arrival; an exit older than 15 minutes, or one fired before a position on that symbol/side was opened | TradingView delivery delay, or the Mac clock is wrong. A refused exit sends a critical alert: close the position by hand if the strategy exited. |
| `FUTURE` | Entry dated more than `max_future_skew_s` ahead (exits are accepted) | The Mac clock is behind: turn on automatic time. |

### Risk guard (entry refused, status `rejected`)

| Code | Meaning | What to do |
|---|---|---|
| `HALTED` | Entries halted after an uncertain or failed execution, kill switch or interrupted order | Check MT5 against `tvbridge status`, fix any mismatch, then `tvbridge resume`. |
| `PAUSED` | You paused | `tvbridge resume`. |
| `BAD_ACTION` | Not a buy/sell reached the entry path | Report it; should not happen. |
| `NO_SPEC` | No `symbols.specs` entry for the symbol | Add one. |
| `WINDOW` | Outside trading hours/days or after the Friday cutoff | Expected; adjust `trading_*` if needed. |
| `STALE_SIGNAL` | More than `entry_max_delay_s` passed before execution (status `expired`) | Queue was busy, the Mac was asleep, or the engine restarted. By design. |
| `NO_SNAPSHOT` | No account read yet | Is MT5 open with the Trade tab visible? Run `tvbridge read-account`. |
| `SNAPSHOT_STALE` | Last account read older than `equity_max_age_s` | Account reads are failing: screen locked, MT5 minimized, Screen Recording permission or re-approval pending. Check `tvbridge read-account` and the log. |
| `NO_DAY_REFERENCE` | No daily reference yet, or today's reference is a stale estimate (source `stale_estimate`: account reads failed or the engine was off over midnight) | Wait for the first account read after install or rollover; for a stale estimate compare with the Hantec dashboard and run `tvbridge set-reference VALUE`. |
| `UNTRACKED_POSITIONS` | MT5 shows positions tvbridge did not open (also on symbols without a spec) | Close or handle them (or, not recommended, set `block_untracked_positions` false). |
| `POSITIONS_UNCERTAIN` | A ledger position is not visible in MT5 but the balance did not change, or the Toolbox position list could not be verified as complete (`TOOLBOX_INCOMPLETE`) | Check MT5. Position closed: `tvbridge resume`. Still open: make the Toolbox show every row (window size as calibrated, Trade list scrolled to the top, header visible). |
| `SL_MISSING_ON_SERVER` | MT5 shows an open tvbridge position without a stop-loss | Set the stop-loss in MT5 right away; entries resume when MT5 shows it. |
| `NO_PRICE` | Alert has no `price` | Add `"price":{{close}}`. |
| `SL_MISSING` | Alert has no `sl` | Every entry needs a stop-loss price. |
| `SL_WRONG_SIDE` | Buy SL not below price, or sell SL not above | Fix the strategy's stop calculation. |
| `TP_WRONG_SIDE` | Buy TP not above price, or sell TP not below | Fix the target calculation. |
| `SL_TOO_TIGHT` | Stop closer than `min_sl_points` x `point` | Widen the stop or adjust the spec. |
| `OPPOSITE_OPEN` | Opposite position open and `reverse_on_opposite` false | Close it first or enable reversals. |
| `PYRAMIDING` | Same-side position already open | Expected unless `allow_pyramiding` is true. |
| `MAX_POSITIONS` | `max_open_positions` reached | Expected. |
| `MAX_TRADES_DAY` | `max_trades_per_day` reached | Expected; resets at server midnight. |
| `NO_FX_RATE` | Cross pair with no way to convert to USD | Send `quote_usd` in the alert. |
| `SIZE_TOO_SMALL` | Size rounded down below `min_lot` | Stop too wide for the risk budget. |
| `TOTAL_OPEN_RISK` | Would exceed `max_total_open_risk_pct`, or an open position's risk is unknown (with `block_untracked_positions` false: an untracked position without a readable SL) | Wait for open trades to close; give manual positions a stop-loss. |
| `BELOW_ENTRY_FLOOR` | Equity at or below the internal floor | No more entries today (or at all, near the max floor). |
| `WORST_CASE` | If this stop were hit, the account would end below the entry floor | Expected near the floors. |
| `RISK_ERROR` | Internal error in the risk check; the entry was refused (fail closed) | Report it with `tvbridge events`. |
| `KILL` | Equity reached the kill floor: everything was flattened and entries halted | Review what happened. Entries stay halted until you run `tvbridge resume` (refused while equity is still at or below the kill floor; `--force` overrides). |
| `SUPERSEDED_BY_CLOSE` (status `expired`) | A close (same symbol, that side or both) or close_all arrived after this entry while it waited in the queue | Normal: the strategy already exited, so the entry is never placed. |
| `FLATTEN_PENDING` / `PAUSED` / `HALTED` right before the order | A flatten, pause or halt arrived while the entry was being prepared | Nothing was sent. |
| `ABORTED` (status `rejected`) | The same, noticed right before the Buy/Sell click: the ticket was cancelled with Escape | Nothing was sent. |

### Mirror mode (`sync` alerts)

A sync that opens a position can also show every risk-guard code above; one that closes or
reduces can show `CLOSE_FAILED`.

| Code | Meaning | What to do |
|---|---|---|
| `MIRROR_DISABLED` (status `rejected`) | A `sync` alert arrived while `mirror.enabled` is false; nothing was done | Set `mirror.enabled` to true and restart the engine, or delete the alert. |
| `IN_SYNC` (status `done`) | MT5 already matches the strategy's position (within `size_tolerance_lots`) | Normal. |
| `SUPERSEDED_BY_SYNC` (status `expired`) | A newer sync for the same symbol was already waiting; only the newest one acts | Normal after a busy moment or a restart. |
| `FANNED_OUT` (status `done`) | The alert's symbol has a `mirror.fan_out` entry: the alert was split into one sync per target symbol (ids `<alert id>@<MT5 symbol>`), which carry the real outcomes | Normal. Look at the child signals in `tvbridge status` / the notifications; each target can succeed or be refused on its own. |
| `MIRROR_ADD_REFUSED` (status `rejected`) | The strategy's position is larger than MT5's on the same side and `mirror.allow_adds` is false | Expected when the strategy scales in, or when the MT5 position was reduced by hand. Nothing was opened. |
| `MIRROR_NOT_AN_ENTRY` (status `rejected`) | The alert reports a partial exit (`prev_size` larger than `size`, same side) but MT5 has no position on the symbol (the entry was refused or missed earlier) | Nothing was opened: a partial exit never starts a position. |
| `POSITIONS_UNKNOWN` (status `failed`) | The MT5 position list could not be read (`TOOLBOX_INCOMPLETE`, `ACCOUNT_UNREADABLE`), so tvbridge cannot tell what to change. A sync to flat still closes what it can see | Critical alert: compare MT5 with the strategy and fix by hand; make the Trade list fully visible. |
| `STALE_SIGNAL` (status `expired`) / `FUTURE` | The sync was too old (or dated ahead) to open a position; its closing part still ran | By design. |
| `QUOTE_UNREADABLE` (status `failed`) | The bid / ask line of the order ticket could not be read, so no stop could be computed; the ticket was cancelled | Nothing was sent. Look at the `order_*_quote` screenshot in `~/.tvbridge/shots`; rehearse. |
| `PRICE_GAP` (status `failed`) | The MT5 quote is further than `mirror.max_price_gap_pct` from the alert price: wrong symbol in the ticket, a stale alert or a feed problem | Nothing was sent. Check `symbols.map` and the MT5 symbol. |
| `PARTIAL_VERIFY_FAILED` (in `CLOSE_FAILED`) | After typing the volume for a partial close, the close button did not read back that volume and the position's ticket; cancelled with Escape | Nothing was sent; the position is still larger than the strategy's. Reduce it by hand; check `executor.gui.volume_field_offset_px` and the screenshot. |

### MT5 executor (status `failed` or `error`)

Errors **before** the Buy/Sell click (`VERIFY_FAILED`, `MT5_NOT_FOUND`, `WRONG_ACCOUNT`,
`MAIN_LAYOUT_CHANGED`, `STRAY_DIALOG`, `GUI_BUSY`, `GUI_ERROR`, ...) mean nothing was sent: the
signal fails with a warning (critical after 3 in a row) and entries keep running. Anything
that may have reached the broker (`UNCERTAIN_EXECUTION`, `RESULT_MISMATCH`, an unexpected
exception) halts new entries until `tvbridge resume`.

| Code | Meaning | What to do |
|---|---|---|
| `MT5_NOT_FOUND` | No MT5 main window (at least 600x400) on screen | Start MT5, un-minimize it, bring it to the current Space; `tvbridge doctor` lists the windows it sees. |
| `WRONG_ACCOUNT` | No MT5 window title contains `account_login` (as a whole number) / `server_name` | MT5 is logged into another account (e.g. still the demo). Intentional guard. |
| `AMBIGUOUS_MAIN_WINDOW` | Several windows could be the MT5 main window (MetaEditor, MT4, a second terminal) | Close the others, or set `account.account_login`. |
| `ACCOUNT_LOGIN_REQUIRED` | Live mode with an empty `account.account_login`: the engine does not start | Set your MT5 login number in config.json. |
| `MAIN_LAYOUT_CHANGED` | The MT5 main window is not its calibrated size (entries refused; positions read as unknown) | Restore the window size (a display change can resize it), or `tvbridge calibrate`. |
| `STRAY_DIALOG` | Another MT5 window stayed open after two Escapes. Entries are refused; closes continue unless that window covers the position row | Close it by hand. |
| `ORDER_DIALOG_NOT_OPENED` | F9 did not open the order ticket in time | MT5 not focused, Accessibility permission missing, F9 remapped, or the ticket title changed (`order_dialog_title_contains`). |
| `DIALOG_LAYOUT_CHANGED` | Ticket size differs from calibration | MT5 LiveUpdate, display scaling or font change: `tvbridge calibrate`, then rehearse. |
| `VERIFY_FAILED` | OCR did not read back symbol/volume/SL/TP/"Market" exactly | Nothing was sent. Look at the screenshot in `~/.tvbridge/shots`. Wrong field points (re-calibrate), symbol autocomplete, keyboard layout not U.S., or an OCR misread. |
| `BUTTON_LABEL_MISMATCH` | The label at the calibrated Buy/Sell point is not the wanted side | Nothing was sent. Re-calibrate. |
| `ACCOUNT_UNREADABLE` | Balance/Equity line not readable in the Toolbox | Select the Trade tab, make the Toolbox taller, check Screen Recording, re-calibrate the Toolbox area. The last failing screenshot is `account_failed_latest.png`. |
| `TOOLBOX_INCOMPLETE` | Balance/equity were read but the position list cannot be trusted as complete: window size changed, Trade-list header not visible, a gap where a row was not read, rows read but margin in use, or equity - balance not matching the rows' profit | Entries are refused (`POSITIONS_UNCERTAIN`); a close or flatten closes every visible row and reports the rest as possibly open. Make the whole Trade list visible as calibrated, keep the Swap column shown; `tvbridge read-account` shows the reason. |
| `NO_ROWS_VISIBLE` (in `CLOSE_FAILED`) | close_all/flatten found no position rows while the ledger or MT5's margin says positions are open | Not treated as flat: the ledger is kept and the kill switch keeps retrying. Check MT5 and close by hand. |
| broker rejection | MT5 reported e.g. "market closed", "not enough money", "invalid stops", "requote" | Not retried. Check the screenshot and the market. |
| `UNCERTAIN_EXECUTION` | Clicked, but the result could not be confirmed (no answer, the ticket vanished, or MT5 reported a timeout / no connection / an error). Entries halted. | Check MT5: if a position opened and shows in the Toolbox within 10 minutes, tvbridge adopts it into the ledger (otherwise it stays untracked and blocks entries). Make sure it has its SL, then `tvbridge resume`. |
| `REVERSAL_CLOSE_FAILED` | Could not close the opposite position before a reversal; the entry was not placed | Check MT5 and close by hand if needed. |
| `CLOSE_FAILED` | A close, close_all or flatten did not complete | Positions may still be open: check MT5 and close by hand. |
| `INTERNAL_ERROR` | Unexpected error while processing a signal (an entry also halts) | Check MT5 and the log; report it. |
| `GUI_ERROR` | An unexpected GUI/driver error before the click (nothing was sent), or while closing | Check the screenshot and the log; rehearse. |
| `BAD_REQUEST` / `NO_PRICE` / `NO_SPEC` / `PAPER_STATE_INVALID` / `BAD_MODE` | Invalid order request, or (paper) no price, no spec or a damaged `paper_state`, or an unknown `executor.mode` | Should not happen with a valid config; report it. |
| `ORDER_DIALOG_CLOSED` / `DIALOG_MOVED` | The ticket closed or moved before the click; nothing was sent | Leave MT5 alone while tvbridge works (`tvbridge pause` first). |
| `RESULT_MISMATCH` | MT5 reported a fill for another side, volume or symbol, or at a price far from the alert price (treated as uncertain: entries halted) | Check MT5 immediately. |
| `POSITION_DIALOG_NOT_OPENED` / `CLOSE_BUTTON_NOT_FOUND` | A close could not open the position window or read its close button; nothing was sent | Close by hand; re-calibrate if it repeats. |
| `POSITION_STILL_LISTED` | MT5 said the position closed but the Toolbox still lists it | Check MT5. |
| `GUI_BUSY` | Another tvbridge process (e.g. `rehearse`) was driving MT5 | Retry when it is done. |
| `INTERRUPTED` | The engine stopped (crash, restart, power cut) while an order was in progress | Check MT5, then `tvbridge resume`. |
| `NO_POSITION` | A close alert found nothing to close (e.g. already stopped out on the server), or a delayed close found only positions opened after it fired | Normal. |
| `DEFERRED` | A close waits for `min_hold_s_for_signal_close` | Normal; it runs when the hold time is reached. |
| `REHEARSED` / `REJECTED` / `CLOSED` | Message prefixes: rehearsal result, broker rejection, close confirmed by the row disappearing | Informational. |
| `closed_on_server` | A ledger position disappeared from MT5 and the balance (or margin) shows it was closed (SL/TP hit or closed by hand) | Informational. |
| `EXECUTOR STALLED` | The executor thread died or a task ran far too long; the engine exits and launchd restarts it | Check MT5 and the log; `tvbridge doctor`. |
| calibration missing | `tvbridge calibrate` has not been run (or the file is invalid) | Run it. |

### Setup problems

| Symptom | What to do |
|---|---|
| Engine keeps restarting | `tvbridge status` shows "last start FAILED: ..." (also alerted every 15 minutes); `tail ~/Library/Logs/tvbridge/engine.stderr.log`; usually a config or calibration error, or port 8787 in use. `tvbridge doctor` shows it precisely. |
| `doctor` shows permissions false | See [step 4](#step-4-doctor-and-permissions); restart the engine after granting. |
| ngrok not connecting | `tail ~/Library/Logs/tvbridge/ngrok.log`: invalid authtoken, domain not on your account, or another ngrok agent already running on the same account (the free plan allows one). |
| TradingView shows webhook errors | The tunnel or engine is down: `tvbridge doctor`, `tvbridge status`. |

## 14. Mirror mode

**What it does.** Some strategies manage everything inside TradingView: stops, targets,
trailing, a partial profit (close about 30 %, keep a runner), and they have no per-order
alert messages. For those, one strategy alert that fires on every order fill sends the
strategy's *resulting position* (`"action":"sync"`: long/short/flat and the size), and
tvbridge makes the MT5 position on that symbol match it:

| Strategy says | MT5 has | tvbridge does |
|---|---|---|
| flat | anything on the symbol | closes everything on the symbol (also positions it did not open) |
| long 28 | nothing | opens a long through the risk guard |
| long 19.6 (after a partial exit) | the long from before | closes the difference (partial close) |
| the same as MT5 | | nothing (`IN_SYNC`) |
| short | a long | closes the long, re-reads the account, then opens the short through the risk guard |
| more than MT5 on the same side | | nothing, with a warning (`MIRROR_ADD_REFUSED`) unless `mirror.allow_adds` |

MT5's own position list is the truth, not tvbridge's ledger. The alert template and the
TradingView steps are in [tradingview/ALERTS.md](tradingview/ALERTS.md) section 10.

**Config.** Section `mirror` ([reference](#mirror)): set `enabled` to true, check
`units_per_lot` (gold: 100 oz per lot) and `stop_distance`, then restart the engine.
`tvbridge status` shows whether mirror mode is on and the current scale per symbol.

**Safety properties.**

- **Exits fail open.** Closing and reducing always run: when paused, halted, outside the
  trading window, and for an alert up to 15 minutes late. If the position list cannot be
  read, a sync to flat still closes what is visible; any other sync fails with
  `POSITIONS_UNKNOWN` and a critical alert.
- **Entries fail closed.** Opening goes through every risk check of a normal entry (floors,
  trading window, max trades per day, untracked positions on other symbols, pause/halt, ...)
  with a synthetic stop `stop_distance` from the alert price and `max_risk_per_trade_pct`.
  It needs a price, an alert younger than `risk.entry_max_delay_s`, and is never retried. An
  uncertain result halts entries exactly like a normal entry.
- **Sizes are capped and scaled.** The size opened is the smallest of the strategy's size,
  the risk guard's size and `risk.max_lots`. If that is less than the strategy asked for,
  tvbridge remembers the ratio (the *scale*) and applies it to later targets of that
  position: with scale 0.5, a partial exit from 28 to 19.6 units reduces MT5 from 0.14 to
  0.09 lots. Lots are always rounded down; a remaining target below the minimum lot closes
  the position. The scale resets when the symbol is flat.
- **Only the newest sync per symbol acts** (`SUPERSEDED_BY_SYNC`), checked again right
  before an order is sent.
- **The price-gap check.** TradingView's price feed differs from the broker's (about $0.9
  on gold). The stop is therefore measured from the bid/ask quote read in the MT5 order
  ticket itself (buy: ask - `stop_distance`, sell: bid + `stop_distance`), and an entry is
  refused (`PRICE_GAP`) when that quote is more than `max_price_gap_pct` from the alert
  price, which also catches a wrong symbol in the ticket. No readable quote
  (`QUOTE_UNREADABLE`): no order.
- **Partial exits** type the volume into the position window and click its close button
  only after reading back that volume and the ticket number (`PARTIAL_VERIFY_FAILED`
  otherwise, nothing sent). A partial-exit alert never opens a position
  (`MIRROR_NOT_AN_ENTRY`).
- **Adds are refused** by default: if the strategy scales in, MT5 keeps the smaller size.

**Limits. Read these.**

- **The broker-side stop is only a safety net** at a fixed distance. The strategy's real
  exits (stop, target, trailing, runner) arrive as alerts. **If the Mac, the internet
  connection, ngrok or TradingView fails, the position is protected only by that fixed
  stop**: it is not trailed, no partial profit is taken, and it stays open until the stop is
  hit or you close it. A stop the strategy moved (break-even, trailing) exists only in
  TradingView.
- **Latency.** Every action takes about 10 seconds of GUI work after the alert arrives, so
  fills differ from the backtest, most of all for a strategy on a seconds chart. Fast
  sequences of fills are collapsed to the newest state.
- **Missed alerts are not replayed.** If an exit alert is lost, MT5 stays in the position
  until the next sync arrives. Compare `tvbridge status` with the strategy from time to time.
- **A refused entry stays refused.** If the risk guard (or a pause, a halt, a stale alert)
  refuses the opening, tvbridge does not join the trade later.
- **One strategy per symbol**, and no ordinary buy/sell/close alerts on that symbol.
- **`min_hold_s_for_signal_close` does not apply** to sync alerts.
- `tvbridge send-test` cannot send a sync alert; test with a TradingView alert in paper mode.

### Mirror fan-out

One alert can be mirrored onto several MT5 symbols, for example the standard gold contract
and a micro contract, **each as its own independent position with the strategy's full size**:

```json
"symbols": {
  "map":   { "XAUUSDMICRO": "XAUUSDmicro" },
  "specs": { "XAUUSDMICRO": { "contract_size": 10, "quote": "USD", "digits": 2, "point": 0.01,
                              "min_sl_points": 100, "commission_per_lot_usd": 0.5 } }
},
"mirror": {
  "enabled": true,
  "fan_out": { "XAUUSD": ["XAUUSD", "XAUUSDMICRO"] },
  "units_per_lot_by_symbol": { "XAUUSDMICRO": 10 }
}
```

- The alert stays the one for `XAUUSD`. It is finished at once as `FANNED_OUT`, and one child
  sync per target (id `<alert id>@<MT5 symbol>`) is processed in the listed order by the
  normal mirror logic: own ledger row, own scale, own result and notification. To mirror
  only onto the micro, list only the micro.
- **Sizes.** 28 oz is 0.28 lots on `XAUUSD` (100 oz per lot) and 2.80 lots on the micro
  (10 oz per lot, `units_per_lot_by_symbol`). `contract_size` of the micro spec must match
  (10), or its risk is computed wrongly. **Check both against the broker's specification.**
- **Risk is per target, and it adds up.** Every child is an entry of its own: capped by
  `max_risk_per_trade_pct` and `risk.max_lots`, counted in `max_open_positions` and
  `max_trades_per_day`, and the second child sees the first one's booked risk
  (`max_total_open_risk_pct`). Two targets can therefore lose up to twice
  `max_risk_per_trade_pct` on one strategy trade. If the guard refuses one child (for
  example `TOTAL_OPEN_RISK`), the other stays open and the refused one is reported as
  rejected; it is not retried.
- With `idea_risk_pct` the stop distance is the same on every target (the same ounces).
- Exits fan out too: a partial exit reduces each target with its own scale, flat closes each.
- Only the children of the newest alert act (`SUPERSEDED_BY_SYNC`); after a restart a
  half-finished fan-out is completed without duplicating children.
- A target symbol must not get sync alerts of its own from another strategy.
- `tvbridge status` lists the targets on the `mirror:` line.
