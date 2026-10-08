"""Configuration: JSON file deep-merged over DEFAULTS, strictly validated.

``load_config()`` reads ``$TVBRIDGE_HOME/config.json`` (default ``~/.tvbridge``) and
raises :class:`ConfigError` with a precise, human-readable message on any problem.
Unknown keys raise (catches typos) unless they start with "_" (comments).
"""

import copy
import ipaddress
import json
import math
import os
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, get_type_hints

from . import clock

SECRET_PLACEHOLDER = "CHANGE_ME_run_tvbridge_init"
MIN_SECRET_LEN = 16
EXECUTOR_MODES = ("paper", "rehearsal", "live")
NOTIFY_LEVELS = ("debug", "info", "warn", "critical")

#: config.example.json shipped at the repository root (copied by ``tvbridge init``).
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.example.json"


class ConfigError(Exception):
    """Invalid or missing configuration. ``str(e)`` is a precise message for the user."""


def default_home() -> Path:
    """Runtime data directory: ``$TVBRIDGE_HOME`` or ``~/.tvbridge``."""
    return Path(os.environ.get("TVBRIDGE_HOME", "~/.tvbridge")).expanduser()


# --------------------------------------------------------------------------- dataclasses


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8787
    path: str = "/webhook"
    secret: str = ""
    enforce_ip_allowlist: bool = True
    tradingview_ips: List[str] = field(
        default_factory=lambda: ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"]
    )
    allow_local_requests: bool = True
    max_body_bytes: int = 8192
    max_signal_age_s: float = 120
    max_future_skew_s: float = 30
    rate_limit_per_min: int = 30
    # Other tvbridge engines (one per MT5 account) that get a copy of every authenticated alert,
    # e.g. ["http://127.0.0.1:8788/webhook"]. They must use the same server.secret.
    forward_to: List[str] = field(default_factory=list)


@dataclass
class AccountCfg:
    name: str = "Hantec Endurance 50k"
    initial_balance: float = 50000.0
    currency: str = "USD"
    # A number of hours, or "auto": UTC+3 while US DST is in effect, else UTC+2 (brokers
    # whose day ends at the New York close). Resolve it with Config.server_offset_at().
    server_utc_offset_hours: Union[float, str] = 3.0
    account_login: str = ""   # if set, the MT5 main window title must contain it
    server_name: str = ""     # if set, the MT5 main window title must contain it


@dataclass
class RiskCfg:
    daily_loss_pct: float = 4.0
    max_loss_pct: float = 8.0
    daily_buffer_pct: float = 1.0
    max_buffer_pct: float = 1.0
    kill_buffer_pct: float = 0.3
    risk_per_trade_pct: float = 0.5
    max_risk_per_trade_pct: float = 1.0
    max_total_open_risk_pct: float = 2.0
    max_open_positions: int = 3
    max_trades_per_day: int = 8
    max_lots: float = 5.0
    commission_per_lot_usd: float = 5.0
    slippage_buffer_pct: float = 15.0
    equity_max_age_s: float = 90
    entry_max_delay_s: float = 45
    reverse_on_opposite: bool = True
    allow_pyramiding: bool = False
    block_untracked_positions: bool = True
    trading_start_server: str = "00:05"
    trading_end_server: str = "23:50"
    trading_days_server: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon=0, server time
    friday_cutoff_server: Optional[str] = "22:00"   # None disables
    min_hold_s_for_signal_close: float = 0
    # True: after an uncertain entry (e.g. the order window closed before its result could be read),
    # entries resume on their own when MT5 shows exactly one new position on that symbol and side with
    # the order's size, a ticket and a stop-loss. False: always wait for `tvbridge resume`.
    resume_after_verified_fill: bool = False
    # When the engine was down over server midnight (no fresh snapshot near 00:00), today's daily
    # reference can only be estimated. 0: store it as "stale_estimate" and refuse entries until
    # `tvbridge set-reference`. > 0: raise the estimate by this many percent as a safety margin
    # (tighter floors), store it as "stale_buffered" and keep trading; a warning is sent.
    stale_reference_buffer_pct: float = 0.0


@dataclass
class SymbolSpec:
    contract_size: float = 100000.0
    quote: str = "USD"
    digits: int = 5
    point: float = 0.00001
    lot_step: float = 0.01
    min_lot: float = 0.01
    min_sl_points: float = 50
    # round-trip commission per 1.00 lot of THIS symbol in USD; None = risk.commission_per_lot_usd
    commission_per_lot_usd: Optional[float] = None

    @property
    def lot_decimals(self) -> int:
        """Number of decimals in lot_step (0.01 -> 2, 0.1 -> 1, 1 -> 0)."""
        try:
            exp = Decimal(repr(float(self.lot_step))).normalize().as_tuple().exponent
        except (InvalidOperation, ValueError):
            return 2
        if not isinstance(exp, int):
            return 2
        return max(0, -exp)


@dataclass
class SymbolsCfg:
    suffix: str = ".h"
    map: Dict[str, str] = field(default_factory=dict)          # normalized TV symbol -> exact MT5 symbol
    specs: Dict[str, SymbolSpec] = field(default_factory=dict)  # keyed by normalized TV symbol
    allowed: List[str] = field(default_factory=list)           # empty = every symbol in specs


