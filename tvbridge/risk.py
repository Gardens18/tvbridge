"""Risk guard (SPEC section 9): pure functions, no I/O.

Every entry signal passes through :func:`plan_entry`, which either approves it with a
position size or rejects it with ``"CODE: human text"``. The checks run in the exact
order given in the spec and stop at the first failure.

Design principles:

* **Fail closed.** Anything unknown, missing, non-finite or inconsistent blocks the entry.
  :func:`plan_entry` never raises; an unexpected internal error is returned as a rejected
  plan (code ``RISK_ERROR``) rather than propagated.
* **Never round size up.** Lots are floored to the lot step with :mod:`decimal`, so a
  computed 0.29999999 lots becomes 0.29, never 0.30.
* **Conservative references.** A higher daily reference means a tighter floor, so
  :func:`estimate_reference` always takes the highest candidate.

The $50,000 Hantec Endurance example (defaults, reference 50,000):
hard daily floor 48,000, static max floor 46,000, internal daily 48,500, internal max
46,500, entry floor 48,500, kill floor 48,150.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import Any, Dict, List, Optional, Tuple

from . import clock
from .config import Config, SymbolSpec, normalize_tv_symbol
from .models import SIDES, AccountSnapshot, ObservedPosition, Signal, TradePlan, opposite_side

__all__ = [
    "Floors",
    "RiskState",
    "compute_floors",
    "quote_to_usd",
    "per_lot_loss_usd",
    "round_lots_down",
    "check_trading_window",
    "plan_entry",
    "evaluate_kill",
    "estimate_reference",
    "untracked_risk_usd",
]

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# --------------------------------------------------------------------------- small helpers


def _finite(x: Any) -> bool:
    """True for a real, finite number (bools and None are rejected)."""
    if x is None or isinstance(x, bool):
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _dec(x: float) -> Decimal:
    """Decimal from the shortest round-tripping repr of a float (0.29999999 -> '0.29999999').

    Using ``repr`` rather than the exact binary value means 0.3 is treated as 0.3, which is
    what a human (and the broker) means; it never inflates a value beyond its repr.
    """
    return Decimal(repr(float(x)))


def _get(row: Any, key: str) -> Any:
    """``row[key]`` for dicts and sqlite3.Row alike; None when the key is absent."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        getter = getattr(row, "get", None)
        if callable(getter):
            return getter(key)
        return None


def _norm_side(side: Any) -> str:
    return str(side or "").strip().lower()


def _usd(x: float) -> str:
    return "%.2f" % x


# --------------------------------------------------------------------------- floors


@dataclass
class Floors:
    """Equity levels for one server day."""

    reference: float            # max(prev EOD balance, prev EOD equity) at 00:00 server time
    hard_daily_floor: float     # reference * (1 - daily_loss_pct/100)
    hard_max_floor: float       # initial_balance * (1 - max_loss_pct/100)   (static)
    internal_daily_floor: float # reference * (1 - (daily_loss_pct - daily_buffer_pct)/100)
    internal_max_floor: float   # initial_balance * (1 - (max_loss_pct - max_buffer_pct)/100)
    entry_floor: float          # max(internal_daily_floor, internal_max_floor)
    kill_floor: float           # max(hard_daily_floor, hard_max_floor) + initial_balance * kill_buffer_pct/100

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


def compute_floors(reference: float, cfg: Config) -> Floors:
    """Floors for a day whose daily-loss reference is ``reference``.

    Raises ValueError if ``reference`` is not a finite number > 0.
    """
    if not _finite(reference) or float(reference) <= 0:
        raise ValueError("daily reference must be a finite number > 0 (got %r)" % (reference,))
    ref = float(reference)
    r = cfg.risk
    initial = float(cfg.account.initial_balance)
    hard_daily = ref * (1.0 - r.daily_loss_pct / 100.0)
    hard_max = initial * (1.0 - r.max_loss_pct / 100.0)
    internal_daily = ref * (1.0 - (r.daily_loss_pct - r.daily_buffer_pct) / 100.0)
    internal_max = initial * (1.0 - (r.max_loss_pct - r.max_buffer_pct) / 100.0)
    return Floors(
        reference=ref,
        hard_daily_floor=hard_daily,
        hard_max_floor=hard_max,
        internal_daily_floor=internal_daily,
        internal_max_floor=internal_max,
        entry_floor=max(internal_daily, internal_max),
        kill_floor=max(hard_daily, hard_max) + initial * r.kill_buffer_pct / 100.0,
    )


