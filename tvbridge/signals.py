"""TradingView alert payload -> validated :class:`~tvbridge.models.Signal`.

Pure functions (no I/O). The webhook server calls them in this order::

    d = decode_body(body)                      # SignalError("BAD_JSON")
    check_secret(d, cfg)                       # False -> HTTP 401
    sig = parse_payload(d, cfg, received_at)   # SignalError(code)
    check_freshness(sig, now, cfg)             # "STALE" | "FUTURE" | None

Recommended payload (see tradingview/ALERTS.md)::

    {"secret": "...", "time": "{{timenow}}", "symbol": "{{ticker}}", "price": {{close}},
     "action": "buy", "sl": 1.08100, "tp": 1.08900, "id": "optional-unique", "strategy": "name"}

or, for Pine strategies, the per-order fields inside ``"order"`` (a JSON object, or a
string containing one), which are merged over the outer fields.

Reason codes raised as :class:`SignalError`: BAD_JSON, BAD_ACTION, NO_SYMBOL,
SYMBOL_NOT_ALLOWED, BAD_NUMBER, NO_TIME, BAD_POSITION, BAD_SIZE.

Mirror mode (action ``"sync"``): one strategy alert that fires on every order fill reports
the strategy's resulting position (``position`` long/short/flat and ``size`` in strategy
units); the engine makes the MT5 position on that symbol match it.
"""

import hashlib
import hmac
import json
import math
import re
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from . import clock
from .config import Config
from .models import Signal

#: Keys that carry the shared secret. Never stored, logged or echoed.
SECRET_KEYS = ("secret", "passphrase")

#: action aliases (keys are lowercased, with spaces/hyphens folded to "_").
ACTION_ALIASES = {
    "buy": "buy",
    "long": "buy",
    "sell": "sell",
    "short": "sell",
    "close": "close",
    "exit": "close",
    "flat": "close",
    "close_position": "close",
    "close_all": "close_all",
    "closeall": "close_all",
    "flatten": "close_all",
    "flatten_all": "close_all",
    "sync": "sync",
}  # type: Dict[str, str]

SIDE_ALIASES = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}  # type: Dict[str, str]

# Field aliases, in precedence order.
PRICE_KEYS = ("price", "close")
SL_KEYS = ("sl", "stop", "stop_loss")
TP_KEYS = ("tp", "take_profit", "limit")
RISK_KEYS = ("risk_pct", "risk")
QUOTE_USD_KEYS = ("quote_usd",)
SYMBOL_KEYS = ("symbol", "ticker")
TIME_KEYS = ("time", "timenow", "fired")
POSITION_KEYS = ("market_position", "position")
# sync (mirror mode)
SYNC_POSITION_KEYS = ("position", "market_position")
SYNC_SIZE_KEYS = ("size", "market_position_size")
SYNC_POSITIONS = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell", "flat": None}  # type: Dict[str, Optional[str]]

MAX_ID_LEN = 128
MAX_STRATEGY_LEN = 64
MAX_COMMENT_LEN = 24
DEFAULT_COMMENT = "tvb"
HASH_ID_LEN = 32
#: Deepest nesting of objects/arrays accepted in a payload (a real alert uses 2).
MAX_JSON_DEPTH = 16

# Plain decimal / scientific notation only: no "inf", "1_000", "0x10", thousands separators.
_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_NAN_STRINGS = ("nan", "+nan", "-nan")
_BOM = "﻿"


