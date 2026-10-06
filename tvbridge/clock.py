"""Time helpers.

All datetimes inside tvbridge are timezone-aware UTC. "Now" must only be obtained
through :func:`utcnow` so tests can freeze time with :func:`set_clock`.

Server time (the MT5 broker's clock, e.g. GMT+3) is only used to determine the
trading day and trading windows; it is always derived from UTC with a fixed offset.
"""

import math
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional, Union

UTC = timezone.utc

_lock = threading.Lock()
_provider = None  # type: Optional[Callable[[], datetime]]


def _real_now() -> datetime:
    return datetime.now(UTC)


def utcnow() -> datetime:
    """Return the current time as a tz-aware UTC datetime (overridable for tests)."""
    with _lock:
        fn = _provider
    if fn is None:
        return _real_now()
    return ensure_utc(fn())


def set_clock(fn: Optional[Callable[[], datetime]]) -> None:
    """Install a clock provider (tests: freeze time). ``None`` restores the real clock.

    The provider may return a naive datetime, which is interpreted as UTC.
    """
    global _provider
    with _lock:
        _provider = fn


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to tz-aware UTC (naive datetimes are assumed to be UTC)."""
    if not isinstance(dt, datetime):
        raise TypeError("expected datetime, got %r" % type(dt).__name__)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _server_tz(offset_hours: float) -> timezone:
    return timezone(timedelta(hours=float(offset_hours)))


def to_server(dt_utc: datetime, offset_hours: float) -> datetime:
    """Convert a UTC instant to the broker's server time (tz-aware, fixed offset)."""
    return ensure_utc(dt_utc).astimezone(_server_tz(offset_hours))


def server_date(dt_utc: datetime, offset_hours: float) -> date:
    """The broker server's calendar date at instant ``dt_utc``."""
    return to_server(dt_utc, offset_hours).date()


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def us_dst_active(dt_utc: datetime) -> bool:
    """True if US daylight saving time is in effect at ``dt_utc``.

    US rules since 2007: from the second Sunday of March at 02:00 EST (07:00 UTC) to the
    first Sunday of November at 02:00 EDT (06:00 UTC).
    """
    dt = ensure_utc(dt_utc)
    y = dt.year
    start = datetime.combine(_nth_sunday(y, 3, 2), time(7, 0), tzinfo=UTC)
    end = datetime.combine(_nth_sunday(y, 11, 1), time(6, 0), tzinfo=UTC)
    return start <= dt < end


def ny_close_offset_hours(dt_utc: datetime) -> float:
    """Server UTC offset of a broker whose day ends at the New York close (17:00 New York
    = 00:00 server): UTC+3 while US DST is in effect, otherwise UTC+2."""
    return 3.0 if us_dst_active(dt_utc) else 2.0


def server_midnight_utc(server_day: date, offset_hours: float) -> datetime:
    """UTC instant of 00:00 server time on ``server_day``.

    Example: offset 3.0, 2026-10-01 -> 2026-09-30T21:00:00Z.
    """
    if isinstance(server_day, datetime):
        server_day = server_day.date()
    midnight = datetime(server_day.year, server_day.month, server_day.day, tzinfo=UTC)
    return midnight - timedelta(hours=float(offset_hours))


_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def parse_hhmm(s: str) -> time:
    """Parse "HH:MM" (or "HH:MM:SS") into a naive :class:`datetime.time`. Raises ValueError."""
    if not isinstance(s, str):
        raise ValueError("expected 'HH:MM' string, got %r" % (s,))
    m = _HHMM_RE.match(s)
    if not m:
        raise ValueError("invalid time %r (expected HH:MM)" % (s,))
    hh, mm = int(m.group(1)), int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError("invalid time %r (out of range)" % (s,))
    return time(hh, mm, ss)


# ISO-ish timestamps: date, 'T' or space, time with optional seconds/fraction, optional zone.
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"\s*(Z|z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)?$"
)
_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")

# Epoch values above this are interpreted as milliseconds (1e11 s is the year 5138).
_MS_THRESHOLD = 1e11


def _from_epoch(x: float) -> datetime:
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        raise ValueError("invalid epoch value %r" % (x,))
    seconds = float(x)
    if abs(seconds) >= _MS_THRESHOLD:
        seconds = seconds / 1000.0
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as e:
        raise ValueError("epoch value out of range: %r (%s)" % (x, e))


def _parse_iso_string(s: str) -> datetime:
    m = _ISO_RE.match(s)
    if not m:
        raise ValueError("unrecognized time format: %r" % (s,))
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 0
    mi = int(m.group(5)) if m.group(5) else 0
    ss = int(m.group(6)) if m.group(6) else 0
    frac = m.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    zone = m.group(8)
    tz = UTC
    if zone and zone not in ("Z", "z", "UTC", "GMT"):
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        oh = int(digits[:2])
        om = int(digits[2:4]) if len(digits) >= 4 else 0
        if oh > 23 or om > 59:
            raise ValueError("invalid UTC offset in %r" % (s,))
        tz = timezone(sign * timedelta(hours=oh, minutes=om))
    try:
        dt = datetime(year, month, day, hh, mi, ss, micro, tzinfo=tz)
    except ValueError as e:
        raise ValueError("invalid time %r: %s" % (s, e))
    return dt.astimezone(UTC)


def parse_tv_time(v: Union[str, int, float, datetime]) -> datetime:
    """Parse a TradingView time value into tz-aware UTC.

    Accepts ISO strings ("2026-10-01T09:56:00Z", fractional seconds, "+00:00" or other
    offsets, "2026-10-01 09:56:00" which is assumed UTC), epoch seconds or epoch
    milliseconds (int, float or digit strings) and datetimes. Raises ValueError.
    """
    if v is None or isinstance(v, bool):
        raise ValueError("missing or invalid time value: %r" % (v,))
    if isinstance(v, datetime):
        return ensure_utc(v)
    if isinstance(v, (int, float)):
        return _from_epoch(v)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if not isinstance(v, str):
        raise ValueError("unsupported time value type: %s" % type(v).__name__)
    s = v.strip()
    if not s:
        raise ValueError("empty time value")
    if _NUM_RE.match(s):
        return _from_epoch(float(s) if "." in s else int(s))
    return _parse_iso_string(s)


def iso(dt: datetime) -> str:
    """Format as UTC ISO-8601 with a "Z" suffix, e.g. "2026-10-01T09:56:00Z".

    Microseconds are included only when non-zero. Naive datetimes are assumed UTC.
    """
    s = ensure_utc(dt).isoformat()
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def from_iso(s: Union[str, datetime]) -> datetime:
    """Parse an ISO-8601 string (as produced by :func:`iso`) into tz-aware UTC.

    Naive strings are assumed UTC. Datetimes are passed through (converted to UTC).
    Raises ValueError on garbage.
    """
    if isinstance(s, datetime):
        return ensure_utc(s)
    if not isinstance(s, str):
        raise ValueError("expected ISO string, got %r" % (s,))
    return _parse_iso_string(s.strip())