def _floors_valid(f: Floors) -> bool:
    vals = (f.reference, f.hard_daily_floor, f.hard_max_floor, f.internal_daily_floor,
            f.internal_max_floor, f.entry_floor, f.kill_floor)
    return all(_finite(v) for v in vals) and float(f.reference) > 0


# --------------------------------------------------------------------------- sizing primitives


def quote_to_usd(tv_symbol: str, spec: SymbolSpec, price: float,
                 signal_quote_usd: Optional[float]) -> Optional[float]:
    """USD value of one unit of the symbol's quote currency, or None if unknown.

    * quote USD (EURUSD, XAUUSD, ...)                      -> 1.0
    * USD base with matching quote (USDJPY, USDCAD, ...)   -> 1 / price
    * otherwise the alert's ``quote_usd`` if it is > 0     -> that value (crosses)
    * else None (the entry is refused with NO_FX_RATE).
    """
    quote = (spec.quote or "").strip().upper()
    if quote == "USD":
        return 1.0
    sym = normalize_tv_symbol(tv_symbol, "")
    if sym.startswith("USD") and len(sym) >= 6 and quote == sym[3:6]:
        if _finite(price) and float(price) > 0:
            return 1.0 / float(price)
    if _finite(signal_quote_usd) and float(signal_quote_usd) > 0:
        return float(signal_quote_usd)
    return None


def per_lot_loss_usd(entry: float, sl: float, spec: SymbolSpec, q2usd: float,
                     commission_per_lot: float) -> float:
    """Loss in USD of 1 lot stopped out at ``sl``, including round-trip commission."""
    return abs(float(entry) - float(sl)) * float(spec.contract_size) * float(q2usd) + float(commission_per_lot)


def round_lots_down(lots: float, step: float, min_lot: float) -> float:
    """Floor ``lots`` to a multiple of ``step`` using Decimal; 0.0 if below ``min_lot``.

    Never rounds up: 0.29999999 with step 0.01 -> 0.29. Non-finite or non-positive
    inputs (and an invalid step) give 0.0, which callers treat as "too small".
    """
    if not (_finite(lots) and _finite(step) and _finite(min_lot)):
        return 0.0
    if float(lots) <= 0 or float(step) <= 0:
        return 0.0
    try:
        d_lots, d_step, d_min = _dec(lots), _dec(step), _dec(min_lot)
        with localcontext() as ctx:
            ctx.rounding = ROUND_FLOOR          # even the division itself never rounds up
            steps = (d_lots / d_step).to_integral_value(rounding=ROUND_FLOOR)
            floored = steps * d_step
    except (InvalidOperation, ValueError, ArithmeticError):
        return 0.0
    if floored <= 0 or floored < d_min:
        return 0.0
    return float(floored)


# --------------------------------------------------------------------------- state & window


@dataclass
class RiskState:
    """Everything :func:`plan_entry` needs to know about the account right now."""

    now: datetime
    snapshot: Optional[AccountSnapshot]
    floors: Optional[Floors]
    open_positions: List[dict]                 # ledger rows (symbol, side, lots, risk_usd, ...)
    observed_positions: Optional[List[ObservedPosition]]   # latest parsed from MT5, None = unknown
    untracked_positions: List[ObservedPosition]            # observed on MT5 but not in ledger
    trades_today: int
    paused: bool
    halted: str                                # "" when not halted
    # (integration) engine-supplied doubts; empty/None = nothing to report
    positions_uncertain: str = ""              # ledger rows not visible in MT5 / position list incomplete
    sl_issues: List[str] = field(default_factory=list)   # open positions without a stop-loss on the server
    reference_note: str = ""                   # today's reference is a stale estimate (set-reference needed)
    confirmed_balance: Optional[float] = None  # min(balance of the last two reads), for the worst case
    confirmed_equity: Optional[float] = None   # min(equity of the last two reads)