class SignalError(Exception):
    """A payload was refused. ``code`` is the reason code (e.g. "BAD_NUMBER")."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message) if self.message else self.code


# --------------------------------------------------------------------------- body & auth


def decode_body(body: bytes) -> Dict[str, Any]:
    """Decode a webhook body: UTF-8 (a leading BOM is stripped), one JSON object.

    Raises ``SignalError("BAD_JSON")`` for anything else (invalid UTF-8, invalid JSON,
    an empty body, or JSON that is not an object).
    """
    if isinstance(body, str):
        text = body
    else:
        try:
            text = bytes(body).decode("utf-8")
        except (UnicodeDecodeError, TypeError, ValueError):
            raise SignalError("BAD_JSON", "body is not valid UTF-8")
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    if not text.strip():
        raise SignalError("BAD_JSON", "empty body")
    d = _loads(text, "body")
    if not isinstance(d, dict):
        raise SignalError("BAD_JSON", "expected a JSON object, got %s" % _json_type(d))
    return d


def _loads(text: str, what: str) -> Any:
    """json.loads with BAD_JSON errors and a nesting limit (keeps later code non-recursive-safe)."""
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise SignalError("BAD_JSON", "%s is not valid JSON: %s" % (what, e))
    except RecursionError:
        raise SignalError("BAD_JSON", "%s is nested too deeply" % what)
    if _depth_exceeds(obj, MAX_JSON_DEPTH):
        raise SignalError("BAD_JSON", "%s is nested deeper than %d levels" % (what, MAX_JSON_DEPTH))
    return obj


def _depth_exceeds(obj: Any, limit: int) -> bool:
    """True if ``obj`` nests dicts/lists more than ``limit`` levels deep (iterative)."""
    stack = [(obj, 1)]
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, dict):
            children = list(cur.values())
        elif isinstance(cur, list):
            children = cur
        else:
            continue
        if depth > limit:
            return True
        stack.extend((c, depth + 1) for c in children if isinstance(c, (dict, list)))
    return False


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "a boolean"
    if isinstance(v, (int, float)):
        return "a number"
    if isinstance(v, str):
        return "a string"
    if isinstance(v, list):
        return "an array"
    return type(v).__name__


def check_secret(d: Dict[str, Any], cfg: Config) -> bool:
    """True if ``d["secret"]`` (or ``d["passphrase"]``) equals ``cfg.server.secret``.

    Compared with :func:`hmac.compare_digest` on UTF-8 bytes (constant time). False when
    the secret is missing, empty, not a string, or when no secret is configured.
    """
    expected = getattr(cfg.server, "secret", "") or ""
    provided = None  # type: Any
    if isinstance(d, dict):
        for key in SECRET_KEYS:
            v = d.get(key)
            if v is not None and v != "":
                provided = v
                break
    if not isinstance(provided, str) or not isinstance(expected, str):
        # Still run a comparison so a missing secret costs the same as a wrong one.
        hmac.compare_digest(b"\x00" * 32, b"\x01" * 32)
        return False
    ok = hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    return bool(ok) and bool(expected)


def strip_secrets(d: Any, _depth: int = 0) -> Any:
    """Copy of ``d`` without ``secret``/``passphrase`` keys (any case), also inside
    nested dicts/lists up to :data:`MAX_JSON_DEPTH` levels."""
    if _depth > MAX_JSON_DEPTH:
        return d
    if isinstance(d, dict):
        return {k: strip_secrets(v, _depth + 1) for k, v in d.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(d, list):
        return [strip_secrets(v, _depth + 1) for v in d]
    return d


# --------------------------------------------------------------------------- helpers


def _parse_order(value: Any) -> Optional[Dict[str, Any]]:
    """The ``order`` field as a dict, or None when absent/empty.

    Accepts a JSON object or a string containing one (also a JSON string that itself
    contains an encoded object). Anything else is ``BAD_JSON``: a malformed order is a
    broken alert, and guessing what it meant is not safe.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        cur = value  # type: Any
        for _ in range(2):  # tolerate one level of double encoding
            s = cur.strip()
            if s.startswith(_BOM):
                s = s[len(_BOM):].strip()
            if not s:
                return None
            cur = _loads(s, "'order'")
            if isinstance(cur, dict):
                return cur
            if not isinstance(cur, str):
                break
        raise SignalError("BAD_JSON", "'order' must be a JSON object, got %s" % _json_type(cur))
    raise SignalError("BAD_JSON", "'order' must be a JSON object, got %s" % _json_type(value))


def _short(v: Any, n: int = 40) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def _first(d: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[str], Any]:
    """(key, value) of the first key present with a non-empty value, else (None, None)."""
    for k in keys:
        if k in d:
            v = d[k]
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            return k, v
    return None, None