@dataclass
class GuiCfg:
    owner_names: List[str] = field(
        default_factory=lambda: ["MetaTrader 5", "terminal64", "wine64-preloader", "wine-preloader", "wine"]
    )
    main_title_contains: str = ""
    order_dialog_title_contains: List[str] = field(default_factory=lambda: ["Order"])
    position_dialog_title_contains: List[str] = field(default_factory=lambda: ["Position", "Order"])
    require_dialog_text: List[str] = field(default_factory=lambda: ["Market"])
    dialog_timeout_s: float = 4.0
    result_timeout_s: float = 8.0
    action_delay_s: float = 0.15
    size_tolerance_px: float = 12
    ocr_min_confidence: float = 0.3
    account_poll_s: float = 15
    keep_screenshots_days: int = 14
    # partial close: the volume field of the position dialog is clicked this many points right
    # of the right edge of its "Volume" label
    volume_field_offset_px: float = 85
    # How the New Order window is opened: "f9" (falls back to the toolbar button) or "toolbar".
    open_order_via: str = "f9"
    # Lock file shared by every tvbridge engine on this Mac (one mouse, one keyboard).
    # Empty = <home>/gui.lock (single engine).
    lock_path: str = ""
    # How long to wait for another engine to finish driving MetaTrader before giving up (GUI_BUSY).
    lock_timeout_s: float = 30.0


@dataclass
class ExecutorCfg:
    mode: str = "paper"                          # "paper" | "rehearsal" | "live"
    paper_start_balance: Optional[float] = None
    gui: GuiCfg = field(default_factory=GuiCfg)


@dataclass
class MirrorCfg:
    """Mirror mode: ``sync`` alerts report the strategy's position and tvbridge makes MT5 match it."""

    enabled: bool = False
    units_per_lot: float = 100.0        # strategy units (e.g. ounces) per 1.00 MT5 lot
    stop_distance: float = 8.5          # broker-side protective stop, price units from the ticket's own quote
    # > 0: the stop keeps the DOLLAR risk of the strategy's size constant, like the strategy's own hard
    # stop: distance = account.initial_balance * idea_risk_pct / 100 / (strategy units), never wider
    # than stop_distance. 0 = always stop_distance.
    idea_risk_pct: float = 0.0
    tp_distance: float = 0.0            # 0 = no take-profit on the order
    size_tolerance_lots: float = 0.005  # lot differences up to this count as "in sync"
    max_price_gap_pct: float = 0.5      # max % between the alert price and the MT5 quote before opening
    allow_adds: bool = False            # False: never add to an open position
    # Fan-out: one sync alert for the key (normalized TV symbol) is mirrored onto every listed
    # target symbol (config keys or MT5 names), each as its own position with the full strategy size.
    fan_out: Dict[str, List[str]] = field(default_factory=dict)
    # per-target override of units_per_lot, keyed by normalized symbol key (e.g. a micro contract)
    units_per_lot_by_symbol: Dict[str, float] = field(default_factory=dict)
    # per-symbol overrides of stop_distance / tp_distance (price units), keyed like units_per_lot_by_symbol
    stop_distance_by_symbol: Dict[str, float] = field(default_factory=dict)
    tp_distance_by_symbol: Dict[str, float] = field(default_factory=dict)
    # USD value of one unit of a symbol's quote currency (e.g. {"XAUEUR": 1.12} = 1 EUR is 1.12 USD),
    # for symbols not quoted in USD. A fan-out child on such a symbol gets the parent's alert price
    # divided by this rate, and the rate sizes its risk. It is approximate on purpose: when the
    # market drifts away from it the price-gap check refuses entries until it is updated.
    quote_usd_by_symbol: Dict[str, float] = field(default_factory=dict)
    # per-symbol override of max_price_gap_pct (wider for symbols converted with quote_usd_by_symbol)
    max_price_gap_pct_by_symbol: Dict[str, float] = field(default_factory=dict)
    # Re-entry after a broker-side stop: when MT5 closes a mirrored position at a loss (its stop)
    # while the strategy still holds that side (its latest sync on the symbol is unchanged), the
    # position is opened again after reenter_cooldown_s, at most reenter_max times per strategy
    # position. A close in profit (take-profit, manual) never re-enters.
    reenter_after_stop: bool = False
    reenter_cooldown_s: float = 60.0
    reenter_max: int = 1


@dataclass
class NotifyCfg:
    macos: bool = True
    ntfy_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    min_level: str = "info"                      # debug < info < warn < critical


@dataclass
class NgrokCfg:
    authtoken: str = ""
    domain: str = ""                             # e.g. "your-name.ngrok-free.app"; no scheme


def normalize_tv_symbol(s: Optional[str], suffix: str = ".h") -> str:
    """Normalize a TradingView/MT5 symbol to the canonical key used in config.

    Strips whitespace, keeps the part after the last ":" ("OANDA:EURUSD" -> "EURUSD"),
    removes "/" and "_", uppercases and strips a trailing ``suffix`` (case-insensitive,
    "EURUSD.H" -> "EURUSD"). ``Config.normalize_tv_symbol`` passes the configured suffix.
    """
    if s is None:
        return ""
    out = str(s).strip()
    if ":" in out:
        out = out.rsplit(":", 1)[1]
    out = out.replace("/", "").replace("_", "").strip().upper()
    suf = (suffix or "").strip().upper()
    if suf and len(out) > len(suf) and out.endswith(suf):
        out = out[: -len(suf)]
    return out