def check_trading_window(now_utc: datetime, cfg: Config) -> Optional[str]:
    """None if new entries are allowed at ``now_utc``, else ``"WINDOW: ..."``.

    Everything is evaluated in broker server time (``Config.server_offset_at(now_utc)``):
    the weekday must be in ``trading_days_server``; the time must be within
    [trading_start_server, trading_end_server) (a start later than the end means an
    overnight window; start == end means no window at all); on Friday entries stop at
    ``friday_cutoff_server`` (None disables the cutoff).
    """
    r = cfg.risk
    server_now = clock.to_server(now_utc, cfg.server_offset_at(now_utc))
    wd = server_now.weekday()
    stamp = "%s %s server time" % (_DAY_NAMES[wd], server_now.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        start = clock.parse_hhmm(r.trading_start_server)
        end = clock.parse_hhmm(r.trading_end_server)
        cutoff = clock.parse_hhmm(r.friday_cutoff_server) if r.friday_cutoff_server is not None else None
    except ValueError as e:
        return "WINDOW: invalid trading hours in config (%s)" % e

    if wd not in r.trading_days_server:
        return "WINDOW: %s is not a trading day (trading_days_server=%s)" % (stamp, list(r.trading_days_server))

    t = server_now.time()  # naive wall-clock time in server tz
    if start < end:
        if t < start:
            return "WINDOW: %s is before trading start %s" % (stamp, r.trading_start_server)
        if t >= end:
            return "WINDOW: %s is after trading end %s" % (stamp, r.trading_end_server)
    elif start > end:
        if end <= t < start:
            return "WINDOW: %s is outside the overnight window %s-%s" % (
                stamp, r.trading_start_server, r.trading_end_server)
    else:
        return "WINDOW: empty trading window (trading_start_server == trading_end_server == %s)" % (
            r.trading_start_server,)

    if cutoff is not None and wd == 4 and t >= cutoff:
        return "WINDOW: %s is after the Friday cutoff %s" % (stamp, r.friday_cutoff_server)
    return None


# --------------------------------------------------------------------------- plan_entry


def _reject(code: str, text: str, details: Optional[Dict[str, Any]] = None) -> TradePlan:
    return TradePlan(False, "%s: %s" % (code, text), details=dict(details or {}))


def _symbol_key(cfg: Config, symbol: Any) -> str:
    """Canonical key so "EURUSD", "EURUSD.h" and "eurusd.H" compare equal."""
    s = str(symbol or "")
    return cfg.tv_symbol_for(s) or cfg.normalize_tv_symbol(s)


def _row_to_observed(row: Any) -> ObservedPosition:
    def opt(key: str) -> Optional[float]:
        v = _get(row, key)
        return float(v) if _finite(v) else None

    lots = _get(row, "lots")
    ticket = _get(row, "ticket")
    return ObservedPosition(
        symbol=str(_get(row, "symbol") or ""),
        side=_norm_side(_get(row, "side")),
        lots=float(lots) if _finite(lots) else 0.0,
        ticket=None if ticket in (None, "") else str(ticket),
        open_price=opt("entry_price"),
        sl=opt("sl"),
        tp=opt("tp"),
    )


def _ledger_open_risk(rows: List[Any]) -> Tuple[float, List[str]]:
    """(sum of risk_usd over open ledger rows, descriptions of rows whose risk is unknown)."""
    total = 0.0
    bad = []  # type: List[str]
    for row in rows:
        v = _get(row, "risk_usd")
        if not _finite(v) or float(v) < 0:
            bad.append("%s %s (risk_usd=%r)" % (_get(row, "symbol"), _get(row, "side"), v))
            continue
        total += float(v)
    return total, bad


def untracked_risk_usd(p: ObservedPosition, cfg: Config) -> Optional[float]:
    """Worst-case loss of a position tvbridge did not open, if its SL, price and spec are known.

    lots x (|open price - SL| x contract size x quote->USD + commission per lot); None when
    the stop-loss is unknown or missing, or the quote currency cannot be converted.
    """
    spec = cfg.spec_for(p.symbol)
    if spec is None or p.sl_missing or not (_finite(p.sl) and _finite(p.open_price) and _finite(p.lots)):
        return None
    entry, sl = float(p.open_price), float(p.sl)  # type: ignore[arg-type]
    side = _norm_side(p.side)
    if (side == "buy" and sl >= entry) or (side == "sell" and sl <= entry):
        return 0.0 + cfg.commission_per_lot(p.symbol) * float(p.lots)   # stop in profit: commission only
    q2usd = quote_to_usd(cfg.tv_symbol_for(p.symbol) or p.symbol, spec, entry, None)
    if q2usd is None:
        return None
    return float(p.lots) * per_lot_loss_usd(entry, sl, spec, q2usd, cfg.commission_per_lot(p.symbol))


def plan_entry(sig: Signal, state: RiskState, cfg: Config) -> TradePlan:
    """Decide whether an entry signal may be executed and with what size.

    Returns an approved :class:`TradePlan` (lots, risk_usd, close_first, details) or a
    rejected one whose ``reason`` is ``"CODE: text"``. Never raises: unexpected errors
    become ``"RISK_ERROR: ..."`` rejections (fail closed).
    """
    try:
        return _plan_entry(sig, state, cfg)
    except Exception as e:  # noqa: BLE001 - fail closed on any internal error
        return _reject("RISK_ERROR", "internal error while checking risk (%s: %s); entry refused"
                       % (type(e).__name__, e))


def _plan_entry(sig: Signal, state: RiskState, cfg: Config) -> TradePlan:
    r = cfg.risk
    now = clock.ensure_utc(state.now)

    # 1. halted / paused
    if state.halted:
        return _reject("HALTED", "new entries are halted (%s); check MT5, then run `tvbridge resume`"
                       % state.halted)
    if state.paused:
        return _reject("PAUSED", "new entries are paused; run `tvbridge resume`")

    # 2. action
    if sig.action not in SIDES:
        return _reject("BAD_ACTION", "action %r is not an entry (buy/sell)" % (sig.action,))
    side = sig.action
    opp = opposite_side(side)

    # 3. symbol spec
    spec = cfg.spec_for(sig.symbol)
    if spec is None:
        return _reject("NO_SPEC", "no symbols.specs entry for %r" % (sig.symbol,))

    # 4. trading window (server time)
    window = check_trading_window(now, cfg)
    if window:
        return TradePlan(False, window)

    # 5. signal age
    signal_age = (now - clock.ensure_utc(sig.fired_at)).total_seconds()
    if signal_age > r.entry_max_delay_s:
        return _reject("STALE_SIGNAL", "signal fired %.1f s ago (entry_max_delay_s=%s)"
                       % (signal_age, r.entry_max_delay_s))

    # 6. account snapshot
    snap = state.snapshot
    if snap is None:
        return _reject("NO_SNAPSHOT", "no account snapshot yet (balance/equity unknown)")
    snap_age = (now - clock.ensure_utc(snap.ts)).total_seconds()
    if snap_age > r.equity_max_age_s:
        return _reject("SNAPSHOT_STALE", "last account snapshot is %.1f s old (equity_max_age_s=%s)"
                       % (snap_age, r.equity_max_age_s))
    if not (_finite(snap.balance) and _finite(snap.equity) and snap.balance > 0 and snap.equity > 0):
        return _reject("NO_SNAPSHOT", "account snapshot has invalid balance/equity (%r / %r)"
                       % (snap.balance, snap.equity))
    balance = float(snap.balance)
    equity = float(snap.equity)

    # 7. daily reference / floors
    floors = state.floors
    if floors is None or not _floors_valid(floors):
        return _reject("NO_DAY_REFERENCE", "no valid daily reference/floors for today; "
                       "wait for an account read or run `tvbridge set-reference VALUE`")
    if state.reference_note:
        return _reject("NO_DAY_REFERENCE", "%s; check the Hantec dashboard and run `tvbridge set-reference VALUE`"
                       % state.reference_note)

    # 7b. daily profit lock (firms with a best-day / consistency rule)
    lock_pct = float(getattr(r, "daily_profit_lock_pct", 0.0) or 0.0)
    if lock_pct > 0:
        lock_usd = float(cfg.account.initial_balance) * lock_pct / 100.0
        day_profit = balance - float(floors.reference)
        if day_profit >= lock_usd:
            return _reject("DAILY_PROFIT_LOCK", "closed profit today %s >= lock %s (daily_profit_lock_pct=%s); "
                           "no new entries until the next server day" % (_usd(day_profit), _usd(lock_usd), lock_pct))

    # 8. untracked positions
    if r.block_untracked_positions and state.untracked_positions:
        names = ", ".join("%s %s %s" % (p.symbol, p.side, p.lots) for p in state.untracked_positions)
        return _reject("UNTRACKED_POSITIONS", "MT5 shows positions tvbridge did not open: %s" % names)
    # 8b. positions tvbridge cannot see (rows missing without proof of a close, list incomplete)
    if state.positions_uncertain:
        return _reject("POSITIONS_UNCERTAIN", "%s; entries wait until MT5 shows the positions again (or check "
                       "MT5 and run `tvbridge resume`)" % state.positions_uncertain)
    # 8c. an open position has no stop-loss on the server: its loss is unbounded
    if state.sl_issues:
        return _reject("SL_MISSING_ON_SERVER", "%s; set the stop-loss in MT5" % "; ".join(state.sl_issues))

    # 9. price / SL / TP sanity
    price = sig.price
    if price is None or not _finite(price) or float(price) <= 0:
        return _reject("NO_PRICE", "signal has no valid price (%r)" % (price,))
    price = float(price)
    sl = sig.sl
    if sl is None or not _finite(sl) or float(sl) <= 0:
        return _reject("SL_MISSING", "every entry needs a valid stop-loss price (sl=%r)" % (sl,))
    sl = float(sl)
    if side == "buy" and not sl < price:
        return _reject("SL_WRONG_SIDE", "buy SL %s must be below price %s" % (sl, price))
    if side == "sell" and not sl > price:
        return _reject("SL_WRONG_SIDE", "sell SL %s must be above price %s" % (sl, price))
    tp = sig.tp
    if tp:
        if side == "buy" and not tp > price:
            return _reject("TP_WRONG_SIDE", "buy TP %s must be above price %s" % (tp, price))
        if side == "sell" and not (0 < tp < price):
            return _reject("TP_WRONG_SIDE", "sell TP %s must be below price %s" % (tp, price))
    # Decimal so that an exact boundary (e.g. 50 points) is not lost to float error.
    sl_distance = abs(_dec(price) - _dec(sl))
    min_distance = _dec(spec.min_sl_points) * _dec(spec.point)
    if sl_distance < min_distance:
        return _reject("SL_TOO_TIGHT", "SL distance %s is below min_sl_points x point = %s"
                       % (sl_distance, min_distance))

    # 10. reversal (opposite positions on the same symbol)
    key = _symbol_key(cfg, sig.symbol)
    observed = state.observed_positions  # None = unknown
    ledger = list(state.open_positions or [])

    def on_symbol(sym: Any) -> bool:
        return _symbol_key(cfg, sym) == key

    obs_on = [p for p in observed if on_symbol(p.symbol)] if observed is not None else []
    led_on = [row for row in ledger if on_symbol(_get(row, "symbol"))]
    obs_opp = [p for p in obs_on if _norm_side(p.side) == opp]
    led_opp = [row for row in led_on if _norm_side(_get(row, "side")) == opp]
    if observed is not None and obs_opp:
        opposite = list(obs_opp)
    elif led_opp:
        # Observed unknown, or the snapshot predates a just-filled ledger position: use the ledger.
        opposite = [_row_to_observed(row) for row in led_opp]
    else:
        opposite = []
    close_first = []  # type: List[ObservedPosition]
    if opposite:
        if not r.reverse_on_opposite:
            return _reject("OPPOSITE_OPEN", "%d %s position(s) open on %s and reverse_on_opposite is false"
                           % (len(opposite), opp, sig.symbol))
        close_first = opposite

    # 11. pyramiding (same side, same symbol) - either source counts
    same_side = (any(_norm_side(p.side) == side for p in obs_on)
                 or any(_norm_side(_get(row, "side")) == side for row in led_on))
    if same_side and not r.allow_pyramiding:
        return _reject("PYRAMIDING", "a %s position on %s is already open (allow_pyramiding is false)"
                       % (side, sig.symbol))

    # 12. position count & trades per day (positions closed by the reversal are excluded)
    def excluded(sym: Any, s: Any) -> bool:
        return bool(close_first) and on_symbol(sym) and _norm_side(s) == opp

    obs_count = (len([p for p in observed if not excluded(p.symbol, p.side)])
                 if observed is not None else 0)
    led_count = len([row for row in ledger if not excluded(_get(row, "symbol"), _get(row, "side"))])
    open_count = max(obs_count, led_count)
    if open_count >= r.max_open_positions:
        return _reject("MAX_POSITIONS", "%d position(s) open (max_open_positions=%d)"
                       % (open_count, r.max_open_positions))
    if state.trades_today >= r.max_trades_per_day:
        return _reject("MAX_TRADES_DAY", "%d trade(s) today (max_trades_per_day=%d)"
                       % (state.trades_today, r.max_trades_per_day))

    # 13. quote -> USD
    tv_key = cfg.tv_symbol_for(sig.symbol) or sig.tv_symbol
    q2usd = quote_to_usd(tv_key, spec, price, sig.quote_usd)
    if q2usd is None:
        return _reject("NO_FX_RATE", "%s is quoted in %s; send quote_usd (USD value of 1 %s) in the alert"
                       % (sig.symbol, spec.quote, spec.quote))

    # 14. sizing
    base = min(balance, equity)
    requested_pct = sig.risk_pct if sig.risk_pct else r.risk_per_trade_pct
    details = {
        "symbol": sig.symbol,
        "side": side,
        "floors": floors.to_dict(),
        "balance": balance,
        "equity": equity,
        "base": base,
        "q2usd": q2usd,
        "sl_distance": float(sl_distance),
        "close_first": [p.to_dict() for p in close_first],
    }  # type: Dict[str, Any]
    if not _finite(requested_pct) or float(requested_pct) <= 0:
        return _reject("SIZE_TOO_SMALL", "invalid risk_pct %r" % (requested_pct,), details)
    risk_pct = min(float(requested_pct), r.max_risk_per_trade_pct)
    target = base * risk_pct / 100.0
    per_lot = per_lot_loss_usd(price, sl, spec, q2usd, cfg.commission_per_lot(sig.symbol))
    per_lot_buffered = per_lot * (1.0 + r.slippage_buffer_pct / 100.0)
    details.update(risk_pct=risk_pct, target=target, per_lot=per_lot, per_lot_buffered=per_lot_buffered)
    if not _finite(per_lot_buffered) or per_lot_buffered <= 0:
        return _reject("SIZE_TOO_SMALL", "cannot compute loss per lot (%r)" % (per_lot_buffered,), details)
    raw_lots = target / per_lot_buffered
    capped_lots = min(raw_lots, float(r.max_lots))
    lots = round_lots_down(capped_lots, spec.lot_step, spec.min_lot)
    details.update(raw_lots=raw_lots, capped_by_max_lots=raw_lots > r.max_lots, lots=lots)
    if lots == 0:
        return _reject("SIZE_TOO_SMALL", "%.4f lots (risk target %s USD / %s USD per lot incl. buffer) "
                       "rounds below min_lot %s" % (capped_lots, _usd(target), _usd(per_lot_buffered),
                                                    spec.min_lot), details)
    risk_usd = lots * per_lot_buffered
    details["risk_usd"] = risk_usd

    # 15. total open risk (all open ledger rows, including those about to be closed, plus
    #     untracked positions when they are allowed at all)
    open_risk, unknown = _ledger_open_risk(ledger)
    untracked_risk = 0.0
    if not r.block_untracked_positions:
        for p in state.untracked_positions or []:
            u = untracked_risk_usd(p, cfg)
            if u is None:
                unknown.append("untracked %s %s %s with unknown SL/risk" % (p.symbol, p.side, p.lots))
            else:
                untracked_risk += u
    open_risk += untracked_risk
    total_cap = base * r.max_total_open_risk_pct / 100.0
    details.update(open_risk_usd=open_risk, untracked_risk_usd=untracked_risk, total_risk_cap=total_cap)
    if unknown:
        return _reject("TOTAL_OPEN_RISK", "cannot verify total open risk: open position(s) "
                       "without a valid risk_usd: %s" % ", ".join(unknown), details)
    if open_risk + risk_usd > total_cap:
        return _reject("TOTAL_OPEN_RISK", "open risk %s + this trade %s USD exceeds %s USD "
                       "(max_total_open_risk_pct=%s)" % (_usd(open_risk), _usd(risk_usd), _usd(total_cap),
                                                         r.max_total_open_risk_pct), details)

    # 16. equity already at/below the entry floor (the lower of the last two reads: one OCR
    #     misread upwards never approves an entry)
    c_balance, c_equity = balance, equity
    if _finite(state.confirmed_balance):
        c_balance = min(balance, float(state.confirmed_balance))  # type: ignore[arg-type]
    if _finite(state.confirmed_equity):
        c_equity = min(equity, float(state.confirmed_equity))  # type: ignore[arg-type]
    details.update(confirmed_balance=c_balance, confirmed_equity=c_equity)
    if c_equity <= floors.entry_floor:
        return _reject("BELOW_ENTRY_FLOOR", "equity %s <= entry floor %s"
                       % (_usd(c_equity), _usd(floors.entry_floor)), details)

    # 17. worst case: every open SL and this SL hit
    worst_balance = c_balance - open_risk - risk_usd
    worst_equity = c_equity - risk_usd
    details.update(worst_case_balance=worst_balance, worst_case_equity=worst_equity)
    if worst_balance < floors.entry_floor or worst_equity < floors.entry_floor:
        return _reject("WORST_CASE", "worst case balance %s / equity %s would be below entry floor %s"
                       % (_usd(worst_balance), _usd(worst_equity), _usd(floors.entry_floor)), details)

    # 18. approved
    return TradePlan(
        approved=True,
        reason="",
        lots=lots,
        risk_usd=risk_usd,
        per_lot_loss_usd=per_lot,
        close_first=close_first,
        details=details,
    )


# --------------------------------------------------------------------------- kill switch & reference


def evaluate_kill(snapshot: AccountSnapshot, floors: Floors) -> Optional[str]:
    """``"KILL: ..."`` if equity is at or below the kill floor, else None."""
    if snapshot is None or floors is None:
        return None
    if not (_finite(snapshot.equity) and _finite(floors.kill_floor)):
        return None
    equity = float(snapshot.equity)
    if equity <= floors.kill_floor:
        return ("KILL: equity %s <= kill floor %s (hard daily floor %s, hard max floor %s)"
                % (_usd(equity), _usd(floors.kill_floor), _usd(floors.hard_daily_floor),
                   _usd(floors.hard_max_floor)))
    return None


def estimate_reference(last_before_midnight: Optional[AccountSnapshot], today_snaps: List[AccountSnapshot],
                       initial_balance: float, have_history: bool) -> Optional[float]:
    """Best (highest, hence most conservative) estimate of today's daily-loss reference.

    Candidates: balance and equity of the last snapshot before server midnight, balance
    and equity of every snapshot taken today, and ``initial_balance`` when there is no
    history at all. Non-finite values are ignored. None when there is no candidate.
    """
    candidates = []  # type: List[float]
    snaps = []  # type: List[AccountSnapshot]
    if last_before_midnight is not None:
        snaps.append(last_before_midnight)
    snaps.extend(s for s in (today_snaps or []) if s is not None)
    for s in snaps:
        for v in (s.balance, s.equity):
            if _finite(v):
                candidates.append(float(v))
    if not have_history and _finite(initial_balance):
        candidates.append(float(initial_balance))
    return max(candidates) if candidates else None