def _num(v: Any, zero_is_none: bool = False) -> Optional[float]:
    """Parse a payload number.

    Accepts int/float and numeric strings (surrounding whitespace allowed). Returns None
    for null, "", NaN (number or "nan"/"NaN" string) and, when ``zero_is_none`` (sl/tp:
    0 means "not set"), for zero. Raises ValueError for anything else, including
    booleans, infinities and strings like "1,085" or "abc".
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError("boolean is not a number")
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except OverflowError:
            raise ValueError("number out of range")
    elif isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in _NAN_STRINGS:
            return None
        if not _NUM_RE.match(s):
            raise ValueError("not a number")
        f = float(s)
    else:
        raise ValueError("not a number")
    if math.isnan(f):
        return None
    if math.isinf(f):
        raise ValueError("not a finite number")
    if zero_is_none and f == 0.0:
        return None
    return f


def _field_num(d: Dict[str, Any], keys: Iterable[str], zero_is_none: bool, strict: bool) -> Optional[float]:
    """Value of the first alias in ``keys`` that holds a set number.

    Aliases that are present but unset (null, "", NaN, 0 for sl/tp) are skipped. A
    non-numeric value in *any* present alias raises ``SignalError("BAD_NUMBER")`` when
    ``strict`` (entries: a broken field means a broken alert); otherwise (exit signals,
    which must never be blocked by an irrelevant field) it is ignored.
    """
    found = None  # type: Optional[float]
    for k in keys:
        if k not in d:
            continue
        try:
            f = _num(d[k], zero_is_none=zero_is_none)
        except ValueError as e:
            if strict:
                raise SignalError("BAD_NUMBER", "%s: %s is not a valid number (%s)" % (k, _short(d[k]), e))
            continue
        if found is None:
            found = f
    return found


def _norm_word(v: Any) -> str:
    """Lowercase, strip, fold spaces/hyphens into "_" ("Close All" -> "close_all")."""
    if v is None:
        return ""
    s = str(v).strip().lower()
    return re.sub(r"[\s\-]+", "_", s)


def _resolve_action(d: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Apply the action rules (aliases, market_position). Returns (action, inferred close side).

    * no action and position "flat" -> close (both sides);
    * buy/sell with market_position "flat" -> close of the side the order closed: a "sell"
      that leaves the strategy flat closed a long ("buy"), a "buy" closed a short;
    * sell while market_position is still "long" (or buy while "short") is a partial exit,
      which tvbridge cannot do: BAD_ACTION ("PARTIAL_EXIT ...") instead of a new entry.
    """
    _, raw_action = _first(d, ("action",))
    _, raw_pos = _first(d, POSITION_KEYS)
    position = _norm_word(raw_pos)
    if raw_action is None:
        if position == "flat":
            return "close", None
        raise SignalError("BAD_ACTION", "missing 'action'")
    action = ACTION_ALIASES.get(_norm_word(raw_action))
    if action is None:
        raise SignalError("BAD_ACTION", "unknown action %s" % _short(raw_action))
    if action == "sync":
        return action, None
    if action in ("buy", "sell") and position == "flat":
        # TradingView strategy exit: an order that leaves the strategy flat closes.
        return "close", ("buy" if action == "sell" else "sell")
    if (action, position) in (("sell", "long"), ("buy", "short")):
        raise SignalError("BAD_ACTION", "PARTIAL_EXIT: %s while the strategy is still %s (a partial exit); "
                          "tvbridge cannot close part of a position, so this alert was refused and the position "
                          "stays open" % (action, position))
    return action, None


def peek_action(d: Dict[str, Any]) -> Optional[str]:
    """Best guess of an alert's action without raising (for rate limiting and notifications).

    Returns "close"/"close_all"/"buy"/"sell"/"sync", "partial_exit" for a refused partial exit, or
    None when it cannot be told.
    """
    if not isinstance(d, dict):
        return None
    merged = d
    try:
        order = _parse_order(d.get("order"))
    except SignalError:
        order = None
    if order is not None:
        merged = dict(d)
        merged.update(order)
    try:
        return _resolve_action(merged)[0]
    except SignalError as e:
        return "partial_exit" if "PARTIAL_EXIT" in (e.message or "") else None


