# Moving the Mac engines to the office Windows laptop (2026-10-12)

Decision (Riaz, 2026-10-11): the office Windows laptop runs FundingPips, Audacity and
The5ers; the AWS server keeps Hantec; the Mac then runs nothing (3 + 1).
A Claude Code session ON THE LAPTOP executes this file. The Mac session (Remote Control,
"MetaTrader automated file status") does the Mac-side steps when asked. Rules from Riaz:
do NOT open MT5 until he says so; when MT5 must be opened, turn the display off first.

## State found on 2026-10-12 04:50 UTC
- Laptop tunnel is up: server 127.0.0.1:18792 -> laptop engine, /health answers ok.
  Which account/mode that engine runs is unknown to the Mac session: check first.
- Server forward_to already lists 18787, 18788 and 18792. 18787/18788 are currently held by
  the MAC's tunnel (launchd com.tvbridge.aws-tunnel); the laptop cannot bind them until the
  Mac tunnel is stopped (ExitOnForwardFailure would kill the laptop tunnel).
- Mac engines FundingPips (port 8787, live) and Audacity (8788, live) are running and flat.
  Mac's Hantec engine is stopped; Mac's The5ers engine is paper and never ran.
- Mac config files to copy (contain Telegram token and webhook secret; never commit):
  ~/.tvbridge/config.json (FundingPips 20855526), ~/.tvbridge-audacity/config.json
  (Audacity 240878), ~/.tvbridge-the5ers/config.json (The5ers 26687677, mode paper, port 8792).
  The Mac session will scp them to the server at C:\tvbridge-setup\handover\ on request;
  the laptop pulls them from there over its own SSH, then the folder is deleted.
  calibration.json does NOT travel: recalibrate on Windows.

## Laptop ports and homes
| account | home | port | server port |
|---|---|---|---|
| FundingPips | C:\tvbridge-home-fp | 8787 | 18787 |
| Audacity | C:\tvbridge-home-aud | 8788 | 18788 |
| The5ers | C:\tvbridge-home-5ers | 8792 | 18792 |
One MT5 install per account, own folder, started with /portable, window at 0,0 1024x728
(TVBRIDGE_WIN_GEOMETRY). One engine task per account (engine-<name>.ps1 with its own
TVBRIDGE_HOME). Tunnel command (one task, retry loop):
ssh -i C:\keys\hantec-pilot.pem -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes
  -R 127.0.0.1:18787:127.0.0.1:8787 -R 127.0.0.1:18788:127.0.0.1:8788
  -R 127.0.0.1:18792:127.0.0.1:8792 Administrator@16.192.38.115
Add the 18787/18788 forwards ONLY after the Mac tunnel is stopped (step 5).

## Steps
1. Inventory the laptop: existing C:\tvbridge*, tasks tvb-*, running engine (which config,
   mode), MT5 installs, Python/Tesseract. Report before changing anything.
2. Install missing pieces per ops/WINDOWS-SETUP.md section 1. git pull C:\tvbridge.
3. Fetch the three config.json files (see above), place them in the homes, set
   server.forward_to = [] in all three (the server is the hub), keep ports as in the table.
4. Per account, when Riaz allows MT5: display off, start that MT5 (/portable), Riaz types
   the password once with "Save password", open one chart per traded symbol, Toolbox on
   Trade, One Click Trading off. Then `python -m tvbridge calibrate` and a rehearsal per
   traded symbol. Executor mode stays "rehearsal".
5. Cutover, one account at a time, coordinated with the Mac session:
   a. Mac session: `tvb pause` on that engine, quit that MT5 on the Mac (two terminals on
      one login kick each other off).
   b. Laptop: engine for that account in rehearsal -> status clean -> go live (Riaz decides).
   c. After FundingPips and Audacity are both moved: Mac session unloads
      com.tvbridge.aws-tunnel and stops both Mac engines; laptop tunnel task gets the
      18787/18788 forwards and restarts; verify on the server all three ports listen.
6. Harden: lid close = Do nothing (battery and AC), sleep Never, display off allowed,
   screensaver off, no sign-in required on wake, dynamic lock off, Autologon (Riaz types the
   password), tasks tvb-engine-*, tvb-mt5-*, tvb-tunnel, tvb-runner, tvb-watchdog with
   ExecutionTimeLimit PT0S (fixtasks.ps1). Test: close lid 2 min; ports still listening on
   the server and all heartbeats fresh.
7. Final: status.ps1-equivalent per engine (running, entries allowed, untracked none),
   Telegram test, one test alert from the server reaching all three laptop engines.
Caveats: The5ers had ~$323 room to its max-loss line on 2026-10-06: confirm balance before
live; keep risk.max_lots low until the first fill shows the margin.
