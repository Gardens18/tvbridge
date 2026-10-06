# tvbridge — build specification

TradingView alert → webhook on a Mac → risk guard → deterministic GUI clicker for MetaTrader 5 (Wine build on macOS).
Target account: Hantec Trader "Endurance" 3-step $50,000 (4% daily loss, 8% static max loss), MT5 server `HantecMarketsMU-MT5`, symbols suffixed `.h`.

**No LLM is in the execution path.** Everything here is deterministic Python run by the user.

## 0. Ground rules for every module

- **Python 3.9** (macOS system `/usr/bin/python3`). No `match`, no `X | Y` type unions, no `list[int]` generics at runtime — use `typing.Optional/List/Dict/Tuple`. `from __future__ import annotations` is allowed.
- Stdlib only, plus pyobjc (`Quartz`, `AppKit`, `Vision`, `ApplicationServices`, `Foundation`), which may only be imported inside `tvbridge/gui/driver.py` and `tvbridge/gui/ocr.py`, and lazily (inside functions or in a `try` at module import), so every other module and test imports without pyobjc.
- Tests use stdlib `unittest`. Run with: `.venv/bin/python -m unittest discover -s tests -t . -v` from the repo root. Tests must not need Accessibility/Screen Recording permission, a running MT5, or network access beyond 127.0.0.1.
- Datetimes are timezone-aware UTC everywhere internally. Get "now" only via `tvbridge.clock.utcnow()` so tests can freeze time.
- Logging: `logging.getLogger("tvbridge.<module>")`. Never log the webhook secret, ngrok token, or telegram token.
- **Fail closed for entries, fail open for exits.** Any uncertainty blocks new positions; closing positions is always allowed (even when paused/halted).
- Never retry an entry order automatically. An entry that may have reached the broker (uncertain after the click) halts new entries until a human runs `tvbridge resume`; an executor error before the click (nothing sent) only fails that signal.
- File ownership: only edit the files you are assigned. If another module's interface in this spec looks wrong, code against the spec and report the issue in your final answer.

## 1. Layout

```
tvbridge/                        repo root (current working directory)
  SPEC.md  README.md  requirements.txt  config.example.json
  install.sh  uninstall.sh
  launchd/com.tvbridge.engine.plist.template
  launchd/com.tvbridge.ngrok.plist.template
  tradingview/ALERTS.md
  tradingview/tvbridge_example_strategy.pine
  tvbridge/__init__.py            __version__ = "0.1.0"
  tvbridge/__main__.py            from .cli import main; raise SystemExit(main())
  tvbridge/models.py
  tvbridge/config.py
  tvbridge/clock.py
  tvbridge/store.py
  tvbridge/notify.py
  tvbridge/signals.py
  tvbridge/server.py
  tvbridge/risk.py
  tvbridge/engine.py
  tvbridge/cli.py
  tvbridge/executors/__init__.py  make_executor()
  tvbridge/executors/base.py
  tvbridge/executors/paper.py
  tvbridge/executors/mt5gui.py
  tvbridge/gui/__init__.py
  tvbridge/gui/driver.py          Window, OcrItem, Driver (abstract), MacDriver
  tvbridge/gui/ocr.py             Vision OCR helpers used by MacDriver
  tvbridge/gui/keys.py
  tvbridge/gui/parse.py           pure OCR-text parsers
  tvbridge/gui/calibration.py     Calibration load/save + interactive wizard
  tests/...
```

Runtime data lives in `TVBRIDGE_HOME` (env var; default `~/.tvbridge`): `config.json`, `calibration.json`, `tvbridge.db`, `heartbeat.json`, `shots/`, `bin/ngrok`, `ngrok.yml`. Logs go to `~/Library/Logs/tvbridge/`.

## 2. models.py (dataclasses, no logic beyond helpers)

```python
ACTIONS = ("buy", "sell", "close", "close_all", "sync")   # sync: mirror mode, section 15
SIDES = ("buy", "sell")

@dataclass
class Signal:
    id: str
    action: str                      # buy|sell|close|close_all
    tv_symbol: str                   # normalized TradingView symbol, e.g. "EURUSD"; "" for close_all
    symbol: str                      # MT5 symbol, e.g. "EURUSD.h"; "" for close_all
    side: Optional[str]              # close only: restrict to "buy"/"sell" positions; None = both
    price: Optional[float]           # alert's reference price ({{close}})
    sl: Optional[float]              # absolute price
    tp: Optional[float]              # absolute price
    risk_pct: Optional[float]        # optional per-signal override, still capped by config
    quote_usd: Optional[float]       # USD value of 1 unit of the quote currency (crosses)
    fired_at: datetime               # UTC, from alert {{timenow}}
    received_at: datetime            # UTC
    strategy: str = ""
    comment: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)   # payload minus secret
    target_side: Optional[str] = None    # (mirror) sync only: "buy"/"sell", None = flat
    target_units: Optional[float] = None # (mirror) sync only: strategy units; None when flat
    def to_dict(self) -> Dict[str, Any]       # JSON-safe (datetimes as ISO strings)
    @staticmethod
    def from_dict(d) -> "Signal"

@dataclass
class ObservedPosition:
    symbol: str; side: str; lots: float
    ticket: Optional[str] = None; open_price: Optional[float] = None
    sl: Optional[float] = None; tp: Optional[float] = None; profit: Optional[float] = None
    swap: Optional[float] = None          # (review) Swap column when the header shows one
    sl_missing: bool = False              # (review) S/L column read and shows 0 (no SL on the server)

@dataclass
class AccountSnapshot:
    ts: datetime; balance: float; equity: float
    margin: Optional[float] = None; free_margin: Optional[float] = None
    positions: Optional[List[ObservedPosition]] = None   # None = unknown/unparsed
    source: str = ""                                     # "paper" | "mt5gui"
    positions_note: str = ""              # (review) why positions is None, e.g. "TOOLBOX_INCOMPLETE: ..."

@dataclass
class OrderRequest:
    symbol: str; side: str; lots: float; sl: float; tp: Optional[float]
    digits: int; lot_decimals: int = 2; comment: str = ""; price_hint: Optional[float] = None
    quote_usd: Optional[float] = None    # (integration) signal's quote->USD rate; paper P/L for crosses
    sl_distance: Optional[float] = None  # (mirror) executor computes SL from its own quote: buy ask - d, sell bid + d
    tp_distance: Optional[float] = None  # (mirror) likewise: buy ask + d, sell bid - d

@dataclass
class OrderResult:
    status: str        # "filled" | "rejected" | "uncertain" | "rehearsed" | "error" | "no_position"
    message: str = ""
    fill_price: Optional[float] = None; ticket: Optional[str] = None; lots: Optional[float] = None
    evidence: List[str] = field(default_factory=list)     # screenshot paths
    sl: Optional[float] = None; tp: Optional[float] = None   # (mirror) open_market: the SL/TP actually used

@dataclass
class TradePlan:
    approved: bool
    reason: str = ""          # "" when approved, else "CODE: human text"
    lots: float = 0.0
    risk_usd: float = 0.0     # worst-case loss incl. commission and slippage buffer
    per_lot_loss_usd: float = 0.0
    close_first: List[ObservedPosition] = field(default_factory=list)  # opposite positions to close before entry (reversal)
    details: Dict[str, Any] = field(default_factory=dict)
```

## 3. config.py

JSON file. `load_config(path=None) -> Config` deep-merges the file over DEFAULTS, validates, and raises `ConfigError(msg)` with a precise message. `default_home() -> Path` returns `Path(os.environ.get("TVBRIDGE_HOME", "~/.tvbridge")).expanduser()`. If `path` is None, use `default_home()/"config.json"`. `config_from_dict(d, home) -> Config` for tests.

Dataclasses (field = default):

- `ServerCfg`: host="127.0.0.1", port=8787, path="/webhook", secret="" (must be ≥ 16 chars, else ConfigError), enforce_ip_allowlist=True, tradingview_ips=["52.89.214.238","34.212.75.30","54.218.53.128","52.32.178.7"], allow_local_requests=True, max_body_bytes=8192, max_signal_age_s=120, max_future_skew_s=30, rate_limit_per_min=30
- `AccountCfg`: name="Hantec Endurance 50k", initial_balance=50000.0, currency="USD", server_utc_offset_hours=3.0 (a number, or (integration) `"auto"` = UTC+3 while US DST is in effect, else UTC+2; never read the field directly, use `Config.server_offset_at`), account_login="" (if set, the MT5 main window title must contain it), server_name="" (if set, main window title must contain it)
- `RiskCfg`: daily_loss_pct=4.0, max_loss_pct=8.0, daily_buffer_pct=1.0, max_buffer_pct=1.0, kill_buffer_pct=0.3, risk_per_trade_pct=0.5, max_risk_per_trade_pct=1.0, max_total_open_risk_pct=2.0, max_open_positions=3, max_trades_per_day=8, max_lots=5.0, commission_per_lot_usd=5.0, slippage_buffer_pct=15.0, equity_max_age_s=90, entry_max_delay_s=45, reverse_on_opposite=True, allow_pyramiding=False, block_untracked_positions=True, trading_start_server="00:05", trading_end_server="23:50", trading_days_server=[0,1,2,3,4] (Mon=0, server-time weekday), friday_cutoff_server="22:00" (None disables), min_hold_s_for_signal_close=0
  - Validation: 0 < risk_per_trade_pct ≤ max_risk_per_trade_pct ≤ 3; 0 ≤ daily_buffer_pct < daily_loss_pct; 0 ≤ max_buffer_pct < max_loss_pct; max_open_positions ≥ 1.