def _hash_id(action: str, symbol: str, side: Optional[str], price: Optional[float],
             sl: Optional[float], tp: Optional[float], fired_at: datetime, strategy: str) -> str:
    canon = json.dumps(
        [action, symbol, side, price, sl, tp, clock.iso(fired_at), strategy],
        separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:HASH_ID_LEN]


def _sync_target(d: Dict[str, Any]) -> Tuple[Optional[str], Optional[float]]:
    """(target side, target units) of a sync alert: ("buy"|"sell", units > 0) or (None, None) = flat.

    BAD_POSITION unless position is long/short/flat; BAD_SIZE unless a non-flat position
    comes with a positive size (the absolute value is used). A flat target ignores the size
    field entirely (an exit must never be blocked by an irrelevant field).
    """
    _, raw_pos = _first(d, SYNC_POSITION_KEYS)
    word = _norm_word(raw_pos)
    if raw_pos is None or word not in SYNC_POSITIONS:
        raise SignalError("BAD_POSITION", "sync needs 'position' long, short or flat, got %s" % _short(raw_pos))
    side = SYNC_POSITIONS[word]
    if side is None:
        return None, None
    key, raw_size = _first(d, SYNC_SIZE_KEYS)
    try:
        size = _num(raw_size)
    except ValueError as e:
        raise SignalError("BAD_SIZE", "%s: %s is not a valid number (%s)" % (key, _short(raw_size), e))
    if size is None or abs(size) <= 0:
        raise SignalError("BAD_SIZE", "sync with position %s needs a positive 'size', got %s"
                          % (word, _short(raw_size)))
    return side, abs(size)


def _sync_id(symbol: str, side: Optional[str], units: Optional[float], fired_at: datetime,
             d: Dict[str, Any], price: Optional[float]) -> str:
    """Content hash of a sync alert: position, size, time and order id (plus the order's action,
    contracts and price and an explicit id), so two fills in the same second get different ids
    while a TradingView retry of the same alert gets the same one."""
    def text(key: str) -> str:
        v = d.get(key)
        return "" if v is None else str(v).strip()[:MAX_ID_LEN]

    canon = json.dumps(
        ["sync", symbol, side or "flat", units, clock.iso(fired_at), text("order_id"), text("order_action"),
         text("order_contracts"), price, text("id")],
        separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    )
    return "sync:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:HASH_ID_LEN]


# --------------------------------------------------------------------------- parse


