"""SQLite persistence: signals, position ledger, account snapshots, day state, events, kv.

One connection (``check_same_thread=False``) guarded by a single ``threading.RLock``.
Timestamps are stored as fixed-width UTC strings ("YYYY-MM-DDTHH:MM:SS.ffffffZ") so
that SQL string comparison equals chronological order; read them back with
``clock.from_iso``.
"""

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from . import clock
from .models import AccountSnapshot, Signal

log = logging.getLogger("tvbridge.store")

SIGNAL_STATUSES = ("queued", "processing", "done", "rejected", "failed", "expired", "rehearsed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
    id TEXT PRIMARY KEY, received_at TEXT, fired_at TEXT, action TEXT, symbol TEXT,
    payload TEXT, status TEXT, reason TEXT, result TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_received ON signals(received_at);
CREATE TABLE IF NOT EXISTS positions(
    pid INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, symbol TEXT, side TEXT, lots REAL,
    entry_price REAL, sl REAL, tp REAL, risk_usd REAL, opened_at TEXT, status TEXT,
    closed_at TEXT, close_reason TEXT, ticket TEXT);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_opened ON positions(opened_at);
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, balance REAL, equity REAL, margin REAL,
    free_margin REAL, n_positions INTEGER, source TEXT);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);
CREATE TABLE IF NOT EXISTS day_state(
    server_date TEXT PRIMARY KEY, ref_balance REAL, ref_equity REAL, reference REAL,
    source TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, kind TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


def _ts(v: Union[datetime, str, None]) -> Optional[str]:
    """Fixed-width UTC timestamp for storage (lexicographic order == time order)."""
    if v is None:
        return None
    dt = clock.from_iso(v) if isinstance(v, str) else clock.ensure_utc(v)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now_ts() -> str:
    return _ts(clock.utcnow())  # type: ignore[return-value]


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


def _json_loads(s: Optional[str]) -> Any:
    if s is None or s == "":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return s


def _day_key(d: Union[date, datetime, str]) -> str:
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


class Store:
    """Thread-safe SQLite store. ``path`` may be ``":memory:"``."""

    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:  # pragma: no cover - e.g. unsupported filesystem
                log.warning("could not enable WAL journal mode for %s", self.path)
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        self._closed = False

    # ------------------------------------------------------------------ basics

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ signals

    def insert_signal(self, sig: Signal, status: str = "queued") -> bool:
        """Insert a new signal. Returns False if a signal with that id already exists."""
        now = _now_ts()
        try:
            self._exec(
                "INSERT INTO signals(id, received_at, fired_at, action, symbol, payload, status, reason, "
                "result, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sig.id, _ts(sig.received_at), _ts(sig.fired_at), sig.action, sig.symbol,
                 _json_dumps(sig.to_dict()), status, "", None, now),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def set_signal_status(self, sig_id: str, status: str, reason: str = "",
                          result: Optional[Dict[str, Any]] = None) -> None:
        """Update status/reason; ``result`` (if given) replaces the stored result JSON."""
        now = _now_ts()
        if result is None:
            self._exec("UPDATE signals SET status=?, reason=?, updated_at=? WHERE id=?",
                       (status, reason or "", now, sig_id))
        else:
            self._exec("UPDATE signals SET status=?, reason=?, result=?, updated_at=? WHERE id=?",
                       (status, reason or "", _json_dumps(result), now, sig_id))

    @staticmethod
    def _signal_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        row["payload"] = _json_loads(row.get("payload"))
        row["result"] = _json_loads(row.get("result"))
        return row

    def get_signal_row(self, sig_id: str) -> Optional[Dict[str, Any]]:
        """All columns of one signal; ``payload``/``result`` parsed from JSON."""
        return self._signal_row(self._one("SELECT * FROM signals WHERE id=?", (sig_id,)))

    def load_signal(self, sig_id: str) -> Optional[Signal]:
        row = self.get_signal_row(sig_id)
        if row is None or not isinstance(row.get("payload"), dict):
            return None
        return Signal.from_dict(row["payload"])

    def signals_with_status(self, statuses: Sequence[str]) -> List[Dict[str, Any]]:
        """Signal rows whose status is in ``statuses``, oldest first."""
        if isinstance(statuses, str):
            statuses = [statuses]
        statuses = list(statuses)
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        rows = self._all(
            "SELECT * FROM signals WHERE status IN (%s) ORDER BY received_at ASC, rowid ASC" % marks, statuses
        )
        return [self._signal_row(r) for r in rows]  # type: ignore[misc]

    def exit_signals_received_after(self, sig_id: str) -> List[Dict[str, Any]]:
        """close / close_all signals received after signal ``sig_id`` (later ``received_at``,
        or the same instant and inserted later), any status, oldest first; payload parsed."""
        row = self._one("SELECT received_at, rowid AS rid FROM signals WHERE id=?", (sig_id,))
        if row is None:
            return []
        rows = self._all(
            "SELECT *, rowid AS rid FROM signals WHERE action IN ('close', 'close_all') AND "
            "(received_at > ? OR (received_at = ? AND rowid > ?)) ORDER BY received_at ASC, rowid ASC",
            (row["received_at"], row["received_at"], row["rid"]),
        )
        return [self._signal_row(r) for r in rows]  # type: ignore[misc]

    def newer_sync_signal(self, sig_id: str, exclude_id_prefix: str = "") -> Optional[Dict[str, Any]]:
        """The newest ``sync`` signal on the same symbol that is later than signal ``sig_id``
        (later ``fired_at``; for the same instant, stored later), any status; None if there is
        none or ``sig_id`` is unknown. Signals whose id starts with ``exclude_id_prefix`` are
        skipped (a fan-out parent's own children: ``<parent id>@``)."""
        row = self._one("SELECT symbol, fired_at, rowid AS rid FROM signals WHERE id=?", (sig_id,))
        if row is None:
            return None
        prefix = exclude_id_prefix or ""
        newer = self._one(
            "SELECT *, rowid AS rid FROM signals WHERE action = 'sync' AND symbol = ? COLLATE NOCASE "
            "AND id != ? AND (fired_at > ? OR (fired_at = ? AND rowid > ?)) "
            "AND (? = '' OR substr(id, 1, ?) != ?) "
            "ORDER BY fired_at DESC, rowid DESC LIMIT 1",
            (row["symbol"], sig_id, row["fired_at"], row["fired_at"], row["rid"], prefix, len(prefix), prefix),
        )
        return self._signal_row(newer)

    def recent_signals(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Most recent signal rows, newest first."""
        rows = self._all("SELECT * FROM signals ORDER BY received_at DESC, rowid DESC LIMIT ?", (int(limit),))
        return [self._signal_row(r) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ ledger

    def add_position(self, signal_id: Optional[str], symbol: str, side: str, lots: float,
                     entry_price: Optional[float], sl: Optional[float], tp: Optional[float],
                     risk_usd: float, opened_at: Union[datetime, str], ticket: Optional[str] = None) -> int:
        """Record an opened position in the ledger. Returns its pid."""
        cur = self._exec(
            "INSERT INTO positions(signal_id, symbol, side, lots, entry_price, sl, tp, risk_usd, opened_at, "
            "status, closed_at, close_reason, ticket) VALUES (?,?,?,?,?,?,?,?,?, 'open', NULL, NULL, ?)",
            (signal_id, symbol, side, float(lots),
             None if entry_price is None else float(entry_price),
             None if sl is None else float(sl), None if tp is None else float(tp),
             float(risk_usd or 0.0), _ts(opened_at), None if ticket is None else str(ticket)),
        )
        return int(cur.lastrowid)

    def close_ledger_positions(self, symbol: Optional[str], side: Optional[str], reason: str,
                               closed_at: Union[datetime, str]) -> int:
        """Mark open ledger rows closed. ``symbol`` matched case-insensitively (None/"" = all
        symbols); ``side`` None = both sides. Returns the number of rows closed."""
        sql = "UPDATE positions SET status='closed', closed_at=?, close_reason=? WHERE status='open'"
        params = [_ts(closed_at), reason]  # type: List[Any]
        if symbol:
            sql += " AND symbol = ? COLLATE NOCASE"
            params.append(symbol)
        if side:
            sql += " AND side = ?"
            params.append(side)
        with self._lock:
            cur = self._conn.execute(sql, params)
            return int(cur.rowcount)

    def close_ledger_position(self, pid: int, reason: str, closed_at: Union[datetime, str]) -> None:
        self._exec(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=? WHERE pid=? AND status='open'",
            (_ts(closed_at), reason, int(pid)),
        )

    def update_position_risk(self, pid: int, sl: Optional[float], risk_usd: Optional[float]) -> None:
        """Re-book an open ledger row's stop-loss and risk (e.g. the SL was widened in MT5)."""
        self._exec("UPDATE positions SET sl=?, risk_usd=? WHERE pid=? AND status='open'",
                   (None if sl is None else float(sl), None if risk_usd is None else float(risk_usd), int(pid)))

    def reduce_ledger_position(self, pid: int, lots_closed: float) -> Optional[float]:
        """After a partial close: lower an open ledger row's lots by ``lots_closed`` and its
        ``risk_usd`` pro rata. Returns the remaining lots (never below 0), or None if the row
        is not open. The caller closes a row that reaches 0."""
        with self._lock:
            row = self._conn.execute("SELECT lots, risk_usd FROM positions WHERE pid=? AND status='open'",
                                     (int(pid),)).fetchone()
            if row is None:
                return None
            old = float(row["lots"] or 0.0)
            left = max(0.0, round(old - float(lots_closed), 8))
            risk = row["risk_usd"]
            new_risk = None if risk is None else (float(risk) * left / old if old > 0 else 0.0)
            self._conn.execute("UPDATE positions SET lots=?, risk_usd=? WHERE pid=? AND status='open'",
                               (left, new_risk, int(pid)))
            return left

    def open_ledger_positions(self) -> List[Dict[str, Any]]:
        """Ledger rows with status 'open', oldest first (timestamps as stored strings)."""
        return self._all("SELECT * FROM positions WHERE status='open' ORDER BY pid ASC")

    def all_ledger_positions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Most recent ledger rows (open and closed), newest first."""
        return self._all("SELECT * FROM positions ORDER BY pid DESC LIMIT ?", (int(limit),))

    def count_trades_on_server_day(self, server_day: date, offset_hours: float) -> int:
        """Number of positions opened within ``server_day`` (server time, UTC+offset)."""
        start = clock.server_midnight_utc(server_day, offset_hours)
        end = start + timedelta(days=1)
        row = self._one(
            "SELECT COUNT(*) AS n FROM positions WHERE opened_at >= ? AND opened_at < ?",
            (_ts(start), _ts(end)),
        )
        return int(row["n"]) if row else 0

    def last_trade_at(self) -> Optional[datetime]:
        row = self._one("SELECT MAX(opened_at) AS t FROM positions")
        if not row or not row.get("t"):
            return None
        return clock.from_iso(row["t"])

    # ------------------------------------------------------------------ snapshots

    def add_snapshot(self, snap: AccountSnapshot) -> None:
        n = None if snap.positions is None else len(snap.positions)
        self._exec(
            "INSERT INTO snapshots(ts, balance, equity, margin, free_margin, n_positions, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (_ts(snap.ts), float(snap.balance), float(snap.equity),
             None if snap.margin is None else float(snap.margin),
             None if snap.free_margin is None else float(snap.free_margin), n, snap.source or ""),
        )

    @staticmethod
    def _snap(row: Dict[str, Any]) -> AccountSnapshot:
        return AccountSnapshot(
            ts=clock.from_iso(row["ts"]),
            balance=float(row["balance"]),
            equity=float(row["equity"]),
            margin=row["margin"],
            free_margin=row["free_margin"],
            positions=None,
            source=row["source"] or "",
        )

    def snapshot_from_row(self, row: Dict[str, Any]) -> AccountSnapshot:
        """AccountSnapshot for a raw snapshot row (``positions`` None)."""
        return self._snap(row)

    def latest_snapshot(self) -> Optional[AccountSnapshot]:
        """Most recent snapshot (``positions`` is always None)."""
        row = self._one("SELECT * FROM snapshots ORDER BY ts DESC, id DESC LIMIT 1")
        return self._snap(row) if row else None

    def last_snapshot_before(self, ts_utc: datetime) -> Optional[AccountSnapshot]:
        """Latest snapshot strictly before ``ts_utc`` (e.g. last one before server midnight)."""
        row = self._one("SELECT * FROM snapshots WHERE ts < ? ORDER BY ts DESC, id DESC LIMIT 1", (_ts(ts_utc),))
        return self._snap(row) if row else None

    def snapshot_rows_between(self, start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
        """Raw snapshot rows (incl. ``n_positions``; None = unknown) with start <= ts <= end, oldest first."""
        return self._all("SELECT * FROM snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts ASC, id ASC",
                         (_ts(start_utc), _ts(end_utc)))

    def last_snapshot_row_before(self, ts_utc: datetime) -> Optional[Dict[str, Any]]:
        """Raw row of the latest snapshot strictly before ``ts_utc`` (incl. ``n_positions``)."""
        return self._one("SELECT * FROM snapshots WHERE ts < ? ORDER BY ts DESC, id DESC LIMIT 1", (_ts(ts_utc),))

    def snapshots_between(self, start_utc: datetime, end_utc: datetime) -> List[AccountSnapshot]:
        """Snapshots with ``start_utc <= ts <= end_utc`` (both inclusive), oldest first."""
        rows = self._all(
            "SELECT * FROM snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts ASC, id ASC",
            (_ts(start_utc), _ts(end_utc)),
        )
        return [self._snap(r) for r in rows]

    def has_any_snapshot(self) -> bool:
        return self._one("SELECT 1 AS x FROM snapshots LIMIT 1") is not None

    def prune_snapshots(self, older_than_utc: datetime) -> int:
        """Delete snapshots older than the given instant. Returns rows deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM snapshots WHERE ts < ?", (_ts(older_than_utc),))
            return int(cur.rowcount)

    # ------------------------------------------------------------------ day state

    def get_day_state(self, server_day: date) -> Optional[Dict[str, Any]]:
        """Row for ``server_day`` (keys: server_date, ref_balance, ref_equity, reference, source, created_at)."""
        return self._one("SELECT * FROM day_state WHERE server_date=?", (_day_key(server_day),))

    def set_day_state(self, server_day: date, ref_balance: float, ref_equity: float, source: str) -> None:
        """Upsert the day's reference; ``reference = max(ref_balance, ref_equity)``."""
        key = _day_key(server_day)
        rb, re_ = float(ref_balance), float(ref_equity)
        reference = max(rb, re_)
        now = _now_ts()
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM day_state WHERE server_date=?", (key,)).fetchone()
            if exists:
                self._conn.execute(
                    "UPDATE day_state SET ref_balance=?, ref_equity=?, reference=?, source=?, created_at=? "
                    "WHERE server_date=?",
                    (rb, re_, reference, source, now, key),
                )
            else:
                self._conn.execute(
                    "INSERT INTO day_state(server_date, ref_balance, ref_equity, reference, source, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, rb, re_, reference, source, now),
                )

    # ------------------------------------------------------------------ events & kv

    def log_event(self, level: str, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append to the event log. Best effort: database errors are logged, never raised."""
        try:
            self._exec(
                "INSERT INTO events(ts, level, kind, message, data) VALUES (?,?,?,?,?)",
                (_now_ts(), level, kind, message, None if data is None else _json_dumps(data)),
            )
        except (sqlite3.Error, TypeError, ValueError) as e:
            log.error("failed to log event %s/%s: %s", level, kind, e)

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Newest events first; ``data`` parsed from JSON."""
        rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))
        for r in rows:
            r["data"] = _json_loads(r.get("data"))
        return rows

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None or row["value"] is None:
            return default
        return row["value"]

    def kv_with_prefix(self, prefix: str) -> Dict[str, str]:
        """Every kv entry whose key starts with ``prefix`` (key -> value)."""
        rows = self._all("SELECT key, value FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key",
                         (len(prefix), prefix))
        return {r["key"]: r["value"] for r in rows if r["value"] is not None}

    def delete_kv_prefix(self, prefix: str) -> int:
        """Delete every kv entry whose key starts with ``prefix``. Returns the number deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
            return int(cur.rowcount)

    def set_kv(self, key: str, value: Optional[str]) -> None:
        """Set a key; ``None`` deletes it."""
        if value is None:
            self._exec("DELETE FROM kv WHERE key=?", (key,))
            return
        self._exec(
            "INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), _now_ts()),
        )