- `SymbolSpec`: contract_size=100000.0, quote="USD", digits=5, point=0.00001, lot_step=0.01, min_lot=0.01, min_sl_points=50, (fan-out) commission_per_lot_usd=None (≥ 0 or None; None = `risk.commission_per_lot_usd`; read only through `Config.commission_per_lot(symbol)`, which every per-lot loss / commission computation uses: risk.plan_entry, untracked_risk_usd, engine booked risk / close proof, paper commission, the GUI equity-check allowance)
  - property `lot_decimals` = number of decimals in lot_step (0.01 → 2, 0.1 → 1, 1 → 0).
- `SymbolsCfg`: suffix=".h", map={} (normalized TV symbol → exact MT5 symbol), specs: Dict[str, SymbolSpec] keyed by normalized TV symbol, allowed: List[str]=[] (empty = every symbol in specs)
- `GuiCfg`: owner_names=["MetaTrader 5","terminal64","wine64-preloader","wine-preloader","wine"], main_title_contains="", order_dialog_title_contains=["Order"], position_dialog_title_contains=["Position","Order"], require_dialog_text=["Market"], dialog_timeout_s=4.0, result_timeout_s=8.0, action_delay_s=0.15, size_tolerance_px=12, ocr_min_confidence=0.3, account_poll_s=15, keep_screenshots_days=14, (mirror) volume_field_offset_px=85
- `ExecutorCfg`: mode="paper" ("paper" | "rehearsal" | "live"), paper_start_balance=None, gui: GuiCfg
- (mirror) `MirrorCfg` (section `mirror`): enabled=False, units_per_lot=100.0 (> 0), stop_distance=8.5 (> 0), tp_distance=0.0 (≥ 0; 0 = none), size_tolerance_lots=0.005 (≥ 0), max_price_gap_pct=0.5 (0 < x ≤ 100), allow_adds=False, idea_risk_pct=0.0 (0..3), (fan-out) fan_out: Dict[str, List[str]]={} (key normalized TV symbol; targets are config keys or MT5 names, stored as config keys; each needs a spec, ≥ 1, no duplicates, else ConfigError), units_per_lot_by_symbol: Dict[str, float]={} (keys stored as config keys, need a spec; values > 0)
- `NotifyCfg`: macos=True, ntfy_url="", telegram_bot_token="", telegram_chat_id="", min_level="info" (debug<info<warn<critical)
- `NgrokCfg`: authtoken="", domain="" (e.g. "your-name.ngrok-free.app"; no scheme)
- `Config`: home: Path, server, account, risk, symbols, executor, mirror, notify, ngrok
  - properties: db_path=home/"tvbridge.db", calibration_path=home/"calibration.json", heartbeat_path=home/"heartbeat.json", shots_dir=home/"shots", log_dir=Path("~/Library/Logs/tvbridge").expanduser()
  - `normalize_tv_symbol(s, suffix=".h") -> str` (module function): strip whitespace, take part after last ":" ("OANDA:EURUSD"→"EURUSD"), remove "/" and "_" , uppercase, strip a trailing `suffix` if present (case-insensitive, e.g. "EURUSD.H"→"EURUSD"). `Config.normalize_tv_symbol(s)` passes `symbols.suffix`; callers use the method.
  - `Config.tv_symbol_for(symbol) -> Optional[str]`: config key for a TV or MT5 symbol (exact `map` value match first, then normalized key in `specs`, then normalized `map` values).
  - Server time (integration): `Config.server_offset_at(dt_utc=None) -> float`, `Config.server_offset_for_day(day) -> float` (offset in force at that day's server midnight), `Config.server_date(dt_utc) -> date`, `Config.server_midnight_utc(day) -> datetime`, `Config.server_offset_is_auto`.
  - `Config.mt5_symbol(tv_symbol) -> str`: normalized; `map[norm]` if present else `norm + suffix`.
  - `Config.spec_for(symbol) -> Optional[SymbolSpec]`: accepts TV or MT5 form (normalize first; also reverse-lookup `map` values).
  - (fan-out) `Config.mirror_units_per_lot(symbol) -> float` (TV or MT5 form; `units_per_lot_by_symbol[key]` else `units_per_lot`), `Config.mirror_fan_out(symbol) -> List[str]` (target keys, [] = none), `Config.commission_per_lot(symbol) -> float`.
  - `Config.is_allowed(tv_symbol) -> bool`: normalized in `allowed` if non-empty, else in `specs`.
  - `Config.known_mt5_symbols() -> List[str]`: MT5 names for every spec.

DEFAULT specs (also written into `config.example.json`): EURUSD, GBPUSD, AUDUSD, NZDUSD (quote USD, digits 5, point 0.00001, min_sl_points 50); USDJPY (quote JPY, digits 3, point 0.001, min_sl_points 50); USDCAD (quote CAD), USDCHF (quote CHF) digits 5; XAUUSD (contract_size 100, quote USD, digits 2, point 0.01, min_sl_points 100). `config.example.json` must carry a `"_comment"` keys explaining that contract sizes must be checked against Hantec's product spec table. Unknown keys starting with "_" are ignored by validation; other unknown keys raise ConfigError (catches typos).

## 4. clock.py

```python
def utcnow() -> datetime                 # tz-aware; uses an overridable provider
def set_clock(fn: Optional[Callable[[], datetime]]) -> None   # tests: freeze; None restores real clock
def to_server(dt_utc, offset_hours) -> datetime
def server_date(dt_utc, offset_hours) -> date
def server_midnight_utc(server_day: date, offset_hours) -> datetime     # UTC instant of 00:00 server time on that day
def parse_hhmm(s: str) -> time
def parse_tv_time(v) -> datetime         # "2026-10-01T09:56:00Z", with fractional secs, "+00:00", "2026-10-01 09:56:00" (assume UTC), epoch seconds or ms (int/float/str digits). Raises ValueError.
def iso(dt) -> str                       # UTC ISO8601 with "Z"
def from_iso(s) -> datetime               # also accepts a datetime (returned as UTC)
def us_dst_active(dt_utc) -> bool        # (integration) US DST: 2nd Sun Mar 07:00 UTC .. 1st Sun Nov 06:00 UTC
def ny_close_offset_hours(dt_utc) -> float   # 3.0 during US DST else 2.0 (used by offset "auto")
```

## 5. store.py

SQLite via `sqlite3`, `check_same_thread=False`, WAL, `busy_timeout=5000`, one `threading.RLock` around every operation. Creates schema on open:

```sql
signals(id TEXT PRIMARY KEY, received_at TEXT, fired_at TEXT, action TEXT, symbol TEXT, payload TEXT, status TEXT, reason TEXT, result TEXT, updated_at TEXT)
positions(pid INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, symbol TEXT, side TEXT, lots REAL, entry_price REAL, sl REAL, tp REAL, risk_usd REAL, opened_at TEXT, status TEXT, closed_at TEXT, close_reason TEXT, ticket TEXT)
snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, balance REAL, equity REAL, margin REAL, free_margin REAL, n_positions INTEGER, source TEXT)
day_state(server_date TEXT PRIMARY KEY, ref_balance REAL, ref_equity REAL, reference REAL, source TEXT, created_at TEXT)
events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, kind TEXT, message TEXT, data TEXT)
kv(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)
```

```python
class Store:
    def __init__(self, path: Union[str, Path])     # ":memory:" allowed
    def close(self)
    # signals — status: queued|processing|done|rejected|failed|expired|rehearsed
    def insert_signal(self, sig: Signal, status="queued") -> bool    # False if id already exists
    def set_signal_status(self, sig_id, status, reason="", result: Optional[dict]=None)
    def get_signal_row(self, sig_id) -> Optional[dict]               # columns; payload/result parsed from JSON
    def load_signal(self, sig_id) -> Optional[Signal]
    def signals_with_status(self, statuses: Sequence[str]) -> List[dict]
    def recent_signals(self, limit=20) -> List[dict]
    # ledger
    def add_position(self, signal_id, symbol, side, lots, entry_price, sl, tp, risk_usd, opened_at, ticket=None) -> int
    def close_ledger_positions(self, symbol, side: Optional[str], reason, closed_at) -> int   # symbol None/"" = all symbols (case-insensitive match); side None = both
    def close_ledger_position(self, pid, reason, closed_at) -> None
    def open_ledger_positions(self) -> List[dict]       # status='open'
    def all_ledger_positions(self, limit=100) -> List[dict]   # newest first
    def count_trades_on_server_day(self, server_day: date, offset_hours) -> int   # positions opened within that server day
    def last_trade_at(self) -> Optional[datetime]
    # snapshots
    def add_snapshot(self, snap: AccountSnapshot) -> None
    def latest_snapshot(self) -> Optional[AccountSnapshot]       # positions=None
    def last_snapshot_before(self, ts_utc) -> Optional[AccountSnapshot]   # strictly before (rollover)
    def prune_snapshots(self, older_than_utc) -> int
    def snapshots_between(self, start_utc, end_utc) -> List[AccountSnapshot]
    def has_any_snapshot(self) -> bool
    # (review) helpers
    def exit_signals_received_after(self, sig_id) -> List[dict]   # close/close_all received after that signal (any status)
    def update_position_risk(self, pid, sl, risk_usd) -> None      # re-book an open row (SL widened in MT5)
    def snapshot_rows_between(self, start, end) / last_snapshot_row_before(ts) -> raw rows incl. n_positions
    def snapshot_from_row(self, row) -> AccountSnapshot
    # day state
    def get_day_state(self, server_day: date) -> Optional[dict]
    def set_day_state(self, server_day: date, ref_balance: float, ref_equity: float, source: str) -> None   # reference = max(both); upsert
    # events & kv
    def log_event(self, level, kind, message, data: Optional[dict]=None) -> None
    def recent_events(self, limit=50) -> List[dict]
    def get_kv(self, key, default=None) -> Optional[str]
    def set_kv(self, key, value: Optional[str]) -> None    # None deletes
    # (mirror) helpers; no schema change
    def newer_sync_signal(self, sig_id) -> Optional[dict]   # newest sync on the same symbol with a later fired_at (same instant: stored later), any status
    def reduce_ledger_position(self, pid, lots_closed) -> Optional[float]   # lots and risk_usd pro rata; remaining lots, None if not open
    def kv_with_prefix(self, prefix) -> Dict[str, str]
    def delete_kv_prefix(self, prefix) -> int
```

Timestamps are stored as fixed-width UTC strings `YYYY-MM-DDTHH:MM:SS.ffffffZ` (SQL order = time order); rows return them as strings, parse with `clock.from_iso`. `snapshots_between` is inclusive at both ends.

kv keys used by the system: `paused` ("1" or absent), `pause_reason`, `halted` (reason string or absent), `command` (JSON `{"cmd": "flatten"}` written by CLI, consumed by engine), `paper_state` (PaperExecutor JSON), `day` (current server date ISO, engine-owned), `resume_ack` (ISO time of the last `tvbridge resume`; the engine closes ledger rows it had flagged as not visible before that time, then deletes it), (mirror) `mirror_scale:<TV symbol>` (opened lots / requested lots of the mirrored position; absent = 1.0).

## 6. notify.py

```python
class Notifier:
    def __init__(self, cfg: NotifyCfg)
    def send(self, title: str, message: str, level: str = "info") -> None
```
Never raises; drops below `min_level`. Channels (each fire-and-forget in a daemon thread, 5 s timeout): macOS via `osascript -e 'display notification "<msg>" with title "<title>"'` (escape `\` and `"`; only if cfg.macos), ntfy (`POST ntfy_url`, headers `Title`, `Priority`: critical→5, warn→4, info→3), Telegram (`https://api.telegram.org/bot<token>/sendMessage`, JSON chat_id+text). `NullNotifier` subclass that records messages in `.sent` list (used by tests).

## 7. signals.py

TradingView payload (JSON object). Recommended shape (see tradingview/ALERTS.md):
```json
{"secret":"...", "time":"{{timenow}}", "symbol":"{{ticker}}", "price":{{close}},
 "action":"buy", "sl":1.08100, "tp":1.08900, "id":"optional-unique", "strategy":"name"}
```
or, for Pine strategies, `{"secret":"...","time":"{{timenow}}","symbol":"{{ticker}}","price":{{close}},"order":{{strategy.order.alert_message}}}` where `alert_message` is a JSON object string.

```python
class SignalError(Exception):
    def __init__(self, code: str, message: str)   # .code .message
def check_secret(d: dict, cfg: Config) -> bool    # d["secret"] or d["passphrase"]; hmac.compare_digest; False if missing
def parse_payload(d: dict, cfg: Config, received_at: datetime) -> Signal
def check_freshness(sig: Signal, now: datetime, cfg: Config) -> Optional[str]   # "STALE" if now-fired_at > max_signal_age_s; "FUTURE" if fired_at-now > max_future_skew_s; else None
def peek_action(d: dict) -> Optional[str]         # (review) best-guess action without raising ("partial_exit" for one), for rate limiting/notifications
def decode_body(body: bytes) -> dict              # UTF-8 (strip BOM) JSON object; SignalError("BAD_JSON")
```
parse_payload rules:
1. If `d["order"]` is a dict, or a string containing a JSON object, merge: `merged = {**d, **order}` (order keys win). Remove `secret`/`passphrase` from what is stored in `raw`.
2. action (lowercased, stripped): aliases long→buy, short→sell; exit/flat/close/close_position→close; closeall/close_all/flatten/flatten_all→close_all. If action missing and `position`/`market_position` == "flat" → close. If action is buy/sell and `market_position` == "flat" → close of the side that order closed (sell+flat → side "buy", buy+flat → side "sell"; an explicit `side` wins). (review) sell while `market_position` is "long", or buy while "short", is a partial exit → SignalError("BAD_ACTION", "PARTIAL_EXIT: ..."). Otherwise SignalError("BAD_ACTION").
3. symbol from `symbol` or `ticker`; required unless close_all → SignalError("NO_SYMBOL"). Normalize; `cfg.is_allowed` else SignalError("SYMBOL_NOT_ALLOWED") -- (review) except a close whose symbol still has a spec, or for which the optional `exit_symbol_ok(mt5_symbol)` callback (the server: an open ledger row) returns True. `symbol = cfg.mt5_symbol(...)`.
4. Floats via `_num(v)`: accepts int/float/numeric strings; "", "nan", "NaN", null, "0" for sl/tp → None (0 means "not set"). Keys: price|close; sl|stop|stop_loss; tp|take_profit|limit; risk_pct|risk; quote_usd. Non-numeric → SignalError("BAD_NUMBER").
5. side (close only): `side` in buy/sell/long/short → normalized; else None.
6. fired_at from time|timenow|fired → `clock.parse_tv_time`; missing/invalid → SignalError("NO_TIME").
7. id: if `id` present and non-empty → `str(id)[:128]` prefixed with the action, e.g. `"buy:" + id`. Else first 32 hex chars of sha256 over canonical JSON of [action, symbol, side, price, sl, tp, iso(fired_at), strategy].
8. strategy: str[:64]; comment: str[:24] default "tvb".
9. (mirror) action `sync`: see section 15. Codes `BAD_POSITION`, `BAD_SIZE`.

## 8. server.py

```python
class WebhookServer:
    def __init__(self, cfg: Config, store: Store, on_signal: Callable[[Signal], None], notifier=None)
    def start(self) -> None      # ThreadingHTTPServer on daemon thread; port 0 allowed; sets self.port
    def stop(self) -> None
    port: int
```
Behavior (respond within 1 s, never redirect, never echo the secret):
- `GET /health` → 200 `{"ok":true,"version":...}`. Other GETs/paths → 404.
- `POST <path>` (exact, or with one trailing "/"):
  1. Content-Length missing → 411; > max_body_bytes → 413.
  2. Client IP = last comma-separated value of `X-Forwarded-For` -- (review) honoured only when the socket peer is loopback (the ngrok agent); a non-loopback peer is judged by its own address -- else socket peer. If no XFF and peer is loopback → local request: 403 unless `allow_local_requests` (best effort). If XFF present and `enforce_ip_allowlist` and IP not in `tradingview_ips` → 403 + `store.log_event("warn","ip_blocked",...)`.
  3. (review) The body is read. `decode_body` → 400 `{"ok":false,"error":"BAD_JSON"}`; `check_secret` false → 401; log event; notify warn at most once per 10 minutes. These unauthenticated requests use their own sliding 60 s budget (`rate_limit_per_min`); when it is used up they get 429 with no event.
  4. (review) Authenticated entries (and unknown actions) count against the sliding 60 s window `rate_limit_per_min` → 429; authenticated close/close_all are never rate-limited. A bucket starting to refuse logs one event and one warn notification per window.
  5. `parse_payload` SignalError → 400 with code; log event `signal_rejected`.
  6. `check_freshness` → 400 with code; log event. (review) Exits fail open: FUTURE close/close_all accepted; STALE ones accepted up to `EXIT_MAX_AGE_S` (900 s) when no open ledger row on that symbol (and side; any symbol for close_all) was opened after `fired_at`. Every refused authenticated exit (STALE, FUTURE, SYMBOL_NOT_ALLOWED, NO_SYMBOL, BAD_JSON in `order`, PARTIAL_EXIT) sends a critical notification throttled per symbol/side (10 min).
  7. `store.insert_signal(sig)` False → (review) a TradingView retry (stored row has the same symbol and fired_at, or the id is a content hash) → 200 `{"ok":true,"duplicate":true}`; an explicit id reused by another firing: exits are re-keyed `<id>:<SYMBOL|*>:<iso fired_at>` and processed, an entry on another symbol re-keyed `<id>:<SYMBOL>`, an entry on the same symbol → 400 `ID_REUSED` (warn event + notification). sqlite exception → 503 (TradingView retries 5xx).
  8. `on_signal(sig)` (exceptions logged, still 200) → 200 `{"ok":true,"id":sig.id}`.
- Override `log_message` to route to logging at debug level.

## 9. risk.py (pure, no I/O)

```python
@dataclass
class Floors:
    reference: float            # max(prev EOD balance, prev EOD equity) at 00:00 server time
    hard_daily_floor: float     # reference * (1 - daily_loss_pct/100)
    hard_max_floor: float       # initial_balance * (1 - max_loss_pct/100)   (static)
    internal_daily_floor: float # reference * (1 - (daily_loss_pct - daily_buffer_pct)/100)
    internal_max_floor: float   # initial_balance * (1 - (max_loss_pct - max_buffer_pct)/100)
    entry_floor: float          # max(internal_daily_floor, internal_max_floor)
    kill_floor: float           # max(hard_daily_floor, hard_max_floor) + initial_balance * kill_buffer_pct/100
def compute_floors(reference: float, cfg: Config) -> Floors

def quote_to_usd(tv_symbol: str, spec: SymbolSpec, price: float, signal_quote_usd: Optional[float]) -> Optional[float]
    # quote USD → 1.0; tv_symbol starts with "USD" and spec.quote == tv_symbol[3:6] → 1/price; signal_quote_usd>0 → it; else None
def per_lot_loss_usd(entry: float, sl: float, spec: SymbolSpec, q2usd: float, commission_per_lot: float) -> float
    # abs(entry - sl) * contract_size * q2usd + commission_per_lot
def round_lots_down(lots: float, step: float, min_lot: float) -> float   # Decimal floor to step; returns 0.0 if < min_lot

@dataclass
class RiskState:
    now: datetime
    snapshot: Optional[AccountSnapshot]
    floors: Optional[Floors]
    open_positions: List[dict]                 # ledger rows (symbol, side, lots, risk_usd, ...)
    observed_positions: Optional[List[ObservedPosition]]   # latest parsed from MT5, None = unknown
    untracked_positions: List[ObservedPosition]            # observed on MT5 but not in ledger
    trades_today: int
    paused: bool
    halted: str                                # "" when not halted
    # (review) engine-supplied doubts, all optional
    positions_uncertain: str = ""              # ledger rows MT5 does not show (no close proven) / incomplete list
    sl_issues: List[str] = []                  # open positions MT5 shows without a stop-loss
    reference_note: str = ""                   # today's reference is a "stale_estimate"
    confirmed_balance: Optional[float] = None  # min(balance of the last two reads)
    confirmed_equity: Optional[float] = None   # min(equity of the last two reads)

def untracked_risk_usd(p: ObservedPosition, cfg) -> Optional[float]   # (review) lots x per_lot_loss from open price/SL; None if unknown

def check_trading_window(now_utc: datetime, cfg: Config) -> Optional[str]   # None if OK else "WINDOW: ..."; server time via cfg.server_offset_at(now_utc); window [start, end), start > end = overnight, start == end = closed; Friday cutoff blocks from the cutoff on
def plan_entry(sig: Signal, state: RiskState, cfg: Config) -> TradePlan
def evaluate_kill(snapshot: AccountSnapshot, floors: Floors) -> Optional[str]   # "KILL: ..." if equity <= kill_floor
def estimate_reference(last_before_midnight: Optional[AccountSnapshot], today_snaps: List[AccountSnapshot],
                       initial_balance: float, have_history: bool) -> Optional[float]
    # candidates: balance & equity of last_before_midnight; balance & equity of every today_snap;
    # initial_balance if not have_history. max(candidates) or None. (Conservative: higher reference = tighter floor.)
```
`plan_entry` checks, in this order, returning `TradePlan(False, "CODE: text")` at the first failure:
1. `HALTED` if state.halted; `PAUSED` if state.paused.
2. `BAD_ACTION` unless sig.action in buy/sell.
3. `NO_SPEC` if `cfg.spec_for(sig.symbol)` is None.
4. trading window (`check_trading_window`) → `WINDOW`.
5. `STALE_SIGNAL` if (now − fired_at) > entry_max_delay_s.
6. `NO_SNAPSHOT` if snapshot None; `SNAPSHOT_STALE` if now − snapshot.ts > equity_max_age_s.
7. `NO_DAY_REFERENCE` if floors None, or (review) if `reference_note` is set (stale estimate: set-reference needed).
8. `UNTRACKED_POSITIONS` if block_untracked_positions and untracked_positions non-empty.
   (review) 8b. `POSITIONS_UNCERTAIN` if `positions_uncertain`. 8c. `SL_MISSING_ON_SERVER` if `sl_issues`.
9. `NO_PRICE` if sig.price None or ≤ 0; `SL_MISSING`; `SL_WRONG_SIDE` (buy needs sl < price, sell needs sl > price); `TP_WRONG_SIDE` (if tp: buy needs tp > price, sell needs tp < price); `SL_TOO_TIGHT` if abs(price − sl) < min_sl_points × point.
10. Reversal: positions on the same symbol and opposite side (from observed_positions when known, else ledger). If any and `reverse_on_opposite` → `close_first` = those, exclude them from counts below. If any and not reverse_on_opposite → `OPPOSITE_OPEN`.
11. `PYRAMIDING` if a same-side position on that symbol is open and not allow_pyramiding.
12. `MAX_POSITIONS` if remaining open count ≥ max_open_positions; `MAX_TRADES_DAY` if trades_today ≥ max_trades_per_day.
13. `NO_FX_RATE` if quote_to_usd returns None.
14. Sizing: base = min(balance, equity); risk_pct = min(sig.risk_pct or risk_per_trade_pct, max_risk_per_trade_pct); target = base × risk_pct/100; per_lot = per_lot_loss_usd(price, sl, …); lots = round_lots_down(min(target / (per_lot × (1 + slippage_buffer_pct/100)), max_lots), lot_step, min_lot); `SIZE_TOO_SMALL` if lots == 0; risk_usd = lots × per_lot × (1 + slippage_buffer_pct/100).
15. `TOTAL_OPEN_RISK` if Σ ledger risk_usd (all open, including close_first ones) + risk_usd > base × max_total_open_risk_pct/100. (review) With `block_untracked_positions` false, each untracked position's `untracked_risk_usd` is added to the open risk; an unknown one (no readable SL, no spec, no FX rate) → `TOTAL_OPEN_RISK`.
16. `BELOW_ENTRY_FLOOR` if equity ≤ entry_floor. (review) equity = min(equity, confirmed_equity).
17. `WORST_CASE` if balance − Σ open risk − risk_usd < entry_floor, or equity − risk_usd < entry_floor. (review) balance/equity = min with confirmed_balance/confirmed_equity.
18. Approved. `details` holds floors, base, target, per_lot, raw lots, worst-case values.

`plan_entry` never raises: an internal error becomes `TradePlan(False, "RISK_ERROR: ...")`.

## 10. executors

### base.py
```python
class ExecutorError(Exception):
    def __init__(self, code: str, message: str = "")   # .code .message
class Executor:
    name = "base"
    def read_account(self) -> AccountSnapshot
    def open_market(self, req: OrderRequest) -> OrderResult
    def close_positions(self, symbol: str, side: Optional[str] = None) -> List[OrderResult]  # [] when nothing matched
    def close_partial(self, symbol: str, side: str, lots: float) -> List[OrderResult]   # (mirror) section 15
    def close_all(self) -> List[OrderResult]
    def health(self) -> Dict[str, Any]     # {"ok": bool, "detail": str}
    def set_price_hint(self, symbol: str, price: float) -> None   # default no-op
```

### paper.py — `PaperExecutor(cfg, store)`
Simulated account persisted in `store` kv `paper_state` (JSON). Balance starts at `paper_start_balance or initial_balance`. `set_price_hint` updates last price per symbol and triggers SL/TP hits (buy: price ≤ sl or ≥ tp; sell: price ≥ sl or ≤ tp) which realize P/L at the SL/TP price. Fill at `req.price_hint` (error result if None). For a cross whose quote->USD rate cannot be derived from hinted prices, `req.quote_usd` is used. P/L = (exit − entry) × contract_size × lots × q2usd (sign by side) − commission_per_lot × lots (charged on close). Equity = balance + floating at last hints. Tickets "P1", "P2"… `read_account` returns positions list (never None). `close_positions` returns one `filled` result per closed position.

### mt5gui.py — `Mt5GuiExecutor(cfg, driver: Driver, calib: Calibration, rehearsal: bool, shots_dir: Path)`
All GUI access is through the `Driver` interface (§11) so the class is fully testable with a fake driver.

Helpers:
- `_main_window()` → (review) `select_main_window(windows, cfg, calib)`: candidates are windows with w ≥ 600 and h ≥ 400 (and title containing `main_title_contains` if set); none → `ExecutorError("MT5_NOT_FOUND")`. Candidates whose title lacks `account_login` (matched as a whole number, `login_in_title`) or `server_name` (case-insensitive) are dropped first; none left → `ExecutorError("WRONG_ACCOUNT")`. One left → it. Several → the one within `size_tolerance_px` of `calib.main_window`, else `ExecutorError("AMBIGUOUS_MAIN_WINDOW")`.
- (review) `_main_size_issue(main)` → "" or why the main window differs from `calib.main_window` w/h by more than `size_tolerance_px`.
- (review) `_toolbox_issue(main, items, acct, rows)` → "" when the parsed position list is trusted as complete, else the reason: (a) main size issue; (b) no Trade-list header (`parse.find_trade_header`: a row with "Symbol" … "Profit") above the rows and the account line; (c) a gap between header, rows and account line wider than 1.75 × the row pitch (or, without rows, 2.5 text heights); (d) no rows but margin > 0; (e) every row has a profit and |equity − balance − Σ(profit + swap)| > max(2, 2% of |equity − balance|) + (commission_per_lot_usd + 5 USD when no Swap column) × Σ lots.
- `_other_windows(main)` → windows with the same pid, excluding main.
- `_dismiss_stray_dialogs(main)` → any other window: activate, press Escape (up to 2 tries each); if any remain → `ExecutorError("STRAY_DIALOG")`.
- `_focus(main)` → `driver.activate(pid)`; click `main.x+focus_point[0], main.y+focus_point[1]`; sleep action_delay.
- `_wait_for_new_window(pid, before_ids, title_needles, timeout)` → poll every 0.1 s.
- `_ocr(win, region=None, tag)` → capture to `shots_dir/YYYYMMDD/HHMMSS_mmm_<tag>.png`, OCR, drop items below ocr_min_confidence, return (items, png_path).
- `_set_field(x, y, text)` → click, key "end", key "home" with ("shift",), type_text(text), key "tab", sleep.

`open_market(req)`:
1. main; (review) main size differs from calibration → `ExecutorError("MAIN_LAYOUT_CHANGED")` before any input; dismiss strays, focus. Remember window ids. `driver.key("f9")`. Wait for new window whose title contains any `order_dialog_title_contains` → else `ExecutorError("ORDER_DIALOG_NOT_OPENED")`.
2. Size check vs `calib.order_dialog["w"/"h"]` within size_tolerance_px → else Escape, `ExecutorError("DIALOG_LAYOUT_CHANGED")`.
3. Fill symbol (then sleep 0.5 s), volume (`f"{lots:.{lot_decimals}f}"`), sl (`f"{sl:.{digits}f}"`), tp (formatted or "0").
4. OCR the dialog; `parse.verify_dialog_fields(items, symbol, lots_str, sl_str, tp_str_or_None, require_text)` → on failure: Escape, return `OrderResult("error", "VERIFY_FAILED: ...", evidence=[png])`.
5. Rehearsal → Escape, wait closed, return `OrderResult("rehearsed", ...)`.
6. Button guard: target point = dialog origin + calib point for "buy"/"sell". `parse.nearest_label(items, (x,y), ["buy","sell"], max_dist=80)` must equal the wanted side → else Escape, `OrderResult("error","BUTTON_LABEL_MISMATCH")`.
7. (review) Right before the click, `executor.abort_check()` (set by the engine) may return a reason → Escape, `OrderResult("error", "ABORTED: ...")`. A flag is set immediately before the single click. Poll OCR of the dialog every 0.5 s up to result_timeout_s: `parse.parse_order_result(" ".join(texts))` → "filled"/"rejected"/"uncertain" stops polling. If the dialog vanished → "uncertain". (review) A "filled" result whose text names the other side, another volume or another known symbol, or whose price is further than max(1% of `price_hint`, 100 × min_sl_points × point) from it → "uncertain" `RESULT_MISMATCH`.
8. Close the dialog (Escape; Return as fallback), confirm gone.
9. Return filled (with ticket/price) / rejected (message) / uncertain. Every return carries screenshot evidence paths.
Any exception at or after the click must become `OrderResult("uncertain", ...)`, never a raised error. (review) Before the click, unexpected exceptions are raised as `ExecutorError("GUI_ERROR", "...; nothing was sent")`.

`read_account()`: main (no focus needed); OCR `calib.toolbox_region` of main; `parse.parse_account_line` (requires balance and equity). If None and `calib.trade_tab_point` (and the main window has its calibrated size): focus, click the trade tab, retry once. Still None → `ExecutorError("ACCOUNT_UNREADABLE")` (review: the first screenshot of a failure streak and `account_failed_latest.png` are kept, the other failure captures deleted). Positions = `parse.parse_position_rows(items, cfg.known_mt5_symbols())` (list) -- (review) or None with `positions_note="TOOLBOX_INCOMPLETE: ..."` when `_toolbox_issue` reports one (balance/equity are always returned). Return snapshot source "mt5gui".

`close_positions(symbol, side)`: up to 10 iterations: OCR toolbox → rows for symbol (same config key, so generic rows match too) (and side) → none: stop. (review) Strays: Escape, but never raise: a remaining stray covering the row anchor → error result for that row; one covering the focus point → no focus click. Double-click the row anchor, wait for a dialog (title needles `position_dialog_title_contains`), OCR it, find the button: an item containing "close" AND the symbol (case-insensitive) → missing: Escape, error result, stop. Rehearsal → Escape, append "rehearsed", stop. Click it, poll result like step 7, close dialog, append result; stop on rejected/uncertain. If the first OCR finds no matching rows → return `[]`. (review) When the last scan's list is incomplete, append `OrderResult("uncertain", "TOOLBOX_INCOMPLETE: ...")`; `_ticket_listed` returns None for an incomplete list.

`close_all()`: rows from toolbox → for each distinct symbol (including symbols without a spec) → close its rows; (review) if the final list is incomplete, append one `TOOLBOX_INCOMPLETE` "uncertain" result (never `[]` while rows may be hidden).

`health()`: main window found + driver permissions.

### executors/__init__.py
```python
def make_executor(cfg, store, driver=None) -> Executor
# paper → PaperExecutor; rehearsal/live → Mt5GuiExecutor(cfg, driver or MacDriver(), load_calibration(cfg.calibration_path), rehearsal=(mode=="rehearsal"), shots_dir=cfg.shots_dir)
# (review) live with an empty account.account_login → ExecutorError("ACCOUNT_LOGIN_REQUIRED")
```

## 11. gui

### driver.py
```python
@dataclass
class Window:
    wid: int; pid: int; owner: str; title: str; x: float; y: float; w: float; h: float
@dataclass
class OcrItem:
    text: str; conf: float; x: float; y: float; w: float; h: float     # global screen points, top-left origin
    cx (property), cy (property)
class Driver:            # abstract; all methods raise NotImplementedError
    def list_windows(self, owner_names: List[str]) -> List[Window]   # on-screen, layer 0, owner name contains any needle (case-insensitive)
    def activate(self, pid: int) -> None
    def capture(self, win: Window, out_path: str) -> float            # writes PNG of that window only, returns scale (pixels per point)
    def ocr(self, png_path: str, win: Window, scale: float, region: Optional[Tuple[float,float,float,float]] = None) -> List[OcrItem]
        # region = (dx, dy, w, h) in window-relative points; returned coords are global screen points
    def click(self, x: float, y: float, count: int = 1) -> None
    def key(self, name: str, mods: Tuple[str, ...] = ()) -> None      # names from keys.KEYCODES; mods: shift, ctrl, alt, cmd
    def type_text(self, text: str) -> None
    def mouse_location(self) -> Tuple[float, float]
    def sleep(self, s: float) -> None
    def permissions(self) -> Dict[str, bool]                          # {"accessibility": bool, "screen_recording": bool}
    def request_permissions(self) -> None
class MacDriver(Driver)
```
MacDriver: `CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID)`; activation via `NSRunningApplication.runningApplicationWithProcessIdentifier_(pid).activateWithOptions_(NSApplicationActivateIgnoringOtherApps)`; capture via `/usr/sbin/screencapture -x -o -l <wid> <path>` then scale = image pixel width / win.w; mouse via `CGEventCreateMouseEvent` + `CGEventPost(kCGHIDEventTap, …)` (mouse-move first, then down/up; for count=2 set `kCGMouseEventClickState` 1 then 2); keys via `CGEventCreateKeyboardEvent` with keycodes from keys.py and flags; `type_text` uses `keys.char_to_key` per character (never unicode-string injection; Wine maps by keycode). Permissions: `ApplicationServices.AXIsProcessTrusted()`, `Quartz.CGPreflightScreenCaptureAccess()`; request: `AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})`, `CGRequestScreenCaptureAccess()`.

### ocr.py
`recognize(png_path, region_px=None) -> List[Tuple[str, float, Tuple[float,float,float,float]]]` — Vision `VNRecognizeTextRequest`, accurate level, language correction off; optional crop with `CGImageCreateWithImageInRect`; returns (text, confidence, (x_px, y_px, w_px, h_px)) in top-left pixel coords of the (cropped) image. Vision may return overlapping observations; keep them all.

### keys.py
`KEYCODES: Dict[str, int]` (US ANSI layout): a–z, 0–9, "period" 47, "comma" 43, "minus" 27, "equal" 24, "slash" 44, "semicolon" 41, "space" 49, "return" 36, "tab" 48, "escape" 53, "delete" 51, "forwarddelete" 117, "home" 115, "end" 119, "pageup" 116, "pagedown" 121, "left" 123, "right" 124, "down" 125, "up" 126, f1–f12 = 122,120,99,118,96,97,98,100,101,109,103,111. `MODIFIER_FLAGS` for shift/ctrl/alt/cmd (Quartz flag values as ints: shift 0x20000, ctrl 0x40000, alt 0x80000, cmd 0x100000). `char_to_key(ch) -> Tuple[str, Tuple[str, ...]]` for a-z, A-Z (shift), 0-9, ".", ",", "-", "_" (shift+minus), "#" (shift+3), ":" (shift+semicolon), "/", " ". Unsupported → ValueError.

### parse.py (pure; operates on `OcrItem` lists)
```python
def parse_number(s: str) -> Optional[float]       # "50 000.00", "50,000.00", "50 000,00"? (no: MT5 uses '.' decimals), "-17.00", "−17.00", nbsp/thin spaces, trailing "%"/"USD"
def parse_account_line(items: List[OcrItem]) -> Optional[Dict[str, float]]   # keys balance, equity, margin?, free_margin?; scan all item texts (and their concatenation in reading order) for "Balance:", "Equity:", "Margin:", "Free Margin:"/"Free margin:"; None unless balance and equity found
def group_rows(items: List[OcrItem], y_tol: float = 6.0) -> List[List[OcrItem]]   # cluster by cy, each row sorted by x; dedupe overlapping duplicates (same text, centers within 3 pt)
def parse_position_rows(items: List[OcrItem], known_symbols: List[str]) -> List[Tuple[ObservedPosition, OcrItem]]
    # a row is a position if its joined text contains a known symbol (case-insensitive, word-bounded) AND a standalone "buy"/"sell" AND a lot-size number after the side; anchor = the item containing the symbol. Ignore the account line.
    # (review, D3) rows without a known symbol: the row's first word must look like a symbol and a ticket (4+ digits) must sit between it and the side; the symbol is kept as read. Known symbols win.
    # (review) swap = number before Profit when the header shows a Swap column; sl_missing when all four price columns parse and S/L is 0.
def find_trade_header(items) -> Optional[dict]   # (review) {"cy","text","swap","h"} of the "Symbol ... Profit" row
def account_line_y(items) -> Optional[float]     # (review) centre y of the Balance/Equity row
def parse_order_result(text: str) -> Tuple[str, str, Optional[str], Optional[float]]
    # (status "uncertain"|"rejected"|"filled"|"unknown", message, ticket from r"#\s?(\d{4,})", price from r"\bat\s+(\d+(?:\.\d+)?)")
    # (review) uncertain first (word-bounded): timeout/time out/timed out, no connection, connection, error, failed -- the order may have executed
    # rejected: invalid, rejected, not enough, no money, market (is) closed, disabled, requote, off quotes, no prices, too many/too frequent
    # filled if any of: done, executed, placed, filled
def result_side_volume(text) -> Optional[Tuple[str, float]]   # (review) ("buy", 0.5) from "buy 0.50 EURUSD.h ..."
def parse_ticket_quote(items) -> Optional[Tuple[float, float]]   # (mirror) (bid, ask) from the order ticket's quote line, section 15
def find_items(items, needle: str) -> List[OcrItem]      # case-insensitive substring
def nearest_label(items, point: Tuple[float,float], labels: List[str], max_dist: float) -> Optional[str]
    # label whose matching item center is closest to point (word-bounded, case-insensitive), None if none within max_dist
def verify_dialog_fields(items, symbol: str, lots_str: str, sl_str: str, tp_str: Optional[str], require_text: List[str]) -> Tuple[bool, List[str]]
    # all texts joined; symbol present (case-insensitive); require_text all present; returns (ok, problems)
    # (review) each of Volume / Stop Loss / Take Profit needs its label read by OCR and a value paired with it: in the
    # label's own observation, or the nearest observation starting with a number, beginning right of the label, with its
    # vertical centre within 0.75 x the taller item's height of the label's; equal within 1e-9 ("0.50" matches "0.5");
    # TP must read 0 when tp_str is None. A number elsewhere in the window never counts.
```

### calibration.py
```python
class CalibrationError(Exception)
@dataclass
class Calibration:
    version: int = 1
    created_at: str = ""
    main_window: Dict[str, Any]          # {"title": str, "w": float, "h": float}
    order_dialog: Dict[str, Any]         # {"title": str, "w": float, "h": float, "points": {"symbol":[dx,dy],"volume":[..],"sl":[..],"tp":[..],"buy":[..],"sell":[..]}}
    toolbox_region: List[float]          # [dx, dy, w, h] relative to main window
    focus_point: List[float]             # [dx, dy] relative to main window: safe spot (e.g. title bar)
    trade_tab_point: Optional[List[float]] = None
def load_calibration(path) -> Calibration     # CalibrationError("missing… run `tvbridge calibrate`") / invalid
def save_calibration(path, calib) -> None     # chmod 600
def run_wizard(driver: Driver, cfg: Config, path, input_fn=input, print_fn=print) -> Calibration
```
Wizard (interactive, never clicks anything in MT5; the human only hovers):
1. Warn: use a DEMO account; do not click Buy/Sell during calibration.
2. Check permissions; find MT5 windows; pick the main window (review: like the executor, account filter first; several candidates → CalibrationError).
3. Ask the user to press F9 in MT5 to open New Order, then press Enter here; detect the new dialog window (same pid, not main); record its title/size.
4. For each point (symbol field, volume field, stop-loss field, take-profit field, Sell button, Buy button): "press Enter, then within 3 s hover the mouse over <X>" → countdown → `driver.mouse_location()` → store relative to dialog origin.
5. Ask the user to close the dialog themselves (Escape in MT5), then capture: focus point (title bar), toolbox top-left, toolbox bottom-right, trade-tab label (optional, Enter to skip) relative to main window.
6. Verify: OCR toolbox → print parsed balance/equity or a clear failure; save.
(review) The main window is picked like the executor does (account filter first; several candidates → CalibrationError). After the button check, the Volume / Stop Loss / Take Profit points must each sit right of, and level with, their own label, nearer to it than to any other field label → else CalibrationError (swapped or misplaced points).

## 12. engine.py

```python
class Engine:
    def __init__(self, cfg, store, executor, notifier, start_server: bool = True)
    def start(self) -> None          # starts executor thread, scheduler thread, WebhookServer (if start_server)
    def stop(self, timeout=10) -> None
    def submit_signal(self, sig: Signal) -> None     # server callback; enqueues task
    def status(self) -> Dict[str, Any]
    server: Optional[WebhookServer]
```
- One executor thread owns the Executor; tasks come from a `PriorityQueue` of `(priority, seq, Task)`; priorities FLATTEN=0, CLOSE=1, (mirror) SYNC=1, OPEN=2, ACCOUNT=3. `Task(kind, signal: Optional[Signal], not_before: Optional[datetime], reason: str)`.
- Scheduler thread (1 s tick): enqueue ACCOUNT every `gui.account_poll_s` (no duplicate pending); consume kv `command` (`flatten` → FLATTEN task); server-day rollover; heartbeat JSON every 10 s; daily screenshot cleanup.
- OPEN: status processing → refresh account if latest snapshot older than 5 s → (review, D1) with `reverse_on_opposite`, opposite positions on the symbol (latest read or ledger) are closed first via `executor.close_positions(symbol, opposite_side)` -- as a close, even when paused/halted and whatever the entry's fate (stale, no SL, outside the window ...), but only if the signal fired at most `server.EXIT_MAX_AGE_S` (900 s) ago, otherwise warn `reversal_too_late` and close nothing; any non-filled → status rejected "REVERSAL_CLOSE_FAILED", notify critical; then the account is re-read → (review) a close (same symbol, side None or the entry's side) or close_all received after this entry → status expired "SUPERSEDED_BY_CLOSE" → build RiskState (floors from today's day_state; untracked from reconciliation; positions_uncertain, sl_issues, reference_note, confirmed balance/equity) → `risk.plan_entry`. Rejected → status "rejected" + notify info. Approved with close_first still non-empty (not a rehearsal) → REVERSAL_CLOSE_FAILED. (review) Right before the order: age re-checked (STALE_SIGNAL), kv halted/paused/command flatten or a pending FLATTEN → rejected HALTED/PAUSED/FLATTEN_PENDING → `OrderRequest` → executor. filled → `store.add_position` (entry = fill_price or sig.price; (review) risk_usd = max(plan risk (scaled to the size), lots × per_lot_loss_usd(fill, SL)); worse than planned → warn, critical if the worst case reaches the kill floor), status done, notify info. rehearsed → status "rehearsed". rejected → status failed, notify warn. (review, D2) error (incl. an ExecutorError, i.e. before the click: nothing sent) → status failed, notify warn (critical from the 3rd in a row), no halt; "ABORTED: ..." → status rejected, info. uncertain/RESULT_MISMATCH/any other exception → status failed, set kv `halted`="UNCERTAIN_EXECUTION …", notify critical, poll account and reconcile; a position on that symbol/side appearing within 10 minutes is adopted into the ledger (risk from its open price and size, never below plan.risk_usd).
- CLOSE: allowed when paused/halted. Respect `min_hold_s_for_signal_close` by re-queuing with not_before -- (review) counting only ledger rows opened at or before the close's `fired_at`; if every matching row is newer → done "NO_POSITION: ... opened after this close alert" without touching MT5. Reversal closes ignore min hold. `executor.close_positions(symbol, side)`; filled → `store.close_ledger_positions`; `[]` → status done "NO_POSITION"; failures → notify critical.
- CLOSE_ALL / FLATTEN: `executor.close_all()`, close ledger rows, notify. (review) `[]` while the ledger has open rows or the last read shows margin > 0 → failure `CLOSE_FAILED: NO_ROWS_VISIBLE: ...` (ledger kept, critical); identical FLATTEN FAILED notifications at most every 5 minutes.
- ACCOUNT poll: `read_account` → `store.add_snapshot` → reconcile (observed not in ledger → untracked, notify warn once per symbol) → `evaluate_kill` with today's floors → set halted + enqueue FLATTEN + notify critical. 4 consecutive read failures → notify warn (entries are blocked anyway by SNAPSHOT_STALE). (review) Database/bookkeeping failures never stop the kill switch: snapshot saving, reconciliation and rollover are each wrapped (critical notification at most every 10 minutes) and the kill check always runs on the in-memory snapshot; the flatten is queued before kv halted is written (a failed write is remembered in memory). 4 reads in a row with `positions_note` → warn.
- (review) Reconciliation: a ledger row MT5 does not show is closed ("closed_on_server") only after 2 reads without it AND proof: the balance changed by more than 0.005 since the read that last showed it (or, for a row never seen since its fill/the start, since the account before it, by more than its entry commission), or margin fell by more than 5%. Without proof it stays open (risk booked) and is reported once (warn); entries are refused `POSITIONS_UNCERTAIN` until it shows again, the balance proves the close, or kv `resume_ack` (written by `tvbridge resume`) confirms it closed. An empty list with margin > 0 counts no misses. A matched row whose observed S/L is 0 → `SL_MISSING_ON_SERVER` (critical, entries refused until it has one); an observed S/L further from entry than the booked one → risk_usd scaled up and SL updated (critical). An equal ticket on another symbol is never paired (critical `TICKET/SYMBOL MISMATCH`).
- Rollover: when `server_date(now)` ≠ kv `day`: reference via `risk.estimate_reference(last snapshot before today's server midnight, today's snapshots, initial_balance, have_history)` → `store.set_day_state` (source "rollover"/"startup"), set kv day, notify info with floors. If no data at all yet, retry each tick. (review) A last pre-midnight snapshot that differs by more than 3% from the one before it (within 10 min) is replaced by that one (warn). With no snapshot since midnight and a basis missing or older than max(4 × account_poll_s, equity_max_age_s, 120 s), the rollover waits for a fresh read; the result is then source "stale_estimate" (critical notification; entries refused NO_DAY_REFERENCE until set-reference) unless both the basis and the first read show 0 positions and the same balance. In the first 3 poll intervals after midnight a non-manual reference is raised (never lowered) by new reads. A reference more than 5% from the previous day's is flagged.
- Startup recovery: signals stuck in "processing" → "failed" ("INTERRUPTED: verify MT5 manually") + notify critical + set halted; "queued" entries → re-submitted (risk will expire stale ones); "queued" closes → re-submitted.
- `status()` → mode, paused/halted, latest snapshot (+age s), floors, open ledger positions, untracked, queue size, trades today.

Integration decisions (as implemented in engine.py):
- Task kinds: FLATTEN (kill switch / CLI command, no signal) and CLOSE_ALL (a close_all signal) both have priority 0. A task with `not_before` in the future waits in a side list. The engine never processes a signal whose stored status is no longer "queued" (no double execution); a signal submitted without a store row is inserted first.
- Every signal's price is passed to `executor.set_price_hint` (only if the alert is younger than `max_signal_age_s`). An entry re-reads the account first when the last read is older than 5 s, or positions/paper prices changed since it; RiskState.observed_positions comes from the last in-memory `read_account()` (None when stale), never from the store.
- Halt strings: `UNCERTAIN_EXECUTION: signal <id> (<side> <lots> <symbol>) ended <status>: <message>` for uncertain results and non-ExecutorError exceptions (review, D2: an ExecutorError becomes an "error" result, which does not halt); a broker rejection does not halt. `INTERRUPTED: ...` at startup recovery; `KILL: ...` from the kill switch. Adoption books `plan.risk_usd` (scaled up if the adopted position is larger).
- Risk rejection with STALE_SIGNAL → status "expired"; other rejections → "rejected" (RISK_ERROR notifies at warn).
- Close outcomes: all filled → close ledger rows for symbol/side; partial → close only rows whose ticket was reported filled, status failed `CLOSE_FAILED: ...` + notify critical; executor exception → failed `CLOSE_FAILED: <code>`; `[]` → done `NO_POSITION: ...` (ledger left to reconciliation); all "rehearsed" → status rehearsed. A deferred close (min hold) stays "queued" with reason `DEFERRED: ...`. Reversal: `[]` from close_positions means the opposite position is already gone and the entry proceeds (an incomplete Toolbox list returns an "uncertain" `TOOLBOX_INCOMPLETE` result instead, so the entry is refused).
- Reconciliation pairs observed positions with ledger rows by ticket (same symbol only), then by symbol+side. A ledger row is closed ("closed_on_server") only after 2 consecutive reads without it and proof of a close (see ACCOUNT poll above). Untracked notifications are sent once per (symbol, side) while it stays untracked.
- Kill switch: when kv halted does not start with "KILL" → enqueue FLATTEN, set it, notify critical; (review) a kill-level read that is implausible (equity moved > 3% since the previous read, or below half the balance) is re-read once first (a failed re-read kills). While killed, leftovers (rows, ledger rows, margin > 0, or an unverifiable list) are re-flattened 15 s after a failed flatten, then 30, 60, 120 s, and every 300 s after 5 failures in a row; a successful flatten resets it. `_flatten_pending` is cleared when the FLATTEN is dequeued, so a kill detected by its own after-flatten read queues a follow-up. After FLATTEN/CLOSE_ALL the account is re-read.
- Rollover keeps an existing day_state (e.g. from `set-reference`); otherwise ref_balance/ref_equity = highest balance/equity candidate (initial balance counts as a balance when there is no history). Deviation: `have_history` is "a snapshot exists before today's server midnight" (not `store.has_any_snapshot()`), because the startup account read runs before the first rollover and would otherwise drop the conservative initial-balance candidate on a fresh install. Start order: recovery → account read → rollover → (review) listener → threads (a bind failure starts nothing). The GUI executor's `abort_check` is set to the engine's halt/pause/flatten check.
- kv `command`: `{"cmd": "flatten", "ts": iso, "by": "cli"}`; a command older than 1 hour is discarded with a warning. Unknown commands are ignored.
- heartbeat.json: ts, pid, version, mode, executor, port, started_at, queue_size, current_task, untracked, positions_uncertain, last_account_read, account_read_failures, last_account_error, account_poll_crashes, executor_stalled, stopped (and `error` when `tvbridge run` failed to start). A heartbeat older than 30 s (or stopped, or executor_stalled) means the engine is not running.
- (review) Watchdog (scheduler tick): executor thread dead, or the current task running longer than 300 s (FLATTEN/CLOSE_ALL/CLOSE/OPEN) / 180 s (ACCOUNT) → critical notification, heartbeat `executor_stalled`, notifier flushed, `os._exit(1)` (launchd restarts). Free disk space under 2 GB → critical notification at most hourly. A crashed ACCOUNT task → critical (throttled) and a counter in status/heartbeat.
- Cleanup (start and every hour): screenshot folders `shots/YYYYMMDD` older than keep_screenshots_days (other folders such as `shots/calibration` are kept), then the oldest day folders (never today's) while their total exceeds 2 GB; snapshots older than 35 days.
- Module helpers used by the CLI: `build_status(cfg, store, now=None)` (status from the database alone), `read_heartbeat`, `heartbeat_age_s`, `engine_alive`, `floors_for_day`, `config_warnings` (kill_buffer_pct not below daily/max buffer: warned at start and by doctor, not a ConfigError because the spec allows buffers of 0).
- The engine reads the config once at construction; config changes need a restart.

## 13. cli.py — `main(argv=None) -> int`

argparse subcommands: `init`, `run`, `doctor [--prompt]`, `calibrate`, `status [--json]`, `read-account`, `rehearse --symbol S --side buy|sell --sl X [--tp Y] [--lots L] [--price P]`, `send-test --action A --symbol S [--price P --sl X --tp Y --url U]`, `pause [--reason R]`, `resume [--force]`, `flatten [--yes]`, `set-reference VALUE [--date YYYY-MM-DD] [--force]`, `events [--limit N]`, `ngrok-config`.
- `init`: create home (0700), copy config.example.json → config.json if absent with `server.secret = secrets.token_urlsafe(32)`, chmod 600, print paths and the TradingView webhook URL if ngrok.domain is set.
- `run`: logging (RotatingFileHandler 5 MB × 5 in log_dir + stderr), Store, Notifier, make_executor, Engine.start; spawn `caffeinate -dimsu -w <pid>`; block until SIGTERM/SIGINT (or an engine thread dies → exit 1); stop. (review) A failure after the config loaded (Store, make_executor, Engine.start) writes heartbeat.json with `error`, sends a critical notification at most every 15 min across launchd restarts (`home/startup_failure.json`), exits 1.
- `doctor`: config OK, secret length, mode, `sys.executable` resolved path (that binary needs the permissions), driver.permissions(), MT5 windows found (+ titles), calibration present & dialog size, ngrok binary/config, `GET http://127.0.0.1:<port>/health`, heartbeat age. Non-zero exit if any check fails.
- `ngrok-config`: write `home/ngrok.yml` (`version: "3"`, `agent: {authtoken: ...}`) chmod 600; print `https://<domain><path>`.
- `send-test`: build payload with secret + `time` = now ISO, POST to `http://127.0.0.1:<port><path>` (no XFF ⇒ local), print status + body.
- `flatten`: require `--yes` or typed confirmation "FLATTEN"; writes kv command.
- (review) `resume [--force]`: a KILL halt is only cleared when the latest snapshot is fresh (≤ equity_max_age_s) and equity is above today's kill floor (`--force` overrides); also writes kv `resume_ack`.
- (review) `set-reference VALUE [--date D] [--force]`: lowering the existing reference, or a value more than 10% from the latest snapshot's max(balance, equity), prints old/new floors and needs the value typed again (refused without a TTY) unless `--force`.

Integration decisions (as implemented in cli.py):
- Exit codes: 0 ok, 1 failure, 2 usage/config error, 130 interrupted.
- `init` keeps an existing config.json (it only replaces a hand-copied placeholder secret), never prints the secret, and exits 1 if the existing config is invalid.
- `run` takes an exclusive lock on `home/engine.lock` (a second engine exits 1), logs to `log_dir/tvbridge.log` (rotating) and to stderr (everything on a terminal, warnings and errors only otherwise, because launchd's stderr capture is not rotated; `TVBRIDGE_LOG_LEVEL` overrides INFO), stops the engine with a 25 s timeout on SIGTERM/SIGINT and terminates caffeinate.
- `doctor` also prints the running image (`ps -o comm=`), a note when run from a terminal (macOS checks the terminal's permissions), the server time in use, config warnings, the ngrok agent's tunnels from `127.0.0.1:4040`, and paused/halted state. MT5 checks (permissions, windows, calibration) fail only in rehearsal/live mode and warn in paper mode.
- `status` uses `engine.build_status` plus heartbeat.json (running?, untracked, queue size) and the last 5 signals.
- `read-account` and `rehearse` always use Mt5GuiExecutor with rehearsal=True; `rehearse` defaults `--lots` to the symbol's min_lot (capped by max_lots).
- `send-test` adds a unique `id`; in live mode every entry (also with `--url`) needs the typed confirmation "LIVE" (refused without a terminal).
- `status` / `doctor` also show positions_uncertain, executor_stalled, account poll crashes and a failed start from the heartbeat; `doctor` picks the MT5 window with `select_main_window` and fails in live mode without `account_login`; `read-account` prints `positions: UNKNOWN (TOOLBOX_INCOMPLETE: ...)`.

## 14. Ops files

- `requirements.txt`: `pyobjc-core>=10.3,<12`, `pyobjc-framework-Quartz>=10.3,<12`, `pyobjc-framework-Vision>=10.3,<12`, `pyobjc-framework-Cocoa>=10.3,<12`, `pyobjc-framework-ApplicationServices>=10.3,<12`.
- `install.sh [--no-launchd] [--no-ngrok]`: create `.venv` with `/usr/bin/python3`, pip install, `python -m tvbridge init`, download ngrok for `uname -m` (arm64/x86_64) from `https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-<arm64|amd64>.zip` into `$TVBRIDGE_HOME/bin/`, `python -m tvbridge ngrok-config` if token+domain set, render launchd templates into `~/Library/LaunchAgents/`, `launchctl bootout gui/$UID/<label>` (ignore failure) then `launchctl bootstrap gui/$UID <plist>`. Idempotent. Prints next steps. Never uses sudo.
- launchd templates: LaunchAgents (must run in the logged-in GUI session to click), `RunAtLoad`, `KeepAlive`, `ThrottleInterval` 10, `ProcessType` Interactive, logs to `~/Library/Logs/tvbridge/`, env `TVBRIDGE_HOME`, `PATH`. Placeholders `__PYTHON__ __APPDIR__ __TVBRIDGE_HOME__ __NGROK__ __DOMAIN__ __PORT__ __LOGDIR__`. ngrok: `__NGROK__ http 127.0.0.1:__PORT__ --url https://__DOMAIN__ --config __TVBRIDGE_HOME__/ngrok.yml --log stdout --inspect=false` (review: no webhook bodies kept on the local inspection API).
- `uninstall.sh`: bootout + delete the two plists; keeps data.

## 15. Mirror mode (action `sync`)

One TradingView strategy alert (fires on every order fill) reports the strategy's resulting position; tvbridge makes the MT5 position on that symbol match it. For strategies that manage stops/targets/trailing/partial exits inside TradingView and have no per-order alert messages (`strategy/xau_x3_5s_mart_lab.pine`). TradingView's price feed differs from the broker's (about $0.9 on gold), so the broker-side stop is measured from the MT5 ticket's own quote.

**Payload (signals.py).** `action` `sync`; `symbol|ticker` (required); `time` (required); `position|market_position` in long/short/flat (also buy/sell) else `BAD_POSITION`; `size|market_position_size` (number; absolute value; required and > 0 unless flat, else `BAD_SIZE`; ignored when flat); `price` (optional, non-strict: garbage → None; required to open); optional `prev_position`, `prev_size`, `order_action`, `order_contracts`, `order_id`, `comment` (kept in `raw`). `Signal.target_side` ("buy"/"sell"/None) and `target_units`; `sl`/`tp`/`risk_pct` are None. Id: always `"sync:" + sha256[:32]` over [symbol, position, size, iso(fired_at), order_id, order_action, order_contracts, price, id], so a TradingView retry is a duplicate and two fills in the same second differ. `peek_action` returns "sync".

**Server.** A sync is never rate-limited when authenticated. Freshness like an exit: FUTURE accepted, STALE accepted up to `EXIT_MAX_AGE_S` (no ledger check; the engine acts only on the newest sync and never opens on a stale one); beyond that 400 `STALE` plus the throttled critical "exit refused" notification. An id collision is always a duplicate.

**Executors.** `OrderRequest.sl_distance` / `tp_distance`: the executor computes the absolute SL/TP from the live quote of its order ticket (buy: sl = ask − sl_distance, tp = ask + tp_distance; sell: sl = bid + sl_distance, tp = bid − tp_distance; rounded to digits) and reports them in `OrderResult.sl` / `.tp`. `close_partial(symbol, side, lots)`: largest position first; a position the remainder covers (within `mirror.size_tolerance_lots` for the GUI) is closed whole, the last one reduced; one result per position, `lots` = what was closed there; `[]` when nothing matched.
- Paper: quote = `price_hint`; a partial close realises P/L and commission pro rata.
- Mt5Gui `open_market` with `sl_distance`: after the symbol field and the 0.5 s wait, OCR the ticket and `parse.parse_ticket_quote(items)` (an observation, or a rebuilt row, matching `^\s*\d[\d ]*\.\d+\s*/\s*\d[\d ]*\.\d+\s*$`, ask ≥ bid, spread ≤ 1 % of the price; several different quotes → the tallest text, a tie → None). None → Escape, `OrderResult("error", "QUOTE_UNREADABLE: ...")`. With `price_hint`: |quote − hint| / hint × 100 > `mirror.max_price_gap_pct` (quote = ask for a buy, bid for a sell) → Escape, `"PRICE_GAP: ..."`. Then the normal flow with the computed levels (fill, read-back verification, button guard, abort check, single click, result).
- Mt5Gui `close_partial`: the close flow for one row; before the close button may be clicked: find the "Volume" label by OCR, click at (label right edge + `gui.volume_field_offset_px`, label cy), End, Shift+Home, type the lots, Tab; OCR again; the close button (item containing "close" and the symbol) must contain the typed volume as a number token (within 1e-9) and the position's ticket when one was read; otherwise Escape and `OrderResult("error", "PARTIAL_VERIFY_FAILED: ...")`, no click. Rehearsal: everything but the click, Escape, "rehearsed". After the click the result is classified like a full close; an "uncertain" result becomes "filled" when the Toolbox lists that ticket with exactly the remaining lots (the analogue of "the row is gone"); a "filled" result naming another volume is "uncertain" `RESULT_MISMATCH`.

**Engine (`_do_sync`, task kind SYNC, priority of CLOSE).** `mirror.enabled` false → status rejected `MIRROR_DISABLED`, notify warn. Then:
a. A later sync for the symbol is stored (`store.newer_sync_signal`) → expired `SUPERSEDED_BY_SYNC`.
b. `read_account`. Positions unknown: target flat → `close_positions(symbol)` anyway; else failed `POSITIONS_UNKNOWN`, notify critical.
c. current = observed positions on the symbol (untracked included). target lots = round down(units / units_per_lot × scale); scale = kv `mirror_scale:<symbol>` (default 1.0), reset when the symbol is flat (at a sync, after a close/flatten).
d. Target flat → `close_positions(symbol)`, ledger closed (`sync_close`), done (`IN_SYNC` if already flat). Always allowed.
e. A position on the other side → close everything on the symbol, re-read (anything left, or list unknown → failed `CLOSE_FAILED`), continue as flat.
f. Same side, current > target + tolerance → `close_partial(symbol, side, current − target)`; ledger rows reduced/closed (`store.reduce_ledger_position`); done. A target that rounds to 0 closes everything.
g. Within tolerance → done `IN_SYNC`.
h. Same side, current < target − tolerance → rejected `MIRROR_ADD_REFUSED` + warn, unless `allow_adds` (then the difference is an entry as in i).
i. Flat and target non-flat → entry: rejected `MIRROR_NOT_AN_ENTRY` if the alert shows a reduction (`prev_position` same side and `prev_size` > `size`); `NO_PRICE`; age > `risk.entry_max_delay_s` → expired `STALE_SIGNAL` (dated more than `max_future_skew_s` ahead → rejected `FUTURE`); HALTED / PAUSED / FLATTEN_PENDING; `SUPERSEDED_BY_CLOSE`; newer sync → `SUPERSEDED_BY_SYNC`; `risk.plan_entry` on a synthetic entry (sl = price ∓ stop_distance, tp = price ± tp_distance, risk_pct = max_risk_per_trade_pct). lots = round down(min(wanted, plan.lots, max_lots)); risk_usd = plan.risk_usd × lots / plan.lots; age/block/newer-sync re-checked right before the order; `OrderRequest(..., sl=<synthetic>, sl_distance, tp_distance, price_hint=price)`. Result handling is the OPEN path's (`_entry_result`), booking the SL/TP the executor reports. Unless the result is rejected/error/rehearsed, scale = (current + opened) / (units / units_per_lot rounded down) is stored.
j. An executor exception in a close path → failed `CLOSE_FAILED`, notify critical.
Recovery: queued syncs are re-submitted; a sync found "processing" at startup → failed `INTERRUPTED` + halt. A crashed SYNC task halts like OPEN. `min_hold_s_for_signal_close` does not apply. `build_status` / `tvbridge status` show `mirror` (enabled, units_per_lot, stop_distance, scales).

**Fan-out (`mirror.fan_out`).** In `_do_sync`, after the `MIRROR_DISABLED` check: a sync whose `tv_symbol` has a fan_out entry and whose `raw` has no `fan_out_parent` is a *parent*. Superseded check first (`store.newer_sync_signal(id, exclude_id_prefix=id + "@")`, its own children do not count) → expired `SUPERSEDED_BY_SYNC`. Otherwise one child per target, in the listed order: `dataclasses.replace(parent, id=parent.id + "@" + mt5, symbol=mt5, tv_symbol=<target key>, raw={**raw, "fan_out_parent": parent.id})`; `store.insert_signal(child)` (an existing id is kept: idempotent); parent → done `FANNED_OUT: <MT5 symbols>` with result `{"fan_out": [child ids]}`, event `sync_fanned_out`; every child whose stored status is "queued" is `submit_signal`ed (normal SYNC tasks). The parent never becomes "processing" and sends no order. A child never fans out. Children run the normal a–j logic on their own symbol (units → lots with `Config.mirror_units_per_lot(symbol)`, own `mirror_scale:<key>`, own ledger rows, results, notifications, risk guard per child). A child is superseded by a newer sync on its own symbol or by a newer sync on its parent's symbol that is not a sibling (`newer_sync_signal(parent, parent + "@")`), so children of an older alert do not act while a newer alert waits to fan out. Recovery: a "processing" sync that is a parent whose children are all stored → done `FANNED_OUT` (no halt); otherwise `INTERRUPTED` + halt as before; queued parents and children are re-submitted. `build_status` `mirror` adds `units_per_lot_by_symbol` and `fan_out` ({key: [{symbol, units_per_lot}]}); `tvbridge status` appends `; fan-out <key> -> <MT5 symbol> (<n> units per lot), ...` to the mirror line.