def parse_payload(d: Dict[str, Any], cfg: Config, received_at: datetime,
                  exit_symbol_ok: Optional[Callable[[str], bool]] = None) -> Signal:
    """Validate a decoded alert and build a :class:`Signal`.

    Raises :class:`SignalError` with one of BAD_JSON, BAD_ACTION, NO_SYMBOL,
    SYMBOL_NOT_ALLOWED, BAD_NUMBER, NO_TIME, BAD_POSITION, BAD_SIZE. Does not check the secret or freshness.
    The returned ``Signal.raw`` is the merged payload without the secret.

    Exits fail open: a close for a symbol that is no longer in ``symbols.allowed`` is still
    accepted while the symbol has a spec, or when ``exit_symbol_ok(mt5_symbol)`` says a
    position on it is open (the server checks the ledger).
    """
    if not isinstance(d, dict):
        raise SignalError("BAD_JSON", "expected a JSON object, got %s" % _json_type(d))

    # 1. merge the nested order (its keys win)
    order = _parse_order(d.get("order"))
    if order is not None:
        merged = dict(d)
        merged.update(order)
        raw = strip_secrets(merged)
        if "order" not in order:
            raw["order"] = strip_secrets(order)
    else:
        merged = d
        raw = strip_secrets(d)

    # 2. action
    action, inferred_side = _resolve_action(merged)
    is_entry = action in ("buy", "sell")

    # 3. symbol
    tv_symbol = ""
    symbol = ""
    if action != "close_all":
        _, raw_symbol = _first(merged, SYMBOL_KEYS)
        tv_symbol = cfg.normalize_tv_symbol(raw_symbol) if raw_symbol is not None else ""
        if not tv_symbol:
            raise SignalError("NO_SYMBOL", "missing 'symbol' (or 'ticker')")
        symbol = cfg.mt5_symbol(tv_symbol)
        if not cfg.is_allowed(tv_symbol):
            exit_ok = action == "close" and (cfg.spec_for(tv_symbol) is not None or
                                             (exit_symbol_ok is not None and exit_symbol_ok(symbol)))
            if not exit_ok:
                raise SignalError("SYMBOL_NOT_ALLOWED", "symbol %s is not allowed" % _short(tv_symbol))

    # 4. numbers (entries fail closed on garbage; exits ignore it)
    price = _field_num(merged, PRICE_KEYS, zero_is_none=False, strict=is_entry)
    sl = _field_num(merged, SL_KEYS, zero_is_none=True, strict=is_entry)
    tp = _field_num(merged, TP_KEYS, zero_is_none=True, strict=is_entry)
    risk_pct = _field_num(merged, RISK_KEYS, zero_is_none=False, strict=is_entry)
    quote_usd = _field_num(merged, QUOTE_USD_KEYS, zero_is_none=False, strict=is_entry)

    # 5. side (close only)
    side = None  # type: Optional[str]
    if action == "close":
        side = SIDE_ALIASES.get(_norm_word(merged.get("side"))) or inferred_side

    # 5b. sync (mirror mode): the strategy's resulting position; it carries no SL/TP of its own
    target_side = None  # type: Optional[str]
    target_units = None  # type: Optional[float]
    if action == "sync":
        target_side, target_units = _sync_target(merged)
        sl = tp = risk_pct = None

    # 6. fired_at
    tkey, raw_time = _first(merged, TIME_KEYS)
    if raw_time is None:
        raise SignalError("NO_TIME", "missing 'time' (use \"time\":\"{{timenow}}\")")
    try:
        fired_at = clock.parse_tv_time(raw_time)
    except (ValueError, TypeError, OverflowError) as e:
        raise SignalError("NO_TIME", "%s: cannot parse %s (%s)" % (tkey, _short(raw_time), e))

    # 8. strategy / comment (needed by the id hash)
    strategy = "" if merged.get("strategy") is None else str(merged.get("strategy")).strip()
    strategy = strategy[:MAX_STRATEGY_LEN]
    comment = "" if merged.get("comment") is None else str(merged.get("comment")).strip()
    comment = comment[:MAX_COMMENT_LEN] or DEFAULT_COMMENT

    # 7. id
    raw_id = merged.get("id")
    given_id = "" if raw_id is None else str(raw_id).strip()
    if action == "sync":
        sig_id = _sync_id(symbol, target_side, target_units, fired_at, merged, price)
    elif given_id:
        sig_id = "%s:%s" % (action, given_id[:MAX_ID_LEN])
    else:
        sig_id = _hash_id(action, symbol, side, price, sl, tp, fired_at, strategy)

    return Signal(
        id=sig_id,
        action=action,
        tv_symbol=tv_symbol,
        symbol=symbol,
        side=side,
        price=price,
        sl=sl,
        tp=tp,
        risk_pct=risk_pct,
        quote_usd=quote_usd,
        fired_at=fired_at,
        received_at=clock.ensure_utc(received_at),
        strategy=strategy,
        comment=comment,
        raw=raw,
        target_side=target_side,
        target_units=target_units,
    )


def check_freshness(sig: Signal, now: datetime, cfg: Config) -> Optional[str]:
    """"STALE" if the alert is older than ``max_signal_age_s``, "FUTURE" if it is dated
    more than ``max_future_skew_s`` ahead of ``now``, else None."""
    age = (clock.ensure_utc(now) - clock.ensure_utc(sig.fired_at)).total_seconds()
    if age > float(cfg.server.max_signal_age_s):
        return "STALE"
    if -age > float(cfg.server.max_future_skew_s):
        return "FUTURE"
    return None