@dataclass
class Config:
    home: Path
    server: ServerCfg = field(default_factory=ServerCfg)
    account: AccountCfg = field(default_factory=AccountCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    symbols: SymbolsCfg = field(default_factory=SymbolsCfg)
    executor: ExecutorCfg = field(default_factory=ExecutorCfg)
    mirror: MirrorCfg = field(default_factory=MirrorCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    ngrok: NgrokCfg = field(default_factory=NgrokCfg)
    source_path: Optional[Path] = None           # the file this config was loaded from, if any

    # ---- paths
    @property
    def db_path(self) -> Path:
        return Path(self.home) / "tvbridge.db"

    @property
    def calibration_path(self) -> Path:
        return Path(self.home) / "calibration.json"

    @property
    def heartbeat_path(self) -> Path:
        return Path(self.home) / "heartbeat.json"

    @property
    def shots_dir(self) -> Path:
        return Path(self.home) / "shots"

    @property
    def log_dir(self) -> Path:
        return Path("~/Library/Logs/tvbridge").expanduser()

    # ---- server time
    @property
    def server_offset_is_auto(self) -> bool:
        return isinstance(self.account.server_utc_offset_hours, str)

    def server_offset_at(self, dt_utc: Optional[datetime] = None) -> float:
        """Broker server UTC offset (hours) at instant ``dt_utc`` (default: now).

        A fixed number from ``account.server_utc_offset_hours``, or for ``"auto"`` UTC+3
        while US DST is in effect and UTC+2 otherwise.
        """
        if self.server_offset_is_auto:
            return clock.ny_close_offset_hours(dt_utc if dt_utc is not None else clock.utcnow())
        return float(self.account.server_utc_offset_hours)

    def server_offset_for_day(self, server_day: date) -> float:
        """Offset in force at 00:00 server time on ``server_day`` (for its midnight/day range).

        With "auto" it is evaluated at 21:30 UTC of the previous day, which lies between the
        two possible server midnights (21:00/22:00 UTC) and far from any US DST switch.
        """
        if not self.server_offset_is_auto:
            return float(self.account.server_utc_offset_hours)
        probe = datetime(server_day.year, server_day.month, server_day.day, tzinfo=timezone.utc)
        return clock.ny_close_offset_hours(probe - timedelta(hours=2, minutes=30))

    def server_date(self, dt_utc: datetime) -> date:
        """Broker server calendar date at ``dt_utc``."""
        return clock.server_date(dt_utc, self.server_offset_at(dt_utc))

    def server_midnight_utc(self, server_day: date) -> datetime:
        """UTC instant of 00:00 server time on ``server_day``."""
        return clock.server_midnight_utc(server_day, self.server_offset_for_day(server_day))

    # ---- symbols
    def normalize_tv_symbol(self, s: Optional[str]) -> str:
        """Module-level :func:`normalize_tv_symbol` with this config's suffix."""
        return normalize_tv_symbol(s, self.symbols.suffix)

    def mt5_symbol(self, tv_symbol: str) -> str:
        """MT5 symbol for a TV symbol: ``map[norm]`` if present else ``norm + suffix``."""
        norm = self.normalize_tv_symbol(tv_symbol)
        mapped = self.symbols.map.get(norm)
        if mapped:
            return mapped
        return norm + self.symbols.suffix

    def tv_symbol_for(self, symbol: str) -> Optional[str]:
        """Config key (normalized TV symbol) for a TV or MT5 symbol, or None if unknown.

        Exact MT5 names from ``map`` are matched first (case-insensitive), then the
        normalized form is looked up in ``specs``, then normalized ``map`` values.
        """
        raw = (symbol or "").strip()
        if not raw:
            return None
        low = raw.lower()
        for tv, mt5 in self.symbols.map.items():
            if mt5.strip().lower() == low:
                return tv
        norm = self.normalize_tv_symbol(raw)
        if norm in self.symbols.specs:
            return norm
        for tv, mt5 in self.symbols.map.items():
            if self.normalize_tv_symbol(mt5) == norm:
                return tv
        return None

    def spec_for(self, symbol: str) -> Optional[SymbolSpec]:
        """SymbolSpec for a TV or MT5 symbol (reverse-looks-up ``map`` values)."""
        key = self.tv_symbol_for(symbol)
        if key is None:
            return None
        return self.symbols.specs.get(key)

    def is_allowed(self, tv_symbol: str) -> bool:
        """True if the normalized symbol is in ``allowed`` (or in ``specs`` when allowed is empty)."""
        norm = self.normalize_tv_symbol(tv_symbol)
        if not norm:
            return False
        if self.symbols.allowed:
            return norm in self.symbols.allowed
        return norm in self.symbols.specs

    def mirror_units_per_lot(self, symbol: str) -> float:
        """Strategy units per 1.00 lot of ``symbol`` (TV or MT5 form) in mirror mode:
        ``mirror.units_per_lot_by_symbol`` for its config key, else ``mirror.units_per_lot``."""
        key = self.tv_symbol_for(symbol) or self.normalize_tv_symbol(symbol)
        value = self.mirror.units_per_lot_by_symbol.get(key)
        return float(value if value is not None else self.mirror.units_per_lot)

    def mirror_stop_distance(self, symbol: str) -> float:
        """Protective stop distance for ``symbol`` (TV or MT5 form) in mirror mode:
        ``mirror.stop_distance_by_symbol`` for its config key, else ``mirror.stop_distance``."""
        key = self.tv_symbol_for(symbol) or self.normalize_tv_symbol(symbol)
        value = self.mirror.stop_distance_by_symbol.get(key)
        return float(value if value is not None else self.mirror.stop_distance)

    def mirror_tp_distance(self, symbol: str) -> float:
        """Take-profit distance for ``symbol`` in mirror mode (0 = none):
        ``mirror.tp_distance_by_symbol`` for its config key, else ``mirror.tp_distance``."""
        key = self.tv_symbol_for(symbol) or self.normalize_tv_symbol(symbol)
        value = self.mirror.tp_distance_by_symbol.get(key)
        return float(value if value is not None else self.mirror.tp_distance)

    def mirror_quote_usd(self, symbol: str) -> Optional[float]:
        """``mirror.quote_usd_by_symbol`` for ``symbol`` (TV or MT5 form), or None."""
        key = self.tv_symbol_for(symbol) or self.normalize_tv_symbol(symbol)
        value = self.mirror.quote_usd_by_symbol.get(key)
        return float(value) if value else None

    def mirror_max_price_gap_pct(self, symbol: str) -> float:
        """Largest allowed gap between the alert price and the MT5 quote for ``symbol``, in percent."""
        key = self.tv_symbol_for(symbol) or self.normalize_tv_symbol(symbol)
        value = self.mirror.max_price_gap_pct_by_symbol.get(key)
        return float(value if value is not None else self.mirror.max_price_gap_pct)

    def mirror_fan_out(self, symbol: str) -> List[str]:
        """Fan-out target keys of a sync alert for ``symbol`` (TV or MT5 form); [] = none."""
        fan = self.mirror.fan_out
        if not fan:
            return []
        norm = self.normalize_tv_symbol(symbol)
        if norm in fan:
            return list(fan[norm])
        key = self.tv_symbol_for(symbol)
        return list(fan.get(key, [])) if key else []

    def commission_per_lot(self, symbol: Optional[str]) -> float:
        """Round-trip commission per lot in USD for ``symbol`` (TV or MT5 form): the spec's
        ``commission_per_lot_usd`` when set, else ``risk.commission_per_lot_usd``."""
        spec = self.spec_for(symbol) if symbol else None
        if spec is not None and spec.commission_per_lot_usd is not None:
            return float(spec.commission_per_lot_usd)
        return float(self.risk.commission_per_lot_usd)

    def known_mt5_symbols(self) -> List[str]:
        """MT5 names for every configured spec."""
        return [self.mt5_symbol(k) for k in self.symbols.specs]

    def to_dict(self, redact: bool = True) -> Dict[str, Any]:
        """JSON-safe dict of the effective config; secrets/tokens redacted by default."""
        d = {
            "home": str(self.home),
            "server": _dc_to_dict(self.server),
            "account": _dc_to_dict(self.account),
            "risk": _dc_to_dict(self.risk),
            "symbols": _dc_to_dict(self.symbols),
            "executor": _dc_to_dict(self.executor),
            "mirror": _dc_to_dict(self.mirror),
            "notify": _dc_to_dict(self.notify),
            "ngrok": _dc_to_dict(self.ngrok),
        }
        if redact:
            for sect, key in (("server", "secret"), ("notify", "telegram_bot_token"), ("ngrok", "authtoken")):
                if d[sect].get(key):
                    d[sect][key] = "***"
        return d


def _dc_to_dict(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _dc_to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _dc_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dc_to_dict(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- defaults


def _fx(quote: str = "USD", digits: int = 5, point: float = 0.00001, min_sl_points: float = 50,
        contract_size: float = 100000.0) -> Dict[str, Any]:
    return _dc_to_dict(SymbolSpec(contract_size=contract_size, quote=quote, digits=digits, point=point,
                                  min_sl_points=min_sl_points))


DEFAULT_SPECS = {
    "EURUSD": _fx("USD"),
    "GBPUSD": _fx("USD"),
    "AUDUSD": _fx("USD"),
    "NZDUSD": _fx("USD"),
    "USDJPY": _fx("JPY", digits=3, point=0.001),
    "USDCAD": _fx("CAD"),
    "USDCHF": _fx("CHF"),
    "XAUUSD": _fx("USD", digits=2, point=0.01, min_sl_points=100, contract_size=100.0),
}  # type: Dict[str, Dict[str, Any]]


def _build_defaults() -> Dict[str, Any]:
    symbols = _dc_to_dict(SymbolsCfg())
    symbols["specs"] = copy.deepcopy(DEFAULT_SPECS)
    return {
        "server": _dc_to_dict(ServerCfg()),
        "account": _dc_to_dict(AccountCfg()),
        "risk": _dc_to_dict(RiskCfg()),
        "symbols": symbols,
        "executor": _dc_to_dict(ExecutorCfg()),
        "mirror": _dc_to_dict(MirrorCfg()),
        "notify": _dc_to_dict(NotifyCfg()),
        "ngrok": _dc_to_dict(NgrokCfg()),
    }


DEFAULTS = _build_defaults()  # type: Dict[str, Any]


def defaults_dict() -> Dict[str, Any]:
    """A deep copy of DEFAULTS (safe to mutate)."""
    return copy.deepcopy(DEFAULTS)


# --------------------------------------------------------------------------- merge & build


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict: ``override`` recursively merged over ``base``.

    Nested dicts merge key by key; every other value (lists included) replaces.
    """
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _type_name(tp: Any) -> str:
    names = {bool: "true/false", int: "an integer", float: "a number", str: "a string"}
    if tp in names:
        return names[tp]
    origin = getattr(tp, "__origin__", None)
    if origin is list:
        return "a list"
    if origin is dict:
        return "an object"
    return getattr(tp, "__name__", str(tp))


def _coerce(value: Any, tp: Any, where: str) -> Any:
    origin = getattr(tp, "__origin__", None)
    args = getattr(tp, "__args__", ()) or ()
    if origin is Union:
        if value is None and type(None) in args:
            return None
        inner = [a for a in args if a is not type(None)]
        if len(inner) == 1:
            return _coerce(value, inner[0], where)
        # e.g. Union[float, str]: the first type that accepts the value wins
        names = " or ".join(_type_name(a) for a in inner)
        for tp_option in inner:
            try:
                return _coerce(value, tp_option, where)
            except ConfigError:
                continue
        raise ConfigError("%s: expected %s, got %s" % (where, names, json.dumps(value)))

    def bad() -> ConfigError:
        return ConfigError("%s: expected %s, got %s" % (where, _type_name(tp), json.dumps(value)))

    if tp is bool:
        if isinstance(value, bool):
            return value
        raise bad()
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad()
        if isinstance(value, float):
            if not value.is_integer():
                raise bad()
            return int(value)
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad()
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            raise bad()
        return f
    if tp is str:
        if not isinstance(value, str):
            raise bad()
        return value
    if origin is list:
        if not isinstance(value, list):
            raise bad()
        return [_coerce(v, args[0], "%s[%d]" % (where, i)) for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise bad()
        out = {}
        for k, v in value.items():
            if str(k).startswith("_"):
                continue
            out[str(k)] = _coerce(v, args[1], "%s.%s" % (where, k))
        return out
    if is_dataclass(tp):
        return _build(tp, value, where)
    raise ConfigError("%s: unsupported config type %r" % (where, tp))  # pragma: no cover


def _build(cls: Any, data: Any, where: str) -> Any:
    """Instantiate dataclass ``cls`` from a dict, rejecting unknown keys and bad types."""
    if not isinstance(data, dict):
        raise ConfigError("%s: expected an object, got %s" % (where, json.dumps(data)))
    hints = get_type_hints(cls)
    names = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        key = str(key)
        if key.startswith("_"):
            continue
        if key not in names:
            raise ConfigError(
                "unknown config key '%s.%s' (valid keys: %s)" % (where, key, ", ".join(sorted(names)))
            )
        kwargs[key] = _coerce(value, hints[key], "%s.%s" % (where, key))
    return cls(**kwargs)


_SECTIONS = {
    "server": ServerCfg,
    "account": AccountCfg,
    "risk": RiskCfg,
    "symbols": SymbolsCfg,
    "executor": ExecutorCfg,
    "mirror": MirrorCfg,
    "notify": NotifyCfg,
    "ngrok": NgrokCfg,
}


def _prenormalize_symbols(d: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize keys of symbols.specs / symbols.map in a user override before merging,
    so that e.g. "eurusd" merges into the default "EURUSD" spec."""
    sym = d.get("symbols")
    if not isinstance(sym, dict):
        return d
    suffix = sym.get("suffix", DEFAULTS["symbols"]["suffix"])
    if not isinstance(suffix, str):
        return d  # type error reported later by _coerce
    d = dict(d)
    sym = dict(sym)
    for sub in ("specs", "map"):
        val = sym.get(sub)
        if not isinstance(val, dict):
            continue
        out = {}  # type: Dict[str, Any]
        for k, v in val.items():
            k = str(k)
            if k.startswith("_"):
                out[k] = v
                continue
            nk = normalize_tv_symbol(k, suffix)
            if not nk:
                raise ConfigError("symbols.%s: invalid symbol key %r" % (sub, k))
            if nk in out:
                raise ConfigError("symbols.%s: duplicate symbol %r after normalization (%r)" % (sub, nk, k))
            out[nk] = v
        sym[sub] = out
    d["symbols"] = sym
    return d


def config_from_dict(d: Dict[str, Any], home: Union[str, Path, None] = None,
                     source_path: Optional[Path] = None) -> Config:
    """Build and validate a Config from a (partial) dict deep-merged over DEFAULTS.

    ``home`` defaults to :func:`default_home`. Raises ConfigError.
    """
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise ConfigError("config must be a JSON object at the top level")
    for key in d:
        if not str(key).startswith("_") and key not in _SECTIONS:
            raise ConfigError(
                "unknown config key '%s' (valid sections: %s)" % (key, ", ".join(sorted(_SECTIONS)))
            )
    override = _prenormalize_symbols(d)
    # A null spec in the user file removes that default symbol.
    merged = deep_merge(DEFAULTS, override)
    specs = merged.get("symbols", {}).get("specs")
    if isinstance(specs, dict):
        merged["symbols"]["specs"] = {k: v for k, v in specs.items() if v is not None}

    built = {}
    for name, cls in _SECTIONS.items():
        built[name] = _build(cls, merged.get(name), name)

    cfg = Config(
        home=Path(home).expanduser() if home is not None else default_home(),
        source_path=source_path,
        **built
    )
    _normalize(cfg)
    validate(cfg)
    return cfg


def load_config(path: Union[str, Path, None] = None) -> Config:
    """Load ``path`` (default ``default_home()/config.json``), merge over DEFAULTS, validate.

    When ``path`` is given explicitly, ``home`` is the directory containing it.
    """
    if path is None:
        home = default_home()
        p = home / "config.json"
    else:
        p = Path(path).expanduser()
        home = p.parent if str(p.parent) else Path(".")
    p = Path(os.path.abspath(str(p)))
    home = Path(os.path.abspath(str(home)))
    if not p.exists():
        raise ConfigError(
            "config file not found: %s -- run 'python -m tvbridge init' to create it" % p
        )
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigError("cannot read config file %s: %s" % (p, e))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigError("config file %s is not valid JSON: %s" % (p, e))
    if not isinstance(data, dict):
        raise ConfigError("config file %s must contain a JSON object" % p)
    try:
        return config_from_dict(data, home, source_path=p)
    except ConfigError as e:
        raise ConfigError("%s (in %s)" % (e, p))


# --------------------------------------------------------------------------- validation


def _normalize(cfg: Config) -> None:
    """Canonicalize values that have a single correct spelling."""
    sym = cfg.symbols
    sym.suffix = sym.suffix.strip()
    sym.map = {normalize_tv_symbol(k, sym.suffix): v.strip() for k, v in sym.map.items()}
    sym.specs = {normalize_tv_symbol(k, sym.suffix): v for k, v in sym.specs.items()}
    for spec in sym.specs.values():
        spec.quote = spec.quote.strip().upper()
    sym.allowed = [normalize_tv_symbol(a, sym.suffix) for a in sym.allowed]
    m = cfg.mirror

    def canon(name: str) -> str:
        # a config key or an MT5 name -> its config key; unknown names stay normalized (validate reports them)
        return cfg.tv_symbol_for(name) or normalize_tv_symbol(name, sym.suffix)

    fan = {}  # type: Dict[str, List[str]]
    for k, targets in m.fan_out.items():
        nk = normalize_tv_symbol(k, sym.suffix)
        _req(bool(nk), "mirror.fan_out: invalid symbol key %r" % (k,))
        _req(nk not in fan, "mirror.fan_out: duplicate symbol %r after normalization (%r)" % (nk, k))
        fan[nk] = [canon(t) for t in targets]
    m.fan_out = fan
    upl = {}  # type: Dict[str, float]
    for k, v in m.units_per_lot_by_symbol.items():
        nk = canon(k)
        _req(bool(nk), "mirror.units_per_lot_by_symbol: invalid symbol key %r" % (k,))
        _req(nk not in upl, "mirror.units_per_lot_by_symbol: duplicate symbol %r after normalization (%r)" % (nk, k))
        upl[nk] = v
    m.units_per_lot_by_symbol = upl
    for name in ("stop_distance_by_symbol", "tp_distance_by_symbol", "quote_usd_by_symbol",
                 "max_price_gap_pct_by_symbol"):
        by_sym = {}  # type: Dict[str, float]
        for k, v in getattr(m, name).items():
            nk = canon(k)
            _req(bool(nk), "mirror.%s: invalid symbol key %r" % (name, k))
            _req(nk not in by_sym, "mirror.%s: duplicate symbol %r after normalization (%r)" % (name, nk, k))
            by_sym[nk] = v
        setattr(m, name, by_sym)
    cfg.executor.mode = cfg.executor.mode.strip().lower()
    cfg.notify.min_level = cfg.notify.min_level.strip().lower()
    cfg.ngrok.domain = cfg.ngrok.domain.strip().rstrip("/")
    cfg.account.currency = cfg.account.currency.strip().upper()
    if isinstance(cfg.account.server_utc_offset_hours, str):
        cfg.account.server_utc_offset_hours = cfg.account.server_utc_offset_hours.strip().lower()


def _req(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _check_hhmm(value: str, where: str) -> None:
    try:
        clock.parse_hhmm(value)
    except ValueError as e:
        raise ConfigError("%s: %s" % (where, e))


def validate(cfg: Config) -> None:
    """Semantic validation (types are already checked). Raises ConfigError."""
    s = cfg.server
    if s.secret == SECRET_PLACEHOLDER:
        raise ConfigError(
            "server.secret is still the placeholder %r from config.example.json; "
            "run 'python -m tvbridge init' to create a config with a random secret "
            "(or set server.secret to a random string of at least %d characters)"
            % (SECRET_PLACEHOLDER, MIN_SECRET_LEN)
        )
    _req(len(s.secret) >= MIN_SECRET_LEN,
         "server.secret must be at least %d characters (got %d); run 'python -m tvbridge init' "
         "to generate one" % (MIN_SECRET_LEN, len(s.secret)))
    _req(s.secret == s.secret.strip(), "server.secret must not start or end with whitespace")
    _req(bool(s.host.strip()), "server.host must not be empty")
    _req(0 <= s.port <= 65535, "server.port must be between 0 and 65535 (got %d)" % s.port)
    _req(s.path.startswith("/") and " " not in s.path,
         "server.path must start with '/' and contain no spaces (got %r)" % s.path)
    for ip in s.tradingview_ips:
        try:
            ipaddress.ip_address(ip.strip())
        except ValueError:
            raise ConfigError("server.tradingview_ips: %r is not a valid IP address" % ip)
    _req(s.max_body_bytes >= 256, "server.max_body_bytes must be >= 256 (got %d)" % s.max_body_bytes)
    _req(s.max_signal_age_s > 0, "server.max_signal_age_s must be > 0")
    _req(s.max_future_skew_s >= 0, "server.max_future_skew_s must be >= 0")
    _req(s.rate_limit_per_min >= 1, "server.rate_limit_per_min must be >= 1")

    a = cfg.account
    _req(a.initial_balance > 0, "account.initial_balance must be > 0")
    if isinstance(a.server_utc_offset_hours, str):
        _req(a.server_utc_offset_hours == "auto",
             "account.server_utc_offset_hours must be a number of hours or \"auto\" (got %r)"
             % a.server_utc_offset_hours)
    else:
        _req(-12 <= a.server_utc_offset_hours <= 14,
             "account.server_utc_offset_hours must be between -12 and 14 (got %s)" % a.server_utc_offset_hours)

    r = cfg.risk
    _req(r.daily_loss_pct > 0, "risk.daily_loss_pct must be > 0")
    _req(r.max_loss_pct > 0, "risk.max_loss_pct must be > 0")
    _req(0 < r.risk_per_trade_pct <= r.max_risk_per_trade_pct <= 3,
         "risk: need 0 < risk_per_trade_pct (%s) <= max_risk_per_trade_pct (%s) <= 3"
         % (r.risk_per_trade_pct, r.max_risk_per_trade_pct))
    _req(0 <= r.daily_buffer_pct < r.daily_loss_pct,
         "risk: need 0 <= daily_buffer_pct (%s) < daily_loss_pct (%s)" % (r.daily_buffer_pct, r.daily_loss_pct))
    _req(0 <= r.max_buffer_pct < r.max_loss_pct,
         "risk: need 0 <= max_buffer_pct (%s) < max_loss_pct (%s)" % (r.max_buffer_pct, r.max_loss_pct))
    _req(r.kill_buffer_pct >= 0, "risk.kill_buffer_pct must be >= 0")
    _req(r.max_total_open_risk_pct > 0, "risk.max_total_open_risk_pct must be > 0")
    _req(0 <= r.stale_reference_buffer_pct <= 10,
         "risk.stale_reference_buffer_pct must be between 0 and 10 (got %s)" % (r.stale_reference_buffer_pct,))
    _req(r.max_open_positions >= 1, "risk.max_open_positions must be >= 1 (got %d)" % r.max_open_positions)
    _req(r.max_trades_per_day >= 0, "risk.max_trades_per_day must be >= 0")
    _req(r.max_lots > 0, "risk.max_lots must be > 0")
    _req(r.commission_per_lot_usd >= 0, "risk.commission_per_lot_usd must be >= 0")
    _req(r.slippage_buffer_pct >= 0, "risk.slippage_buffer_pct must be >= 0")
    _req(r.equity_max_age_s > 0, "risk.equity_max_age_s must be > 0")
    _req(r.entry_max_delay_s > 0, "risk.entry_max_delay_s must be > 0")
    _req(r.min_hold_s_for_signal_close >= 0, "risk.min_hold_s_for_signal_close must be >= 0")
    _check_hhmm(r.trading_start_server, "risk.trading_start_server")
    _check_hhmm(r.trading_end_server, "risk.trading_end_server")
    if r.friday_cutoff_server is not None:
        _check_hhmm(r.friday_cutoff_server, "risk.friday_cutoff_server")
    for d in r.trading_days_server:
        _req(0 <= d <= 6, "risk.trading_days_server: %d is not a weekday number (Mon=0 .. Sun=6)" % d)

    sym = cfg.symbols
    _req(" " not in sym.suffix, "symbols.suffix must not contain spaces")
    _req(bool(sym.specs), "symbols.specs must define at least one symbol")
    for name, spec in sym.specs.items():
        w = "symbols.specs.%s" % name
        _req(spec.contract_size > 0, "%s.contract_size must be > 0" % w)
        _req(len(spec.quote) == 3 and spec.quote.isalpha(),
             "%s.quote must be a 3-letter currency code (got %r)" % (w, spec.quote))
        _req(0 <= spec.digits <= 10, "%s.digits must be between 0 and 10" % w)
        _req(spec.point > 0, "%s.point must be > 0" % w)
        _req(spec.lot_step > 0, "%s.lot_step must be > 0" % w)
        _req(spec.min_lot > 0, "%s.min_lot must be > 0" % w)
        _req(spec.min_sl_points >= 0, "%s.min_sl_points must be >= 0" % w)
        _req(spec.commission_per_lot_usd is None or spec.commission_per_lot_usd >= 0,
             "%s.commission_per_lot_usd must be >= 0 or null (null = risk.commission_per_lot_usd)" % w)
    for k, v in sym.map.items():
        _req(bool(v), "symbols.map.%s must be a non-empty MT5 symbol" % k)
    for a_sym in sym.allowed:
        _req(a_sym in sym.specs,
             "symbols.allowed: %r has no entry in symbols.specs (add a spec or remove it)" % a_sym)

    e = cfg.executor
    _req(e.mode in EXECUTOR_MODES,
         "executor.mode must be one of %s (got %r)" % (", ".join(EXECUTOR_MODES), e.mode))
    _req(e.paper_start_balance is None or e.paper_start_balance > 0,
         "executor.paper_start_balance must be > 0 or null")
    g = e.gui
    _req(bool(g.owner_names), "executor.gui.owner_names must not be empty")
    _req(bool(g.order_dialog_title_contains), "executor.gui.order_dialog_title_contains must not be empty")
    _req(bool(g.position_dialog_title_contains), "executor.gui.position_dialog_title_contains must not be empty")
    for k in ("dialog_timeout_s", "result_timeout_s", "account_poll_s"):
        _req(getattr(g, k) > 0, "executor.gui.%s must be > 0" % k)
    _req(g.action_delay_s >= 0, "executor.gui.action_delay_s must be >= 0")
    _req(g.size_tolerance_px >= 0, "executor.gui.size_tolerance_px must be >= 0")
    _req(0 <= g.ocr_min_confidence <= 1, "executor.gui.ocr_min_confidence must be between 0 and 1")
    _req(g.keep_screenshots_days >= 0, "executor.gui.keep_screenshots_days must be >= 0")
    _req(str(g.open_order_via).lower() in ("f9", "toolbar"), "executor.gui.open_order_via must be \"f9\" or \"toolbar\"")
    _req(0 < g.volume_field_offset_px <= 500, "executor.gui.volume_field_offset_px must be > 0 and <= 500")
    _req(0 < g.lock_timeout_s <= 600, "executor.gui.lock_timeout_s must be > 0 and <= 600")

    m = cfg.mirror
    _req(m.units_per_lot > 0, "mirror.units_per_lot must be > 0 (got %s)" % m.units_per_lot)
    _req(m.stop_distance > 0, "mirror.stop_distance must be > 0 (got %s)" % m.stop_distance)
    _req(0 <= m.idea_risk_pct <= 3, "mirror.idea_risk_pct must be between 0 and 3 (got %s)" % m.idea_risk_pct)
    _req(m.tp_distance >= 0, "mirror.tp_distance must be >= 0 (0 = no take-profit)")
    _req(m.size_tolerance_lots >= 0, "mirror.size_tolerance_lots must be >= 0")
    _req(m.reenter_cooldown_s >= 0, "mirror.reenter_cooldown_s must be >= 0")
    _req(0 <= int(m.reenter_max) <= 10, "mirror.reenter_max must be between 0 and 10 (got %s)" % (m.reenter_max,))
    _req(0 < m.max_price_gap_pct <= 100, "mirror.max_price_gap_pct must be > 0 and <= 100")
    for k, v in m.units_per_lot_by_symbol.items():
        _req(k in sym.specs, "mirror.units_per_lot_by_symbol: %r has no entry in symbols.specs "
                             "(use a symbols.specs key or its MT5 name)" % k)
        _req(v > 0, "mirror.units_per_lot_by_symbol.%s must be > 0 (got %s)" % (k, v))
    for k, v in m.stop_distance_by_symbol.items():
        _req(k in sym.specs, "mirror.stop_distance_by_symbol: %r has no entry in symbols.specs "
                             "(use a symbols.specs key or its MT5 name)" % k)
        _req(v > 0, "mirror.stop_distance_by_symbol.%s must be > 0 (got %s)" % (k, v))
    for k, v in m.tp_distance_by_symbol.items():
        _req(k in sym.specs, "mirror.tp_distance_by_symbol: %r has no entry in symbols.specs "
                             "(use a symbols.specs key or its MT5 name)" % k)
        _req(v >= 0, "mirror.tp_distance_by_symbol.%s must be >= 0 (0 = no take-profit; got %s)" % (k, v))
    for k, v in m.quote_usd_by_symbol.items():
        _req(k in sym.specs, "mirror.quote_usd_by_symbol: %r has no entry in symbols.specs "
                             "(use a symbols.specs key or its MT5 name)" % k)
        _req(v > 0, "mirror.quote_usd_by_symbol.%s must be > 0 (got %s)" % (k, v))
    for k, v in m.max_price_gap_pct_by_symbol.items():
        _req(k in sym.specs, "mirror.max_price_gap_pct_by_symbol: %r has no entry in symbols.specs "
                             "(use a symbols.specs key or its MT5 name)" % k)
        _req(0 < v <= 100, "mirror.max_price_gap_pct_by_symbol.%s must be > 0 and <= 100 (got %s)" % (k, v))
    for k, targets in m.fan_out.items():
        w = "mirror.fan_out.%s" % k
        _req(len(targets) >= 1, "%s must list at least one target symbol (or remove the entry)" % w)
        seen = set()  # type: set
        for t in targets:
            _req(t in sym.specs, "%s: target %r has no entry in symbols.specs (add a spec, or use a "
                                 "symbols.specs key or its MT5 name)" % (w, t))
            _req(t not in seen, "%s: target %r is listed more than once" % (w, t))
            seen.add(t)

    n = cfg.notify
    _req(n.min_level in NOTIFY_LEVELS,
         "notify.min_level must be one of %s (got %r)" % (", ".join(NOTIFY_LEVELS), n.min_level))
    _req(not n.ntfy_url or n.ntfy_url.startswith(("http://", "https://")),
         "notify.ntfy_url must start with http:// or https://")

    _req("://" not in cfg.ngrok.domain and "/" not in cfg.ngrok.domain,
         "ngrok.domain must be a bare host name without scheme or path, e.g. 'your-name.ngrok-free.app'")
