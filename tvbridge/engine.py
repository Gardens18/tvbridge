"""Engine (SPEC section 12): signal queue, risk guard, executor thread and scheduler.

Threads:

* The webhook server's handler threads call :meth:`Engine.submit_signal`, which only
  enqueues a task (fast and thread-safe).
* One **executor thread** owns the :class:`~tvbridge.executors.base.Executor`. It runs
  tasks from a priority queue: FLATTEN/CLOSE_ALL=0, CLOSE/SYNC=1, OPEN=2, ACCOUNT=3 (FIFO
  within a priority). Nothing else calls the executor once the engine has started.
* A **scheduler thread** (1 s tick) requests account polls, consumes CLI commands from kv
  ``command``, performs the server-day rollover, writes ``heartbeat.json`` every 10 s and
  deletes old screenshots.

Safety rules implemented here (SPEC section 0):

* **Entries fail closed.** :func:`tvbridge.risk.plan_entry` refuses on any doubt; an
  execution that may have reached the broker (``uncertain`` after the click, a result for
  another side/volume/symbol/price, any unexpected exception) halts new entries (kv
  ``halted``) until a human runs ``tvbridge resume``; an executor error *before* the click
  (nothing was sent) only fails that signal (warn, critical after 3 in a row); an entry is
  never retried; an order interrupted by a crash or restart is marked failed and halts
  entries on the next start.
* **Exits fail open.** Closes, close_all, flatten, reversal closes and the kill switch run
  even when paused or halted. A ledger row is only dropped when MT5 is seen without it
  *and* the balance (or margin) shows a close; an empty close_all result while positions
  are evidently open is a failure, never "flat".

* **Mirror mode** (``sync`` alerts, :meth:`Engine._do_sync`) follows the same two rules: the
  part of a sync that reduces or closes the MT5 position always runs; the part that opens one
  goes through the risk guard, is never retried, and only the newest sync per symbol acts.

The engine reads the config once, when it is constructed (no hot reload).
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import dataclasses
import queue
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import __version__, clock, risk
from .config import Config
from .executors.base import Executor, ExecutorError
from .models import AccountSnapshot, ObservedPosition, OrderRequest, OrderResult, Signal, TradePlan, opposite_side
from .server import EXIT_MAX_AGE_S, WebhookServer

log = logging.getLogger("tvbridge.engine")

# Task kinds and their priorities (lower runs first).
FLATTEN = "FLATTEN"        # close everything (kill switch, `tvbridge flatten`)
CLOSE_ALL = "CLOSE_ALL"    # a close_all signal
CLOSE = "CLOSE"            # a close signal
OPEN = "OPEN"              # a buy/sell signal
ACCOUNT = "ACCOUNT"        # account poll
SYNC = "SYNC"              # a mirror-mode sync signal (may carry an exit: same priority as CLOSE)
PRIORITIES = {FLATTEN: 0, CLOSE_ALL: 0, CLOSE: 1, SYNC: 1, OPEN: 2, ACCOUNT: 3}
_KIND_FOR_ACTION = {"buy": OPEN, "sell": OPEN, "close": CLOSE, "close_all": CLOSE_ALL, "sync": SYNC}
#: kv "mirror_scale:<TV symbol>": opened lots / requested lots of the mirrored position
MIRROR_SCALE_PREFIX = "mirror_scale:"
#: Signal.raw key of a fan-out child (value: the parent signal's id); a child never fans out again
FAN_OUT_PARENT = "fan_out_parent"
LOTS_EPS = 1e-9

ACCOUNT_REFRESH_S = 5.0        # an entry re-reads the account if the last read is older than this
HEARTBEAT_S = 10.0             # heartbeat.json interval
HEARTBEAT_STALE_S = 30.0       # an older heartbeat means the engine is not running
READ_FAILURE_WARN = 4          # consecutive failed account reads before a warning
RECONCILE_MISSES = 2           # consecutive reads without a ledger position before it may be closed
BALANCE_EPS = 0.005            # a balance change larger than this proves a close was booked
MARGIN_DROP_REL = 0.05         # ... or margin falling by more than 5 %
KILL_RETRY_BACKOFF_S = (15.0, 30.0, 60.0, 120.0)   # while killed: re-flatten delays after a failure
KILL_SLOW_AFTER = 5            # after this many failed flattens in a row ...
KILL_REFLATTEN_S = 300.0       # ... retry only every 5 minutes (e.g. market closed)
FLATTEN_FAIL_NOTIFY_S = 300.0  # the same FLATTEN FAILED notification at most every 5 minutes
KILL_IMPLAUSIBLE_MOVE = 0.03   # an equity jump > 3 % in one poll is re-read before killing
PRECLICK_CRITICAL_AFTER = 3    # pre-click entry failures in a row before a critical notification
ROLLOVER_STALE_MIN_S = 120.0   # a pre-midnight snapshot older than this is not a reference basis
REFERENCE_REFINE_POLLS = 3     # polls after midnight that may still raise a "rollover" reference
REFERENCE_OUTLIER_REL = 0.03   # a last-before-midnight read this far from the one before is dropped
REFERENCE_JUMP_WARN_REL = 0.05 # warn when the reference moves this much from the previous day
PENDING_ADOPTION_S = 600.0     # an uncertain entry's position is adopted when it appears within this
# longest legitimate task: GUI closes of several positions (with dialog and result timeouts) and an
# entry with its reversal close can take minutes; an account poll never does
STALL_LIMITS_S = {"FLATTEN": 300.0, "CLOSE_ALL": 300.0, "CLOSE": 300.0, "OPEN": 300.0, "SYNC": 300.0}
STALL_DEFAULT_S = 180.0
DB_ERROR_NOTIFY_S = 600.0      # database-write failure notifications at most every 10 minutes
POSITIONS_UNKNOWN_WARN = 4     # reads in a row with an unverifiable position list before a warning
COMMAND_MAX_AGE_S = 3600.0     # kv `command` requests older than this are discarded
SNAPSHOT_RETENTION_DAYS = 35   # account snapshots older than this are pruned
CLEANUP_INTERVAL_S = 3600.0    # screenshot/snapshot cleanup (also at start)
SHOTS_MAX_BYTES = 2 * 1024 ** 3   # total size cap of shots/ day folders (oldest deleted first)
DISK_CHECK_S = 300.0
DISK_MIN_FREE_BYTES = 2 * 1024 ** 3
DISK_NOTIFY_S = 3600.0
_DAY_DIR_RE = re.compile(r"^\d{8}$")


@dataclass
class Task:
    """One unit of work for the executor thread."""

    kind: str                                  # FLATTEN | CLOSE_ALL | CLOSE | SYNC | OPEN | ACCOUNT
    signal: Optional[Signal] = None
    not_before: Optional[datetime] = None      # run no earlier than this (UTC)
    reason: str = ""

    def describe(self) -> str:
        if self.signal is None:
            return "%s (%s)" % (self.kind, self.reason) if self.reason else self.kind
        s = self.signal
        return "%s %s %s%s [%s]" % (self.kind, s.action, s.symbol or "*",
                                    " " + s.side if s.side else "", s.id)


# --------------------------------------------------------------------------- helpers


def _reason_of(e: BaseException) -> str:
    """"CODE: message" for ExecutorError, "Type: message" for anything else."""
    if isinstance(e, ExecutorError):
        return e.reason
    return "%s: %s" % (type(e).__name__, e)


def _finite(x: Any) -> bool:
    if x is None or isinstance(x, bool):
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _money(v: Any) -> str:
    try:
        return "{:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _plan_dict(plan: TradePlan) -> Dict[str, Any]:
    return {
        "approved": plan.approved,
        "reason": plan.reason,
        "lots": plan.lots,
        "risk_usd": round(plan.risk_usd, 2),
        "per_lot_loss_usd": round(plan.per_lot_loss_usd, 2),
        "close_first": [p.to_dict() for p in plan.close_first],
        "details": plan.details,
    }


def _results_dict(results: Sequence[OrderResult]) -> List[Dict[str, Any]]:
    return [r.to_dict() for r in results]


def _snapshot_dict(snap: Optional[AccountSnapshot], now: datetime) -> Optional[Dict[str, Any]]:
    if snap is None:
        return None
    d = snap.to_dict()
    d["age_s"] = round((clock.ensure_utc(now) - clock.ensure_utc(snap.ts)).total_seconds(), 1)
    return d


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def floors_for_day(cfg: Config, store: Any, server_day: date) -> Optional[risk.Floors]:
    """Floors from the stored day_state of ``server_day``, or None if missing/invalid."""
    row = store.get_day_state(server_day)
    if row is None:
        return None
    try:
        return risk.compute_floors(float(row["reference"]), cfg)
    except (TypeError, ValueError, KeyError):
        return None


def config_warnings(cfg: Config) -> List[str]:
    """Risky but valid config combinations (shown by the engine at start and by doctor)."""
    out = []  # type: List[str]
    r = cfg.risk
    if r.daily_buffer_pct <= r.kill_buffer_pct or r.max_buffer_pct <= r.kill_buffer_pct:
        out.append(
            "risk.kill_buffer_pct (%s) is not below daily_buffer_pct (%s) and max_buffer_pct (%s): the "
            "entry floor can sit at or below the kill floor, so an approved trade's worst case can "
            "trigger the kill switch" % (r.kill_buffer_pct, r.daily_buffer_pct, r.max_buffer_pct))
    if not r.block_untracked_positions:
        out.append(
            "risk.block_untracked_positions is false: positions tvbridge did not open only count toward open "
            "risk when their stop-loss is readable; otherwise entries are refused (TOTAL_OPEN_RISK)")
    if cfg.executor.mode in ("rehearsal", "live") and not (cfg.account.account_login or "").strip():
        out.append(
            "account.account_login is empty: tvbridge cannot tell the MT5 terminal apart from other Wine "
            "windows (MetaEditor, MT4); live mode refuses to start without it (ACCOUNT_LOGIN_REQUIRED)")
    return out


def mirror_scales(store: Any) -> Dict[str, float]:
    """Stored mirror scales (symbol key -> opened lots / requested lots)."""
    out = {}  # type: Dict[str, float]
    for key, value in store.kv_with_prefix(MIRROR_SCALE_PREFIX).items():
        try:
            out[key[len(MIRROR_SCALE_PREFIX):]] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def read_heartbeat(path: Path) -> Optional[Dict[str, Any]]:
    """The engine's heartbeat.json as a dict, or None if missing/unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_age_s(hb: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[float]:
    if not hb or not hb.get("ts"):
        return None
    try:
        ts = clock.from_iso(hb["ts"])
    except ValueError:
        return None
    return ((now or clock.utcnow()) - ts).total_seconds()


def engine_alive(hb: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> bool:
    """True if the heartbeat is fresh (and not marked stopped or executor-stalled)."""
    age = heartbeat_age_s(hb, now)
    h = hb or {}
    return age is not None and age <= HEARTBEAT_STALE_S and not h.get("stopped") and not h.get("executor_stalled")


def build_status(cfg: Config, store: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Status from the database alone (works whether or not the engine is running)."""
    now = clock.ensure_utc(now) if now is not None else clock.utcnow()
    day = cfg.server_date(now)
    offset = cfg.server_offset_at(now)
    server_now = clock.to_server(now, offset)
    ds = store.get_day_state(day)
    floors = floors_for_day(cfg, store, day)
    rows = store.open_ledger_positions()
    open_risk = sum(float(r.get("risk_usd") or 0.0) for r in rows if _finite(r.get("risk_usd")))
    paused = (store.get_kv("paused") or "") not in ("", "0")
    halted = store.get_kv("halted") or ""
    pending = store.signals_with_status(["queued", "processing"])
    return {
        "now": clock.iso(now),
        "server_time": server_now.strftime("%a %Y-%m-%d %H:%M:%S"),
        "server_utc_offset_hours": offset,
        "server_date": day.isoformat(),
        "mode": cfg.executor.mode,
        "account": cfg.account.name,
        "paused": paused,
        "pause_reason": (store.get_kv("pause_reason") or "") if paused else "",
        "halted": halted,
        "entries_allowed": not paused and not halted,
        "trading_window": risk.check_trading_window(now, cfg) or "open",
        "snapshot": _snapshot_dict(store.latest_snapshot(), now),
        "day_reference": None if ds is None else {
            "server_date": ds.get("server_date"), "reference": ds.get("reference"),
            "ref_balance": ds.get("ref_balance"), "ref_equity": ds.get("ref_equity"),
            "source": ds.get("source"), "created_at": ds.get("created_at"),
        },
        "floors": None if floors is None else floors.to_dict(),
        "open_positions": rows,
        "open_risk_usd": round(open_risk, 2),
        "untracked": [],
        "trades_today": store.count_trades_on_server_day(day, cfg.server_offset_for_day(day)),
        "max_trades_per_day": cfg.risk.max_trades_per_day,
        "mirror": {"enabled": bool(cfg.mirror.enabled), "units_per_lot": cfg.mirror.units_per_lot,
                   "units_per_lot_by_symbol": dict(cfg.mirror.units_per_lot_by_symbol),
                   "stop_distance": cfg.mirror.stop_distance, "tp_distance": cfg.mirror.tp_distance,
                   "stop_distance_by_symbol": dict(cfg.mirror.stop_distance_by_symbol),
                   "tp_distance_by_symbol": dict(cfg.mirror.tp_distance_by_symbol),
                   "allow_adds": bool(cfg.mirror.allow_adds), "scales": mirror_scales(store),
                   "fan_out": {k: [{"symbol": cfg.mt5_symbol(t), "units_per_lot": cfg.mirror_units_per_lot(t)}
                                   for t in v] for k, v in cfg.mirror.fan_out.items()}},
        "pending_signals": [{"id": r.get("id"), "action": r.get("action"), "symbol": r.get("symbol"),
                             "status": r.get("status"), "reason": r.get("reason")} for r in pending],
        "queue_size": len(pending),
    }


# --------------------------------------------------------------------------- engine


class Engine:
    """Turns queued signals into executor actions, guarded by the risk module."""

    def __init__(self, cfg: Config, store: Any, executor: Executor, notifier: Any, start_server: bool = True):
        self.cfg = cfg
        self.store = store
        self.executor = executor
        self.notifier = notifier
        self.start_server = bool(start_server)
        self.server = None  # type: Optional[WebhookServer]
        #: scheduler tick in seconds (tests lower it)
        self.tick_s = 1.0
        #: pause before re-reading an implausible kill-switch equity read (tests lower it)
        self.kill_confirm_delay_s = 1.0
        #: called with a reason when the executor thread is dead or stuck (default: exit so
        #: launchd restarts the process); tests replace it
        self.on_stall = self._exit_for_restart  # type: Callable[[str], None]
        self.stall_limits_s = dict(STALL_LIMITS_S)
        self.stall_default_s = STALL_DEFAULT_S
        self.disk_min_free_bytes = DISK_MIN_FREE_BYTES
        self.shots_max_bytes = SHOTS_MAX_BYTES

        self._queue = queue.PriorityQueue()  # type: queue.PriorityQueue
        self._seq = itertools.count()
        self._lock = threading.RLock()          # guards the in-memory state below
        self._rollover_lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._threads = []  # type: List[threading.Thread]
        self._executor_thread = None  # type: Optional[threading.Thread]
        self._running = False
        self._started_at = None  # type: Optional[datetime]

        self._pending_ids = set()  # type: Set[str]       # signal ids queued or running
        self._delayed = []  # type: List[Task]            # tasks waiting for not_before
        self._current = None  # type: Optional[Task]
        self._current_started_mono = None  # type: Optional[float]
        self._account_pending = False
        self._flatten_pending = False

        self._last_snapshot = None  # type: Optional[AccountSnapshot]   # with positions
        self._prev_snapshot = None  # type: Optional[AccountSnapshot]   # the read before it
        self._account_dirty = True   # positions may have changed since the last read
        self._untracked = []  # type: List[ObservedPosition]
        self._untracked_notified = set()  # type: Set[Tuple[str, str]]
        self._missing_counts = {}  # type: Dict[int, int]  # ledger pid -> consecutive reads without it
        self._seen_basis = {}  # type: Dict[int, Tuple[float, Optional[float]]]  # pid -> (balance, margin) when last seen
        self._basis_fallback = None  # type: Optional[Tuple[float, Optional[float]]]
        self._open_basis = {}  # type: Dict[int, Tuple[float, Optional[float]]]  # pid -> account before the fill
        self._uncertain_rows = {}  # type: Dict[int, Dict[str, Any]]   # pid -> {"desc", "since"}
        self._sl_flags = {}  # type: Dict[int, str]       # pid -> "no stop-loss on the server" description
        self._sl_widened = set()  # type: Set[int]
        self._mismatch_notified = set()  # type: Set[str]
        self._positions_unknown_reads = 0
        self._positions_note = ""
        self._pending_adoptions = []  # type: List[Tuple[Signal, TradePlan, float]]
        self._read_failures = 0
        self._last_read_error = ""
        self._preclick_failures = 0
        self._kill_fail_streak = 0
        self._kill_next_retry_mono = None  # type: Optional[float]
        self._flatten_fail_notified = None  # type: Optional[Tuple[str, float]]
        self._crashed_polls = 0
        self._db_error_notify_mono = None  # type: Optional[float]
        self._floors_cache = None  # type: Optional[Tuple[date, risk.Floors]]
        self._stall_reported = ""
        self._kill_unrecorded = ""         # a kill whose kv `halted` could not be written
        self._rolled_once = False

        self._last_poll_mono = 0.0
        self._last_heartbeat_mono = 0.0
        self._last_cleanup_mono = 0.0
        self._last_disk_check_mono = 0.0
        self._disk_notify_mono = None  # type: Optional[float]

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Recover, read the account, roll the day, then start the listener and the threads."""
        if self._running:
            return
        cfg = self.cfg
        self._stop_ev.clear()
        with self._lock:
            # A fresh queue (also after stop()): recovery re-submits every "queued" signal.
            self._queue = queue.PriorityQueue()
            self._pending_ids = set()
            self._delayed = []
            self._account_pending = False
            self._flatten_pending = False
        self._started_at = clock.utcnow()
        self._stall_reported = ""
        log.info("engine starting: mode %s, executor %s, account %r, server offset %s",
                 cfg.executor.mode, getattr(self.executor, "name", "?"), cfg.account.name,
                 cfg.account.server_utc_offset_hours)
        for w in config_warnings(cfg):
            log.warning("config: %s", w)
            self._event("warn", "config_warning", w)
        if hasattr(self.executor, "abort_check"):
            # the GUI executor asks right before the Buy/Sell click whether to go ahead
            setattr(self.executor, "abort_check", self._entry_block_reason)
        try:
            prior = self.store.latest_snapshot()
        except Exception:
            prior = None
        if prior is not None:
            self._basis_fallback = (float(prior.balance), prior.margin)
        self._recover()
        try:
            self._poll_account("startup")
        except Exception:
            log.exception("startup account read failed")
        try:
            self._maybe_rollover()
        except Exception:
            log.exception("startup rollover failed")
        self._cleanup()

        # The listener starts before the threads: if it cannot bind, nothing has started
        # (a queued kill FLATTEN is re-detected by the next start's account read).
        if self.start_server:
            server = WebhookServer(cfg, self.store, self.submit_signal, self.notifier)
            try:
                server.start()
            except Exception as e:
                log.error("cannot start the webhook listener on %s:%s: %s", cfg.server.host, cfg.server.port, e)
                raise
            self.server = server

        mono = time.monotonic()
        self._last_poll_mono = mono
        self._last_cleanup_mono = mono
        self._last_disk_check_mono = 0.0
        self._running = True
        for name, target in (("tvbridge-executor", self._executor_loop),
                             ("tvbridge-scheduler", self._scheduler_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
            if target == self._executor_loop:
                self._executor_thread = t
        self._write_heartbeat()
        msg = "engine started: mode %s%s" % (
            cfg.executor.mode, ", listening on port %d" % self.server.port if self.server else "")
        self._event("info", "engine_started", msg, {"mode": cfg.executor.mode, "pid": os.getpid(),
                                                     "version": __version__})
        self._notify("tvbridge started", msg, "info")

    def stop(self, timeout: float = 10) -> None:
        """Stop the listener and the threads (the running task may finish first)."""
        self._stop_ev.set()
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                log.exception("error stopping the webhook listener")
        deadline = time.monotonic() + float(timeout)
        for t in self._threads:
            t.join(max(0.1, deadline - time.monotonic()))
        busy = [t.name for t in self._threads if t.is_alive()]
        self._threads = []
        if busy:
            log.warning("threads still busy after %.0f s: %s", timeout, busy)
        if self._running:
            self._running = False
            self._write_heartbeat(stopped=True)
            self._event("info", "engine_stopped", "engine stopped")
            log.info("engine stopped")

    @property
    def running(self) -> bool:
        return self._running

    def threads_alive(self) -> bool:
        """True while both engine threads run (False once a thread died)."""
        threads = list(self._threads)
        return bool(threads) and all(t.is_alive() for t in threads)

    # ------------------------------------------------------------------ public API

    def submit_signal(self, sig: Signal) -> None:
        """Enqueue a stored signal (server callback). Thread-safe and fast."""
        kind = _KIND_FOR_ACTION.get(sig.action)
        if kind is None:
            log.error("signal %s has an unknown action %r", sig.id, sig.action)
            self.store.set_signal_status(sig.id, "rejected", "BAD_ACTION: unknown action %r" % (sig.action,))
            return
        with self._lock:
            if sig.id in self._pending_ids:
                log.debug("signal %s is already queued", sig.id)
                return
            self._pending_ids.add(sig.id)
        self._put(Task(kind, sig, reason="signal"))

    def request_account_poll(self) -> bool:
        """Queue an account poll unless one is already pending."""
        with self._lock:
            if self._account_pending:
                return False
            self._account_pending = True
        self._put(Task(ACCOUNT, reason="poll"))
        return True

    def request_flatten(self, reason: str = "manual") -> bool:
        """Queue a FLATTEN (close every position) unless one is already pending."""
        return self._enqueue_flatten(reason)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until no task is queued or running (delayed tasks excluded). For tests/tools."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._queue.mutex:
                unfinished = self._queue.unfinished_tasks
            if unfinished == 0:
                return True
            time.sleep(0.01)
        return False

    def status(self) -> Dict[str, Any]:
        """Mode, paused/halted, latest snapshot (+age), floors, ledger, untracked, queue, trades today."""
        now = clock.utcnow()
        st = build_status(self.cfg, self.store, now)
        with self._lock:
            snap = self._last_snapshot
            untracked = [p.to_dict() for p in self._untracked]
            current = self._current.describe() if self._current else None
            delayed = len(self._delayed)
        if snap is not None:
            st["snapshot"] = _snapshot_dict(snap, now)
        st["untracked"] = untracked
        st["positions_uncertain"] = self._positions_uncertain_text()
        st["queue_size"] = self._queue.qsize() + delayed
        st["engine"] = {
            "running": self._running,
            "pid": os.getpid(),
            "version": __version__,
            "started_at": clock.iso(self._started_at) if self._started_at else None,
            "current_task": current,
            "account_read_failures": self._read_failures,
            "last_account_error": self._last_read_error,
            "account_poll_crashes": self._crashed_polls,
            "executor_stalled": self._stall_reported,
            "port": self.server.port if self.server is not None else None,
        }
        return st

    # ------------------------------------------------------------------ queue plumbing

    def _put(self, task: Task) -> None:
        with self._lock:
            seq = next(self._seq)
        self._queue.put((PRIORITIES[task.kind], seq, task))

    def _enqueue_flatten(self, reason: str) -> bool:
        with self._lock:
            if self._flatten_pending:
                return False
            self._flatten_pending = True
        self._put(Task(FLATTEN, reason=reason))
        return True

    def _release_due(self) -> None:
        with self._lock:
            if not self._delayed:
                return
            now = clock.utcnow()
            due = [t for t in self._delayed if t.not_before is None or t.not_before <= now]
            if not due:
                return
            self._delayed = [t for t in self._delayed if t not in due]
        for t in due:
            self._put(t)

    def _executor_loop(self) -> None:
        while not self._stop_ev.is_set():
            self._release_due()
            try:
                _prio, _seq, task = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if task.not_before is not None and task.not_before > clock.utcnow():
                    with self._lock:
                        self._delayed.append(task)
                    continue
                if self._stop_ev.is_set():
                    continue  # its signal stays "queued" and is re-submitted at the next start
                with self._lock:
                    self._current = task
                    self._current_started_mono = time.monotonic()
                self._run_task(task)
            except Exception:  # pragma: no cover - _run_task handles its own errors
                log.exception("unexpected error in the executor loop")
            finally:
                with self._lock:
                    self._current = None
                    self._current_started_mono = None
                self._queue.task_done()

    def _run_task(self, task: Task) -> None:
        sig = task.signal
        finished = True
        try:
            if task.kind == ACCOUNT:
                with self._lock:
                    self._account_pending = False
                self._poll_account("poll")
            elif task.kind == FLATTEN:
                # Cleared when dequeued (not when done), so a kill detected during this flatten
                # (e.g. by its own after-flatten read) can queue a follow-up FLATTEN.
                with self._lock:
                    self._flatten_pending = False
                self._do_close_all(task)
            elif sig is None:
                log.error("task %s has no signal", task.kind)
            elif not self._claim(sig):
                pass
            elif task.kind == CLOSE_ALL:
                self._do_close_all(task)
            elif task.kind == CLOSE:
                finished = self._do_close(task)
            elif task.kind == OPEN:
                self._do_open(task)
            elif task.kind == SYNC:
                self._do_sync(task)
        except Exception as e:
            self._task_crashed(task, e)
        finally:
            if sig is not None and finished:
                with self._lock:
                    self._pending_ids.discard(sig.id)

    def _claim(self, sig: Signal) -> bool:
        """True if the signal is still "queued" (never process a signal twice)."""
        row = self.store.get_signal_row(sig.id)
        if row is None:
            self.store.insert_signal(sig)
            return True
        if row.get("status") != "queued":
            log.info("signal %s is already %s; not processing it again", sig.id, row.get("status"))
            return False
        return True

    def _task_crashed(self, task: Task, e: BaseException) -> None:
        log.exception("task %s failed", task.describe())
        reason = "INTERNAL_ERROR: %s" % _reason_of(e)
        sig = task.signal
        if sig is not None:
            try:
                self.store.set_signal_status(sig.id, "failed", reason)
            except Exception:
                log.exception("cannot record the failure of %s", sig.id)
        if task.kind in (OPEN, SYNC):
            # An order may or may not have been sent: fail closed.
            try:
                self._halt("UNCERTAIN_EXECUTION: internal error while executing %s: %s" % (task.describe(), reason))
            except Exception:
                log.exception("cannot record the halt")
            msg = ("%s failed with an internal error (%s). New entries are halted: check MT5, then run "
                   "`tvbridge resume`." % (task.describe(), reason))
            self._event("critical", "uncertain_execution", msg)
            self._notify("tvbridge: CHECK MT5 (internal error)", msg, "critical")
        elif task.kind in (CLOSE, CLOSE_ALL, FLATTEN):
            if task.kind in (CLOSE_ALL, FLATTEN):
                self._note_flatten_outcome(failed=True)
            msg = "%s failed with an internal error (%s); positions may still be open. Check MT5." % (
                task.describe(), reason)
            self._event("critical", "close_failed", msg)
            self._notify("tvbridge: CLOSE FAILED", msg, "critical")
        elif task.kind == ACCOUNT:
            # A crashed poll means the kill switch did not run for this read.
            self._crashed_polls += 1
            msg = ("account poll crashed (%s); the kill switch did not run for this read (%d crash(es) so far). "
                   "Check the engine log and disk/database." % (reason, self._crashed_polls))
            self._event("critical", "account_poll_crashed", msg)
            self._notify_db_throttled("tvbridge: ACCOUNT POLL CRASHED", msg)
        else:
            self._event("warn", "internal_error", "%s: %s" % (task.describe(), reason))

    # ------------------------------------------------------------------ side channels

    def _notify(self, title: str, message: str, level: str = "info") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(title, message, level)
        except Exception:  # pragma: no cover - Notifier.send never raises
            log.exception("notifier failed")

    def _notify_db_throttled(self, title: str, message: str) -> None:
        mono = time.monotonic()
        last = self._db_error_notify_mono
        if last is not None and mono - last < DB_ERROR_NOTIFY_S:
            return
        self._db_error_notify_mono = mono
        self._notify(title, message, "critical")

    def _db_error(self, what: str, e: BaseException) -> None:
        log.exception("database/bookkeeping error while %s", what)
        msg = ("%s failed (%s: %s); the kill switch keeps running on in-memory data. Check disk space and "
               "the engine log." % (what, type(e).__name__, e))
        self._event("critical", "db_error", msg)
        self._notify_db_throttled("tvbridge: DATABASE ERROR", msg)

    def _event(self, level: str, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.store.log_event(level, kind, message, data)
        except Exception:  # pragma: no cover - Store.log_event never raises
            log.exception("cannot log event %s", kind)

    def _halt(self, reason: str) -> None:
        self.store.set_kv("halted", reason)
        log.error("new entries halted: %s", reason)

    def _sym_key(self, symbol: Any) -> str:
        s = str(symbol or "")
        return self.cfg.tv_symbol_for(s) or self.cfg.normalize_tv_symbol(s)

    def _apply_price_hint(self, sig: Signal) -> None:
        """Pass a fresh alert price to the executor (the paper simulator moves its market)."""
        if not sig.symbol or not _finite(sig.price) or float(sig.price) <= 0:  # type: ignore[arg-type]
            return
        age = (clock.utcnow() - sig.fired_at).total_seconds()
        if age > float(self.cfg.server.max_signal_age_s):
            return  # an old price (e.g. a re-submitted signal) must not move the paper market back
        try:
            self.executor.set_price_hint(sig.symbol, float(sig.price))  # type: ignore[arg-type]
        except Exception as e:
            log.warning("price hint for %s failed: %s", sig.symbol, e)
            return
        if getattr(self.executor, "name", "") == "paper":
            with self._lock:
                self._account_dirty = True   # equity moved (and an SL/TP may have been hit)

    def _floors_today(self) -> Optional[risk.Floors]:
        """Today's floors; the last good value of the same day if the database cannot be read."""
        day = self.cfg.server_date(clock.utcnow())
        try:
            floors = floors_for_day(self.cfg, self.store, day)
        except Exception as e:
            log.error("cannot read today's floors (%s); using the cached value", e)
            cached = self._floors_cache
            return cached[1] if cached is not None and cached[0] == day else None
        if floors is not None:
            self._floors_cache = (day, floors)
        return floors

    # ------------------------------------------------------------------ startup recovery

    def _recover(self) -> None:
        stuck = []
        for row in self.store.signals_with_status(["processing"]):
            targets = self._fanned_out_complete(row)
            if targets:
                # a fan-out parent sends no order itself and all its children are stored: nothing is uncertain
                self.store.set_signal_status(row["id"], "done", "FANNED_OUT: %s" % ", ".join(targets))
                log.info("sync %s was fanning out when the engine stopped; all %d children exist", row["id"],
                         len(targets))
                continue
            stuck.append(row)
        for row in stuck:
            reason = "INTERRUPTED: the engine stopped while this signal was being executed; verify MT5 manually"
            self.store.set_signal_status(row["id"], "failed", reason)
            msg = ("signal %s (%s %s) was in progress when the engine stopped. Check MT5 (positions, SL/TP) "
                   "against `tvbridge status`, then run `tvbridge resume`."
                   % (row["id"], row.get("action"), row.get("symbol") or "*"))
            log.error(msg)
            self._event("critical", "interrupted", msg, {"id": row["id"]})
            self._notify("tvbridge: INTERRUPTED order, check MT5", msg, "critical")
        if stuck:
            self._halt("INTERRUPTED: %d signal(s) were in progress when the engine stopped (%s); verify MT5 "
                       "manually, then run `tvbridge resume`" % (len(stuck), ", ".join(r["id"] for r in stuck)))
        resubmitted = 0
        for row in self.store.signals_with_status(["queued"]):
            payload = row.get("payload")
            try:
                sig = Signal.from_dict(payload) if isinstance(payload, dict) else None
            except (KeyError, TypeError, ValueError):
                sig = None
            if sig is None:
                self.store.set_signal_status(row["id"], "failed", "INTERNAL_ERROR: stored payload is unreadable")
                continue
            self.submit_signal(sig)
            resubmitted += 1
        if resubmitted:
            log.info("re-submitted %d queued signal(s) from before the restart", resubmitted)

    # ------------------------------------------------------------------ account polling

    def _poll_account(self, reason: str, adopt: Optional[Tuple[Signal, TradePlan]] = None
                      ) -> Optional[AccountSnapshot]:
        """read_account -> snapshot -> reconcile -> rollover -> kill switch. None if the read failed.

        Bookkeeping failures (database writes) never stop the kill switch: it always runs on
        the snapshot just read.
        """
        if adopt is not None:
            self._pending_adoptions.append((adopt[0], adopt[1], time.monotonic() + PENDING_ADOPTION_S))
        try:
            snap = self.executor.read_account()
        except Exception as e:
            self._account_read_failed(e, reason)
            return None
        recovered = self._read_failures >= READ_FAILURE_WARN
        self._read_failures = 0
        self._last_read_error = ""
        with self._lock:
            prev = self._last_snapshot
            self._prev_snapshot = prev
            self._last_snapshot = snap
            self._account_dirty = False
        try:
            self.store.add_snapshot(snap)
        except Exception as e:
            self._db_error("saving the account snapshot", e)
        if recovered:
            msg = "account reads work again: balance %s, equity %s" % (_money(snap.balance), _money(snap.equity))
            self._event("info", "account_read_ok", msg)
            self._notify("tvbridge: account readable again", msg, "info")
        self._track_positions_note(snap)
        try:
            self._reconcile(snap)
        except Exception as e:
            self._db_error("reconciling positions", e)
        try:
            self._maybe_rollover()
            self._refine_reference(snap)
        except Exception as e:
            self._db_error("updating the daily reference", e)
        return self._check_kill(snap, prev)

    def _account_read_failed(self, e: BaseException, reason: str) -> None:
        self._read_failures += 1
        self._last_read_error = _reason_of(e)
        n = self._read_failures
        log.warning("account read failed (%s, %d in a row): %s", reason, n, self._last_read_error)
        if n == READ_FAILURE_WARN:
            msg = ("%d account reads in a row failed (last: %s). New entries are blocked until a read "
                   "succeeds, and the kill switch cannot see equity." % (n, self._last_read_error))
            self._event("warn", "account_read_failed", msg)
            self._notify("tvbridge: cannot read the account", msg, "warn")

    def _track_positions_note(self, snap: AccountSnapshot) -> None:
        """Warn once when the executor keeps reporting an unverifiable position list."""
        note = snap.positions_note if snap.positions is None else ""
        if note:
            self._positions_unknown_reads += 1
            if self._positions_unknown_reads == POSITIONS_UNKNOWN_WARN:
                msg = ("%d account reads in a row could not verify the MT5 position list (%s). New entries are "
                       "refused (POSITIONS_UNCERTAIN); closes still run but report TOOLBOX_INCOMPLETE. Make the "
                       "Toolbox Trade list fully visible (window size as calibrated, header and every row shown)."
                       % (self._positions_unknown_reads, note))
                self._event("warn", "positions_unknown", msg)
                self._notify("tvbridge: position list not verifiable", msg, "warn")
        else:
            if self._positions_unknown_reads >= POSITIONS_UNKNOWN_WARN:
                self._event("info", "positions_known", "the MT5 position list is complete again")
                self._notify("tvbridge: position list readable again", "the MT5 position list is complete again",
                             "info")
            self._positions_unknown_reads = 0
        self._positions_note = note

    def _close_corroborated(self, basis: Optional[Tuple[float, Optional[float]]], snap: AccountSnapshot,
                            min_change: float = BALANCE_EPS) -> str:
        """Why the balance/margin prove that a close was booked since ``basis`` ("" = no proof)."""
        if basis is None:
            return ""
        bal0, m0 = basis
        if _finite(snap.balance) and _finite(bal0) and abs(float(snap.balance) - float(bal0)) > min_change:
            return "balance %s -> %s" % (_money(bal0), _money(snap.balance))
        if _finite(m0) and _finite(snap.margin) and float(m0) > 0 \
                and float(snap.margin) < float(m0) * (1.0 - MARGIN_DROP_REL):  # type: ignore[arg-type]
            return "margin %s -> %s" % (_money(m0), _money(snap.margin))
        return ""

    def _close_proof(self, pid: int, row: Dict[str, Any], snap: AccountSnapshot) -> str:
        """Proof that ledger row ``pid`` was closed: against the account when the row was last seen
        in MT5; for a row never seen since the fill (or since the start), against the account
        before it, where a change must exceed the commission an entry itself may book."""
        if pid in self._seen_basis:
            return self._close_corroborated(self._seen_basis[pid], snap)
        lots = float(row.get("lots") or 0.0) if _finite(row.get("lots")) else 0.0
        entry_cost = self.cfg.commission_per_lot(row.get("symbol")) * lots + BALANCE_EPS
        basis = self._open_basis.get(pid, self._basis_fallback)
        return self._close_corroborated(basis, snap, entry_cost)

    def _account_basis(self) -> Optional[Tuple[float, Optional[float]]]:
        """(balance, margin) of the latest read, the basis for proving a later close."""
        with self._lock:
            snap = self._last_snapshot
        return (float(snap.balance), snap.margin) if snap is not None else None

    def _consume_resume_ack(self, now: datetime) -> None:
        """`tvbridge resume` confirms that ledger rows MT5 no longer shows are closed."""
        raw = self.store.get_kv("resume_ack")
        if raw is None:
            return
        self.store.set_kv("resume_ack", None)
        try:
            ack = clock.from_iso(raw)
        except (TypeError, ValueError):
            ack = now
        for pid, info in list(self._uncertain_rows.items()):
            if info["since"] > ack:
                continue
            self.store.close_ledger_position(pid, "closed_on_server", now)
            self._uncertain_rows.pop(pid, None)
            self._seen_basis.pop(pid, None)
            self._open_basis.pop(pid, None)
            self._missing_counts.pop(pid, None)
            msg = "%s: closed in the ledger as confirmed by `tvbridge resume`" % info["desc"]
            log.info(msg)
            self._event("info", "closed_on_server", msg, {"pid": pid, "confirmed_by": "resume"})

    def _reconcile(self, snap: AccountSnapshot) -> None:
        """Compare what the executor shows with the ledger (no-op when positions are unknown).

        A ledger row MT5 no longer shows is closed ("closed_on_server") only after
        RECONCILE_MISSES reads without it *and* proof that a close was booked (balance changed
        or margin dropped since the row was last seen). Without proof the row stays open (its
        risk still counts) and entries are refused (POSITIONS_UNCERTAIN) until the row is seen
        again, the balance confirms the close, or `tvbridge resume` / a flatten settles it.
        """
        now = clock.utcnow()
        self._consume_resume_ack(now)
        observed = snap.positions
        rows = self.store.open_ledger_positions()
        open_pids = {int(r["pid"]) for r in rows}
        for d in (self._uncertain_rows, self._sl_flags, self._seen_basis, self._missing_counts, self._open_basis):
            for pid in [k for k in d if k not in open_pids]:
                d.pop(pid, None)
        self._expire_pending_adoptions()
        if observed is None:
            return
        matched, unmatched_obs, unmatched_rows = self._match_positions(list(observed), rows)

        for p, row in matched:
            pid = int(row["pid"])
            self._seen_basis[pid] = (float(snap.balance), snap.margin)
            self._missing_counts.pop(pid, None)
            info = self._uncertain_rows.pop(pid, None)
            if info is not None:
                msg = "%s is visible in MT5 again" % info["desc"]
                log.info(msg)
                self._event("info", "position_visible", msg, {"pid": pid})
            self._check_observed_sl(row, p, now)

        # An empty list while MT5 reports margin in use is an unreadable list, not "flat".
        skip_misses = not observed and _finite(snap.margin) and float(snap.margin) > BALANCE_EPS  # type: ignore[arg-type]
        counts = {}  # type: Dict[int, int]
        for row in unmatched_rows:
            pid = int(row["pid"])
            if skip_misses:
                if self._missing_counts.get(pid):
                    counts[pid] = self._missing_counts[pid]
                continue
            n = self._missing_counts.get(pid, 0) + 1
            counts[pid] = n
            if n < RECONCILE_MISSES:
                continue
            desc = "%s %s %s (ticket %s, ledger #%d)" % (row.get("side"), row.get("lots"), row.get("symbol"),
                                                         row.get("ticket") or "?", pid)
            proof = self._close_proof(pid, row, snap)
            if proof:
                self.store.close_ledger_position(pid, "closed_on_server", now)
                counts.pop(pid, None)
                self._seen_basis.pop(pid, None)
                self._uncertain_rows.pop(pid, None)
                msg = ("%s is no longer shown in MT5 (SL/TP hit or closed by hand; %s); ledger position closed"
                       % (desc, proof))
                log.info(msg)
                self._event("info", "closed_on_server", msg, {"pid": pid, "signal_id": row.get("signal_id")})
                self._notify("tvbridge: position closed on server", msg, "info")
            elif pid not in self._uncertain_rows:
                self._uncertain_rows[pid] = {"desc": desc, "since": now}
                msg = ("%s is not visible in MT5 but the balance did not change, so it may still be open. Its "
                       "risk stays booked and new entries are refused (POSITIONS_UNCERTAIN). Check MT5: if it is "
                       "closed, run `tvbridge resume`; if it is open, make the Toolbox show every row." % desc)
                log.warning(msg)
                self._event("warn", "position_not_visible", msg, {"pid": pid})
                self._notify("tvbridge: position not visible in MT5", msg, "warn")
        self._missing_counts = counts

        if unmatched_obs and self._pending_adoptions:
            for entry in list(self._pending_adoptions):
                sig, plan, _deadline = entry
                adopted = self._adopt(sig, plan, unmatched_obs, now)
                if adopted is not None:
                    unmatched_obs = [p for p in unmatched_obs if p is not adopted]
                    self._pending_adoptions = [e for e in self._pending_adoptions if e is not entry]

        keys = set()  # type: Set[Tuple[str, str]]
        for p in unmatched_obs:
            key = (self._sym_key(p.symbol), p.side)
            keys.add(key)
            if key in self._untracked_notified:
                continue
            effect = ("New entries are blocked while it is open (risk.block_untracked_positions)."
                      if self.cfg.risk.block_untracked_positions else "It is not managed by tvbridge.")
            msg = "MT5 shows %s %s %s (ticket %s) that tvbridge did not open. %s" % (
                p.side, p.lots, p.symbol, p.ticket or "?", effect)
            log.warning(msg)
            self._event("warn", "untracked_position", msg, {"position": p.to_dict()})
            self._notify("tvbridge: untracked position", msg, "warn")
        with self._lock:
            self._untracked = list(unmatched_obs)
            self._untracked_notified = keys

    def _expire_pending_adoptions(self) -> None:
        mono = time.monotonic()
        self._pending_adoptions = [e for e in self._pending_adoptions if e[2] > mono]

    def _check_observed_sl(self, row: Dict[str, Any], p: ObservedPosition, now: datetime) -> None:
        """Compare the stop-loss MT5 shows with the ledger: missing -> block entries; widened ->
        re-book the risk (never lowered)."""
        pid = int(row["pid"])
        desc = "%s %s %s #%s" % (row.get("symbol"), row.get("side"), row.get("lots"), row.get("ticket") or pid)
        if p.sl_missing:
            if pid not in self._sl_flags:
                self._sl_flags[pid] = "%s shows no stop-loss in MT5" % desc
                msg = ("SL_MISSING_ON_SERVER: %s has no stop-loss on the server; its loss is unbounded. New entries "
                       "are refused until it has one. Set the stop-loss in MT5 now." % desc)
                log.critical(msg)
                self._event("critical", "sl_missing_on_server", msg, {"pid": pid})
                self._notify("tvbridge: STOP-LOSS MISSING", msg, "critical")
            return
        if pid in self._sl_flags and p.sl is not None:
            self._sl_flags.pop(pid, None)
            self._event("info", "sl_restored", "%s has a stop-loss again (%s)" % (desc, p.sl), {"pid": pid})
        if p.sl is None:
            return
        entry = row.get("entry_price") if _finite(row.get("entry_price")) else p.open_price
        old_sl = row.get("sl")
        if not (_finite(entry) and _finite(old_sl) and _finite(row.get("risk_usd"))):
            return
        entry, old_sl, new_sl = float(entry), float(old_sl), float(p.sl)  # type: ignore[arg-type]
        side = str(row.get("side") or "")
        widened = (side == "buy" and new_sl < old_sl) or (side == "sell" and new_sl > old_sl)
        d_old = abs(entry - old_sl)
        if not widened or d_old <= 0 or abs(new_sl - old_sl) <= 1e-9 * max(1.0, abs(old_sl)):
            return
        new_risk = float(row["risk_usd"]) * abs(entry - new_sl) / d_old
        self.store.update_position_risk(pid, new_sl, new_risk)
        msg = ("%s: the stop-loss in MT5 (%s) is farther than booked (%s); booked risk raised from %s to %s USD"
               % (desc, new_sl, old_sl, _money(row["risk_usd"]), _money(new_risk)))
        log.warning(msg)
        self._event("critical", "sl_widened", msg, {"pid": pid})
        self._notify("tvbridge: stop-loss widened", msg, "critical")

    def _match_positions(self, observed: List[ObservedPosition], rows: List[Dict[str, Any]]
                         ) -> Tuple[List[Tuple[ObservedPosition, Dict[str, Any]]], List[ObservedPosition],
                                    List[Dict[str, Any]]]:
        """Pair observed positions with open ledger rows.

        Returns (matched pairs, unmatched observed, unmatched rows). Pass 1 pairs equal
        tickets on the same symbol (an equal ticket on another symbol is a mismatch: a critical
        alert, never a pair); pass 2 pairs symbol + side where a ticket is missing on either
        side; pass 3 pairs the remaining symbol + side matches (tickets that differ, e.g. an
        order number recorded instead of the position number).
        """
        remaining = list(rows)
        matched = []  # type: List[Tuple[ObservedPosition, Dict[str, Any]]]

        def take(pred: Callable[[Dict[str, Any]], bool]) -> Optional[Dict[str, Any]]:
            for i, row in enumerate(remaining):
                if pred(row):
                    return remaining.pop(i)
            return None

        rest = []  # type: List[ObservedPosition]
        for p in observed:
            ticket = str(p.ticket) if p.ticket else ""
            row = None
            if ticket:
                same_ticket = [r for r in remaining if r.get("ticket") and str(r["ticket"]) == ticket]
                ok = [r for r in same_ticket if self._sym_key(r.get("symbol")) == self._sym_key(p.symbol)]
                if same_ticket and not ok and ticket not in self._mismatch_notified:
                    self._mismatch_notified.add(ticket)
                    msg = ("MT5 shows ticket #%s on %s but the ledger has it on %s: the order may have gone to "
                           "the wrong instrument. Check MT5 now." % (ticket, p.symbol, same_ticket[0].get("symbol")))
                    log.critical(msg)
                    self._event("critical", "ticket_symbol_mismatch", msg, {"ticket": ticket})
                    self._notify("tvbridge: TICKET/SYMBOL MISMATCH", msg, "critical")
                if ok:
                    row = take(lambda r, t=ok[0]: r is t)
            if row is None:
                rest.append(p)
            else:
                matched.append((p, row))

        unmatched = []  # type: List[ObservedPosition]
        for p in rest:
            key, side = self._sym_key(p.symbol), str(p.side).lower()

            def same(r: Dict[str, Any]) -> bool:
                return self._sym_key(r.get("symbol")) == key and str(r.get("side") or "").lower() == side

            row = take(lambda r: same(r) and (not p.ticket or not r.get("ticket")))
            if row is None:
                row = take(same)
            if row is None:
                unmatched.append(p)
            else:
                matched.append((p, row))
        return matched, unmatched, remaining

    def _adopt(self, sig: Signal, plan: TradePlan, candidates: List[ObservedPosition],
               now: datetime) -> Optional[ObservedPosition]:
        """After an uncertain entry: record an untracked position on that symbol/side in the ledger."""
        key = self._sym_key(sig.symbol)
        cands = [p for p in candidates if self._sym_key(p.symbol) == key and p.side == sig.action]
        if not cands:
            return None
        p = min(cands, key=lambda c: abs(float(c.lots) - float(plan.lots)))
        sl = p.sl if p.sl is not None else sig.sl
        entry = p.open_price if _finite(p.open_price) else sig.price
        risk_usd = self._booked_risk(sig, plan, float(p.lots), entry, sl)[0]
        pid = self.store.add_position(sig.id, sig.symbol, p.side, p.lots, entry, sl, p.tp or sig.tp, risk_usd,
                                      now, ticket=p.ticket)
        basis = self._account_basis()
        if basis is not None:
            self._seen_basis[pid] = basis      # it is visible in this very read
        sl_note = ("Its stop-loss %s was read in MT5." % p.sl if p.sl is not None else
                   "Its stop-loss was NOT readable in MT5 (booked as the signal's %s): check it." % sig.sl)
        # Verified fill: the one new position on that symbol/side is exactly the order (size, ticket, a
        # stop-loss on the losing side of its entry) and the halt is this signal's own -> resume entries.
        halted = self.store.get_kv("halted") or ""
        sl_ok = (p.sl is not None and not p.sl_missing and _finite(entry)
                 and ((p.side == "buy" and float(p.sl) < float(entry))          # type: ignore[arg-type]
                      or (p.side == "sell" and float(p.sl) > float(entry))))    # type: ignore[arg-type]
        verified = (bool(self.cfg.risk.resume_after_verified_fill) and len(cands) == 1 and bool(p.ticket)
                    and sl_ok and abs(float(p.lots) - float(plan.lots)) < 1e-9
                    and halted.startswith("UNCERTAIN_EXECUTION: signal %s " % sig.id))
        if verified:
            self.store.set_kv("halted", None)
            tail = ("Entries resumed automatically: it is the only new position there and matches the order "
                    "(risk.resume_after_verified_fill).")
        else:
            tail = "Entries stay halted until `tvbridge resume`."
        msg = ("adopted %s %s %s (ticket %s) into the ledger as #%d after the uncertain execution of signal %s, "
               "risk %s USD. %s %s"
               % (p.side, p.lots, p.symbol, p.ticket or "?", pid, sig.id, _money(risk_usd), sl_note, tail))
        log.warning(msg)
        self._event("warn", "adopted_position", msg, {"pid": pid, "signal_id": sig.id, "position": p.to_dict(),
                                                      "sl_observed": p.sl is not None,
                                                      "entries_resumed": verified})
        self._notify("tvbridge: position adopted", msg, "warn")
        return p

    def _booked_risk(self, sig: Signal, plan: TradePlan, lots: float, entry: Any, sl: Any
                     ) -> Tuple[float, float, str]:
        """(risk to book, risk from the actual entry, note). Never less than the plan's risk
        (scaled up for a larger size); higher when the fill/entry price is worse."""
        base = float(plan.risk_usd)
        if plan.lots > 0 and lots > plan.lots:
            base = base * lots / float(plan.lots)
        spec = self.cfg.spec_for(sig.symbol)
        q2usd = (plan.details or {}).get("q2usd")
        if spec is None or not (_finite(entry) and _finite(sl) and _finite(q2usd)) or float(entry) <= 0:
            return base, 0.0, ""
        actual = float(lots) * risk.per_lot_loss_usd(float(entry), float(sl), spec, float(q2usd),
                                                     self.cfg.commission_per_lot(sig.symbol))
        note = ""
        if _finite(sig.price) and _finite(sig.sl):
            dist = abs(float(sig.price) - float(sig.sl))  # type: ignore[arg-type]
            if dist > 0 and abs(float(entry) - float(sig.price)) > 3 * dist:  # type: ignore[arg-type]
                note = "implausible fill price %s (alert price %s, SL %s)" % (entry, sig.price, sig.sl)
        return max(base, actual), actual, note

    def _check_kill(self, snap: AccountSnapshot, prev: Optional[AccountSnapshot] = None) -> AccountSnapshot:
        """Kill switch: equity at/below the kill floor -> halt, flatten, keep flattening leftovers.

        Returns the snapshot it judged (a confirming re-read replaces an implausible read).
        """
        floors = self._floors_today()
        if floors is None:
            return snap
        reason = risk.evaluate_kill(snap, floors)
        if reason is None:
            return snap
        why = self._implausible(snap, prev)
        if why:
            snap2 = self._confirm_read(why)
            if snap2 is not None and risk.evaluate_kill(snap2, floors) is None:
                msg = ("ignored an implausible equity read %s (%s): a re-read shows equity %s above the kill floor %s"
                       % (_money(snap.equity), why, _money(snap2.equity), _money(floors.kill_floor)))
                log.warning(msg)
                self._event("warn", "equity_misread", msg)
                return snap2
            if snap2 is not None:
                snap = snap2
                reason = risk.evaluate_kill(snap2, floors) or reason
        try:
            halted = self.store.get_kv("halted") or ""
        except Exception as e:
            log.error("cannot read kv halted (%s)", e)
            halted = ""
        if self._kill_unrecorded and not halted.startswith("KILL"):
            # the earlier kill could not be written: it still counts, and is written now if possible
            try:
                self.store.set_kv("halted", self._kill_unrecorded)
                self._kill_unrecorded = ""
            except Exception:
                pass
            halted = "KILL (not recorded)"
        mono = time.monotonic()
        if not halted.startswith("KILL"):
            # flatten first: a failing database write must never stop it
            if self._enqueue_flatten("kill switch"):
                self._kill_next_retry_mono = float("inf")      # wait for this flatten to finish
            try:
                self.store.set_kv("halted", reason)
            except Exception as e:
                log.error("cannot record the kill halt: %s", e)
                self._kill_unrecorded = reason
            msg = "%s. Closing every position; new entries are halted until `tvbridge resume`." % reason
            log.critical(msg)
            self._event("critical", "kill_switch", msg, {"equity": snap.equity, "balance": snap.balance,
                                                         "floors": floors.to_dict()})
            self._notify("tvbridge: KILL SWITCH", msg, "critical")
            return snap
        # Already killed: keep closing leftovers, on a short backoff after a failed flatten.
        if self._leftovers(snap):
            nxt = self._kill_next_retry_mono
            if nxt is None or mono >= nxt:
                if self._enqueue_flatten("kill switch: positions still open"):
                    self._kill_next_retry_mono = float("inf")
                    self._event("warn", "kill_switch", "kill switch active and positions are still open; "
                                                       "flattening again")
        else:
            self._kill_fail_streak = 0
            self._kill_next_retry_mono = None
        return snap

    def _leftovers(self, snap: AccountSnapshot) -> bool:
        """Evidence that positions are still open (rows, ledger, margin, or an unverifiable list)."""
        if snap.positions:
            return True
        if snap.positions is None and snap.positions_note:
            return True
        if _finite(snap.margin) and float(snap.margin) > BALANCE_EPS:  # type: ignore[arg-type]
            return True
        try:
            return bool(self.store.open_ledger_positions())
        except Exception:
            return True

    def _implausible(self, snap: AccountSnapshot, prev: Optional[AccountSnapshot]) -> str:
        """Why a kill-level equity read looks like an OCR misread ("" = plausible)."""
        if _finite(snap.balance) and float(snap.balance) > 0 and float(snap.equity) < 0.5 * float(snap.balance):
            return "equity below half the balance"
        if prev is not None and _finite(prev.equity) and float(prev.equity) > 0:
            move = abs(float(snap.equity) - float(prev.equity)) / float(prev.equity)
            if move > KILL_IMPLAUSIBLE_MOVE:
                return "equity moved %.1f %% since the previous read" % (100.0 * move)
        return ""

    def _confirm_read(self, why: str) -> Optional[AccountSnapshot]:
        log.warning("kill-level equity read looks implausible (%s); re-reading once", why)
        if self.kill_confirm_delay_s > 0:
            time.sleep(self.kill_confirm_delay_s)
        try:
            snap2 = self.executor.read_account()
        except Exception as e:
            log.error("confirming account read failed (%s): acting on the first read", _reason_of(e))
            return None
        with self._lock:
            self._prev_snapshot = self._last_snapshot
            self._last_snapshot = snap2
        try:
            self.store.add_snapshot(snap2)
        except Exception as e:
            self._db_error("saving the account snapshot", e)
        return snap2

    def _note_flatten_outcome(self, failed: bool) -> None:
        """Set when the kill switch may flatten again: soon after a failure, slower when it keeps failing."""
        mono = time.monotonic()
        if failed:
            self._kill_fail_streak += 1
            n = self._kill_fail_streak
            delay = KILL_REFLATTEN_S if n > KILL_SLOW_AFTER else KILL_RETRY_BACKOFF_S[
                min(n - 1, len(KILL_RETRY_BACKOFF_S) - 1)]
        else:
            self._kill_fail_streak = 0
            delay = KILL_RETRY_BACKOFF_S[0]
        self._kill_next_retry_mono = mono + delay

    # ------------------------------------------------------------------ day rollover

    def _stale_basis_limit_s(self) -> float:
        return max(4.0 * float(self.cfg.executor.gui.account_poll_s), float(self.cfg.risk.equity_max_age_s),
                   ROLLOVER_STALE_MIN_S)

    def _reference_basis_before(self, midnight: datetime) -> Optional[Dict[str, Any]]:
        """The last snapshot row before midnight, unless it is an outlier against the read
        before it (then that earlier read is used)."""
        row = self.store.last_snapshot_row_before(midnight)
        if row is None:
            return None
        try:
            ts = clock.from_iso(row["ts"])
            prev = self.store.last_snapshot_row_before(ts)
        except (KeyError, ValueError):
            return row
        if prev is None:
            return row
        try:
            age = (ts - clock.from_iso(prev["ts"])).total_seconds()
        except (KeyError, ValueError):
            return row
        if age > 600:
            return row
        for k in ("balance", "equity"):
            a, b = row.get(k), prev.get(k)
            if _finite(a) and _finite(b) and float(b) > 0 and abs(float(a) - float(b)) / float(b) > REFERENCE_OUTLIER_REL:
                msg = ("ignored the last snapshot before midnight (%s %s %s vs %s 5 min earlier): it looks like an "
                       "OCR misread" % (row["ts"], k, _money(a), _money(b)))
                log.warning(msg)
                self._event("warn", "reference_outlier", msg)
                return prev
        return row

    def _maybe_rollover(self) -> Optional[str]:
        """Set today's daily reference when the server day changes. Returns the new day or None.

        The basis is the last snapshot before server midnight plus today's snapshots. A basis
        older than ``_stale_basis_limit_s`` (reads failing or engine down over midnight) is
        not trusted alone: the rollover waits for a fresh read, and the result is stored as a
        "stale_estimate" (entries refused until `tvbridge set-reference`) unless nothing can
        have changed (no positions before and after, same balance).
        """
        with self._rollover_lock:
            cfg = self.cfg
            now = clock.utcnow()
            today = cfg.server_date(now)
            key = today.isoformat()
            if self.store.get_kv("day") == key:
                return None
            existing = self.store.get_day_state(today)
            if existing is not None:   # e.g. `tvbridge set-reference` before the engine rolled
                self.store.set_kv("day", key)
                self._rolled_once = True
                msg = "server day %s: keeping the existing daily reference %s (source %s)" % (
                    key, _money(existing.get("reference")), existing.get("source"))
                log.info(msg)
                self._event("info", "day_rollover", msg)
                return key
            source = "rollover" if self._rolled_once else "startup"
            midnight = cfg.server_midnight_utc(today)
            before_row = self._reference_basis_before(midnight)
            last_before = self.store.snapshot_from_row(before_row) if before_row is not None else None
            today_rows = self.store.snapshot_rows_between(midnight, now)
            today_snaps = [self.store.snapshot_from_row(r) for r in today_rows]
            # "History" means a snapshot from before today's server midnight. Snapshots taken
            # today (e.g. by the startup read just before this) say nothing about yesterday's
            # close, so on a fresh install the initial balance stays a (conservative) candidate.
            have_history = last_before is not None
            age = (midnight - last_before.ts).total_seconds() if last_before is not None else None
            stale = age is not None and age > self._stale_basis_limit_s()
            if not today_snaps and (last_before is None or stale):
                log.debug("no fresh basis for today's daily reference yet; waiting for an account read")
                return None
            if stale:
                first = today_rows[0]
                exact = (before_row is not None and before_row.get("n_positions") == 0
                         and first.get("n_positions") == 0
                         and abs(float(first["balance"]) - float(before_row["balance"])) <= BALANCE_EPS)
                if not exact:
                    source = "stale_estimate"
            initial = float(cfg.account.initial_balance)
            ref = risk.estimate_reference(last_before, today_snaps, initial, have_history)
            if ref is None:
                log.debug("no data for today's daily reference yet; will retry")
                return None
            try:
                floors = risk.compute_floors(ref, cfg)
            except ValueError as e:
                log.error("cannot compute floors for %s: %s", key, e)
                return None
            snaps = ([last_before] if last_before is not None else []) + list(today_snaps)
            balances = [float(s.balance) for s in snaps if _finite(s.balance)]
            if not have_history:
                balances.append(initial)
            equities = [float(s.equity) for s in snaps if _finite(s.equity)]
            ref_balance = max(balances) if balances else ref
            ref_equity = max(equities) if equities else ref_balance
            self.store.set_day_state(today, ref_balance, ref_equity, source)
            self.store.set_kv("day", key)
            self._rolled_once = True

            basis = []  # type: List[str]
            if last_before is not None:
                basis.append("last snapshot before midnight (%s%s)" % (
                    clock.iso(last_before.ts), ", %.0f min before midnight" % (age / 60.0) if stale else ""))
            if today_snaps:
                basis.append("%d snapshot(s) since midnight" % len(today_snaps))
            if not have_history:
                basis.append("initial balance (no history)")
            msg = ("server day %s: daily reference %s (%s; from %s). Entry floor %s, kill floor %s, hard daily "
                   "floor %s, hard max floor %s." % (key, _money(ref), source, ", ".join(basis) or "?",
                                                     _money(floors.entry_floor), _money(floors.kill_floor),
                                                     _money(floors.hard_daily_floor), _money(floors.hard_max_floor)))
            level = "info"
            if source == "stale_estimate":
                level = "critical"
                msg += (" The basis is stale, so the true reference may be higher: new entries are refused until "
                        "you check the Hantec dashboard and run `tvbridge set-reference VALUE`.")
            prev_ds = self.store.get_day_state(today - timedelta(days=1))
            if prev_ds is not None and _finite(prev_ds.get("reference")) and float(prev_ds["reference"]) > 0:
                jump = abs(ref - float(prev_ds["reference"])) / float(prev_ds["reference"])
                if jump > REFERENCE_JUMP_WARN_REL:
                    msg += (" Note: %.1f %% away from yesterday's reference %s; compare with the Hantec dashboard."
                            % (100.0 * jump, _money(prev_ds["reference"])))
                    if level == "info":
                        level = "warn"
            log.log(logging.CRITICAL if level == "critical" else logging.INFO, msg)
            self._event(level, "day_rollover", msg, {"server_date": key, "source": source,
                                                     "floors": floors.to_dict()})
            self._notify("tvbridge: new server day", msg, level)
            return key

    def _refine_reference(self, snap: AccountSnapshot) -> None:
        """In the first polls after midnight, raise (never lower) an estimated reference with the
        new read: a TP filled in the last seconds before midnight shows up only now."""
        cfg = self.cfg
        now = clock.utcnow()
        today = cfg.server_date(now)
        midnight = cfg.server_midnight_utc(today)
        if (now - midnight).total_seconds() > REFERENCE_REFINE_POLLS * float(cfg.executor.gui.account_poll_s):
            return
        ds = self.store.get_day_state(today)
        if ds is None or ds.get("source") not in ("rollover", "startup", "stale_estimate"):
            return
        cand = max(float(snap.balance), float(snap.equity))
        if not _finite(ds.get("reference")) or cand <= float(ds["reference"]) + BALANCE_EPS:
            return
        rb = max(float(ds.get("ref_balance") or 0.0), float(snap.balance))
        re_ = max(float(ds.get("ref_equity") or 0.0), float(snap.equity))
        self.store.set_day_state(today, rb, re_, ds["source"])
        msg = "server day %s: daily reference raised from %s to %s by the first read after midnight" % (
            today.isoformat(), _money(ds["reference"]), _money(max(rb, re_)))
        log.info(msg)
        self._event("info", "day_reference_raised", msg)

    # ------------------------------------------------------------------ risk state

    def _positions_uncertain_text(self) -> str:
        rows = dict(self._uncertain_rows)        # a C-level copy: safe from the scheduler thread
        parts = [info["desc"] + " is not visible in MT5" for info in rows.values()]
        with self._lock:
            snap = self._last_snapshot
        if snap is not None and snap.positions is None and snap.positions_note:
            parts.append(snap.positions_note)
        return "; ".join(parts)

    def _risk_state(self, now: datetime) -> risk.RiskState:
        with self._lock:
            snap = self._last_snapshot
            prev = self._prev_snapshot
            dirty = self._account_dirty
            untracked = list(self._untracked)
        confirmed_balance = confirmed_equity = None  # type: Optional[float]
        if snap is None:
            snap = self.store.latest_snapshot()   # positions unknown (None)
            observed = None
        else:
            # After a fill/close without a fresh read the list is stale: unknown -> ledger is used.
            observed = None if dirty else snap.positions
            if prev is not None and (snap.ts - prev.ts).total_seconds() <= 600:
                confirmed_balance = min(float(snap.balance), float(prev.balance))
                confirmed_equity = min(float(snap.equity), float(prev.equity))
        day = self.cfg.server_date(now)
        ds = self.store.get_day_state(day)
        reference_note = ""
        if ds is not None and ds.get("source") == "stale_estimate":
            reference_note = ("today's daily reference %s is an estimate from a stale snapshot"
                              % _money(ds.get("reference")))
        open_rows = self.store.open_ledger_positions()
        open_pids = {int(r["pid"]) for r in open_rows}
        return risk.RiskState(
            now=now,
            snapshot=snap,
            floors=floors_for_day(self.cfg, self.store, day),
            open_positions=open_rows,
            observed_positions=observed,
            untracked_positions=untracked,
            trades_today=self.store.count_trades_on_server_day(day, self.cfg.server_offset_for_day(day)),
            paused=(self.store.get_kv("paused") or "") not in ("", "0"),
            halted=self.store.get_kv("halted") or "",
            positions_uncertain=self._positions_uncertain_text(),
            sl_issues=[v for k, v in dict(self._sl_flags).items() if k in open_pids],
            reference_note=reference_note,
            confirmed_balance=confirmed_balance,
            confirmed_equity=confirmed_equity,
        )

    def _entry_block_reason(self) -> Optional[str]:
        """Why an entry must not be sent right now (halt, pause, flatten), or None.

        Checked right before ``open_market`` and, by the GUI executor, right before the click.
        """
        halted = self.store.get_kv("halted") or ""
        if halted:
            return "HALTED: %s" % halted
        if (self.store.get_kv("paused") or "") not in ("", "0"):
            return "PAUSED: entries were paused (%s)" % (self.store.get_kv("pause_reason") or "no reason")
        with self._lock:
            pending = self._flatten_pending
        raw = self.store.get_kv("command")
        if raw is not None:
            try:
                cmd = json.loads(raw)
            except ValueError:
                cmd = {"cmd": raw}
            if isinstance(cmd, dict) and str(cmd.get("cmd") or "").strip().lower() == "flatten":
                pending = True
        if pending:
            return "FLATTEN_PENDING: a flatten was requested"
        return None

    # ------------------------------------------------------------------ OPEN

    def _do_open(self, task: Task) -> None:
        sig = task.signal
        assert sig is not None
        self.store.set_signal_status(sig.id, "processing")
        self._apply_price_hint(sig)
        with self._lock:
            snap, dirty = self._last_snapshot, self._account_dirty
        now = clock.utcnow()
        if snap is None or dirty or (now - snap.ts).total_seconds() > ACCOUNT_REFRESH_S:
            self._poll_account("before entry %s" % sig.id)

        # Reversal first (owner decision D1): with reverse_on_opposite, opposite positions on the
        # symbol are closed as a close -- even if the entry itself will then be refused (paused,
        # halted, stale, no SL, outside the window ...). The entry is judged afterwards on the
        # refreshed state.
        # A reversal close follows the same lateness limit as a close alert at the webhook
        # (server.EXIT_MAX_AGE_S): a reversal re-queued after a long outage must not close anything.
        reversal_rehearsed = False
        reversal_done = False
        if self.cfg.risk.reverse_on_opposite:
            opposite = self._opposite_positions(sig)
            age_s = (clock.utcnow() - sig.fired_at).total_seconds()
            if opposite and age_s > EXIT_MAX_AGE_S:
                msg = ("reversal %s %s fired %.0f s ago (limit %.0f s): %s left open"
                       % (sig.action, sig.symbol, age_s, EXIT_MAX_AGE_S, ", ".join(opposite)))
                log.warning(msg)
                self._event("warn", "reversal_too_late", msg, {"id": sig.id})
                self._notify("tvbridge: late reversal not acted on", msg + ". Check MT5.", "warn")
                opposite = []
            if opposite:
                ok, reversal_rehearsed = self._close_for_reversal(sig, opposite)
                if not ok:
                    return
                reversal_done = True
                self._poll_account("after the reversal close for %s" % sig.id)

        if self._entry_superseded(sig):
            return

        now = clock.utcnow()
        plan = risk.plan_entry(sig, self._risk_state(now), self.cfg)
        if not plan.approved:
            self._entry_rejected(sig, plan)
            return
        # After a reversal close, close_first may still list a ledger row that MT5 no longer shows
        # (e.g. stopped out, not reconciled yet): fine. MT5 still showing the opposite side is not.
        if plan.close_first and not reversal_rehearsed and (not reversal_done or self._opposite_still_visible(sig)):
            desc = ", ".join("%s %s %s" % (p.side, p.lots, p.symbol) for p in plan.close_first)
            reason = ("REVERSAL_CLOSE_FAILED: %s still open after the reversal close; the %s entry was not placed"
                      % (desc, sig.action))
            self.store.set_signal_status(sig.id, "rejected", reason, {"plan": _plan_dict(plan)})
            log.error(reason)
            self._event("critical", "reversal_close_failed", reason, {"id": sig.id})
            self._notify("tvbridge: REVERSAL CLOSE FAILED", reason + ". Check MT5.", "critical")
            return

        # Last checks right before the order: age (a reversal can take a while) and halts,
        # pauses or a flatten that arrived meanwhile.
        age = (clock.utcnow() - clock.ensure_utc(sig.fired_at)).total_seconds()
        if age > float(self.cfg.risk.entry_max_delay_s):
            self._entry_rejected(sig, TradePlan(False, "STALE_SIGNAL: signal fired %.1f s ago (entry_max_delay_s=%s)"
                                                % (age, self.cfg.risk.entry_max_delay_s)))
            return
        block = self._entry_block_reason()
        if block:
            self._entry_rejected(sig, TradePlan(False, block + "; the entry was not placed"))
            return

        req = self._order_request(sig, plan)
        log.info("executing %s %s %s sl %s tp %s (risk %s USD) for %s", req.side, req.lots, req.symbol,
                 req.sl, req.tp or "-", _money(plan.risk_usd), sig.id)
        try:
            res = self.executor.open_market(req)
        except ExecutorError as e:
            res = OrderResult("error", e.reason)    # raised before the click: nothing was sent
        except Exception as e:
            log.exception("open_market raised for %s", sig.id)
            res = OrderResult("uncertain", "UNCERTAIN_EXECUTION: %s" % _reason_of(e))
        with self._lock:
            self._account_dirty = True
        self._entry_result(sig, plan, req, res)

    def _entry_superseded(self, sig: Signal) -> bool:
        """True (and the signal is marked expired) if a close/close_all arrived after this entry."""
        later = self._later_exit(sig)
        if later is None:
            return False
        reason = ("SUPERSEDED_BY_CLOSE: %s %s arrived after this entry; the entry was not placed"
                  % (later.get("action"), later.get("id")))
        self.store.set_signal_status(sig.id, "expired", reason)
        msg = "%s %s not placed: %s" % (sig.action, sig.symbol, reason)
        log.info(msg)
        self._event("info", "entry_superseded", msg, {"id": sig.id, "by": later.get("id")})
        self._notify("tvbridge: entry superseded by a close", msg, "info")
        return True

    def _opposite_positions(self, sig: Signal) -> List[str]:
        """Descriptions of open positions on the signal's symbol on the opposite side (MT5's
        latest read when known, plus the ledger)."""
        if sig.action not in ("buy", "sell") or not sig.symbol:
            return []
        opp = opposite_side(sig.action)
        key = self._sym_key(sig.symbol)
        out = []  # type: List[str]
        with self._lock:
            snap, dirty = self._last_snapshot, self._account_dirty
        if snap is not None and not dirty and snap.positions:
            for p in snap.positions:
                if self._sym_key(p.symbol) == key and p.side == opp:
                    out.append("%s %s %s" % (p.side, p.lots, p.symbol))
        for row in self.store.open_ledger_positions():
            if self._sym_key(row.get("symbol")) == key and row.get("side") == opp and not out:
                out.append("%s %s %s" % (row.get("side"), row.get("lots"), row.get("symbol")))
        return out

    def _opposite_still_visible(self, sig: Signal) -> bool:
        """True if the latest read shows an opposite position on the symbol (or cannot tell)."""
        with self._lock:
            snap, dirty = self._last_snapshot, self._account_dirty
        if snap is None or dirty or snap.positions is None:
            return True
        opp = opposite_side(sig.action)
        key = self._sym_key(sig.symbol)
        return any(self._sym_key(p.symbol) == key and p.side == opp for p in snap.positions)

    def _later_exit(self, sig: Signal) -> Optional[Dict[str, Any]]:
        """A close (same symbol, side None or the entry's side) or close_all received after this entry."""
        key = self._sym_key(sig.symbol)
        for row in self.store.exit_signals_received_after(sig.id):
            if row.get("action") == "close_all":
                return row
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            side = payload.get("side")
            if self._sym_key(row.get("symbol")) == key and (not side or side == sig.action):
                return row
        return None

    def _order_request(self, sig: Signal, plan: TradePlan) -> OrderRequest:
        spec = self.cfg.spec_for(sig.symbol)
        if spec is None:  # plan_entry refuses NO_SPEC first; this is only a guard
            raise ExecutorError("NO_SPEC", "no symbol spec for %s" % sig.symbol)
        return OrderRequest(
            symbol=sig.symbol, side=sig.action, lots=plan.lots, sl=float(sig.sl),  # type: ignore[arg-type]
            tp=sig.tp if sig.tp else None, digits=int(spec.digits), lot_decimals=spec.lot_decimals,
            comment=sig.comment, price_hint=sig.price, quote_usd=sig.quote_usd,
        )

    def _entry_rejected(self, sig: Signal, plan: TradePlan) -> None:
        code = plan.code
        status = "expired" if code == "STALE_SIGNAL" else "rejected"
        self.store.set_signal_status(sig.id, status, plan.reason, {"plan": _plan_dict(plan)})
        level = "warn" if code == "RISK_ERROR" else "info"
        msg = "%s %s refused: %s" % (sig.action, sig.symbol, plan.reason)
        log.info(msg)
        self._event(level, "entry_rejected", msg, {"id": sig.id, "code": code})
        self._notify("tvbridge: entry refused (%s)" % code, msg, level)

    def _close_for_reversal(self, sig: Signal, opposite: List[str]) -> Tuple[bool, bool]:
        """Close the opposite positions before a reversal entry.

        Returns (ok, rehearsed_only). ok False: the close failed and the entry was refused.
        """
        opp = opposite_side(sig.action)
        desc = ", ".join(opposite)
        log.info("reversal: closing %s before %s %s", desc, sig.action, sig.symbol)
        error = ""
        try:
            results = list(self.executor.close_positions(sig.symbol, opp))
        except Exception as e:
            results = []
            error = _reason_of(e)
        with self._lock:
            self._account_dirty = True
        bad = [r for r in results if r.status not in ("filled", "rehearsed")]
        self._close_ledger_for(results, sig.symbol, opp, "reversal", clock.utcnow(), all_ok=not error and not bad)
        if error or bad:
            detail = error or "; ".join("%s: %s" % (r.status, r.message) for r in bad)
            reason = ("REVERSAL_CLOSE_FAILED: could not close %s before the %s entry (%s); the entry was not placed"
                      % (desc, sig.action, detail))
            self.store.set_signal_status(sig.id, "rejected", reason, {"close_results": _results_dict(results)})
            log.error(reason)
            self._event("critical", "reversal_close_failed", reason, {"id": sig.id})
            self._notify("tvbridge: REVERSAL CLOSE FAILED", reason + ". Check MT5.", "critical")
            return False, False
        if not results:
            log.info("reversal: MT5 shows no %s position on %s any more; continuing with the entry", opp, sig.symbol)
            return True, False
        filled = [r for r in results if r.status == "filled"]
        if filled:
            msg = "reversal: closed %d %s position(s) on %s before the %s entry %s" % (
                len(filled), opp, sig.symbol, sig.action, sig.id)
            log.info(msg)
            self._event("info", "reversal_close", msg, {"id": sig.id})
        return True, all(r.status == "rehearsed" for r in results)

    def _entry_result(self, sig: Signal, plan: TradePlan, req: OrderRequest, res: OrderResult) -> None:
        if res.sl is not None and req.sl_distance is not None:
            # mirror mode: the executor measured the stop from its own quote; book what it used
            sig = dataclasses.replace(sig, sl=res.sl, tp=res.tp)
        now = clock.utcnow()
        result = {"plan": _plan_dict(plan), "order": req.to_dict(), "result": res.to_dict()}
        desc = "%s %s %s" % (req.side, req.lots, req.symbol)
        if res.status == "filled":
            self._preclick_failures = 0
            lots = res.lots if res.lots else plan.lots
            entry = res.fill_price if res.fill_price else sig.price
            others = sum(float(r.get("risk_usd") or 0.0) for r in self.store.open_ledger_positions()
                         if _finite(r.get("risk_usd")))
            risk_usd, actual, note = self._booked_risk(sig, plan, float(lots), entry, sig.sl)
            basis = self._account_basis()      # the account before this position existed
            pid = self.store.add_position(sig.id, sig.symbol, sig.action, lots, entry, sig.sl, sig.tp,
                                          risk_usd, now, ticket=res.ticket)
            if basis is not None:
                self._open_basis[pid] = basis
            result["booked_risk_usd"] = round(risk_usd, 2)
            self.store.set_signal_status(sig.id, "done", "", result)
            msg = "%s filled at %s (ticket %s), SL %s, TP %s, risk %s USD" % (
                desc, entry, res.ticket or "?", sig.sl, sig.tp or "-", _money(risk_usd))
            log.info(msg)
            self._event("info", "entry_filled", msg, {"id": sig.id, "pid": pid})
            self._notify("tvbridge: %s %s filled" % (sig.action, sig.symbol), msg, "info")
            if actual > plan.risk_usd + 0.005 or note:
                level = "warn"
                extra = ""
                floors = self._floors_today()
                with self._lock:
                    snap = self._last_snapshot
                if floors is not None and snap is not None and \
                        float(snap.balance) - others - risk_usd < floors.kill_floor:
                    level = "critical"
                    extra = (" The worst case (all stops hit) now reaches below the kill floor %s: consider "
                             "reducing exposure." % _money(floors.kill_floor))
                warn = ("%s: the fill at %s makes the loss at the stop-loss %s USD, more than the %s USD planned "
                        "(slippage/spread beyond the buffer); %s USD booked.%s%s" % (
                            desc, entry, _money(actual), _money(plan.risk_usd), _money(risk_usd), extra,
                            (" " + note) if note else ""))
                log.warning(warn)
                self._event(level, "fill_slippage", warn, {"id": sig.id, "pid": pid})
                self._notify("tvbridge: fill worse than planned", warn, level)
            return
        if res.status == "rehearsed":
            self._preclick_failures = 0
            self.store.set_signal_status(sig.id, "rehearsed", res.message, result)
            msg = "%s rehearsed: %s" % (desc, res.message)
            self._event("info", "entry_rehearsed", msg, {"id": sig.id})
            self._notify("tvbridge: rehearsed %s %s" % (sig.action, sig.symbol), msg, "info")
            return
        if res.status == "rejected":
            self._preclick_failures = 0
            reason = res.message or "REJECTED"
            self.store.set_signal_status(sig.id, "failed", reason, result)
            msg = "%s was rejected by MT5: %s (not retried)" % (desc, reason)
            log.warning(msg)
            self._event("warn", "entry_failed", msg, {"id": sig.id})
            self._notify("tvbridge: order rejected", msg, "warn")
            return
        if res.status == "error":
            # Before the click (owner decision D2): nothing was sent. Fail this signal, keep
            # entries running; repeated failures mean the GUI needs a human.
            if (res.message or "").startswith("ABORTED"):
                self.store.set_signal_status(sig.id, "rejected", res.message, result)
                msg = "%s not placed: %s" % (desc, res.message)
                self._event("info", "entry_rejected", msg, {"id": sig.id, "code": "ABORTED"})
                self._notify("tvbridge: entry aborted", msg, "info")
                return
            self._preclick_failures += 1
            n = self._preclick_failures
            self.store.set_signal_status(sig.id, "failed", res.message or "error", result)
            level = "critical" if n >= PRECLICK_CRITICAL_AFTER else "warn"
            msg = "%s failed before the click: %s. Nothing was sent; not retried." % (desc, res.message or "error")
            if level == "critical":
                msg += (" %d entries in a row failed before the click: the MT5 GUI needs attention (tvbridge "
                        "doctor, read-account, rehearse)." % n)
            log.warning(msg)
            self._event(level, "entry_failed", msg, {"id": sig.id, "preclick_failures": n})
            self._notify("tvbridge: entry failed (nothing sent)", msg, level)
            return
        # uncertain or anything unexpected: the order may be live. Fail closed.
        self._preclick_failures = 0
        detail = res.message or res.status
        if detail.startswith("UNCERTAIN_EXECUTION: "):
            detail = detail[len("UNCERTAIN_EXECUTION: "):]
        self.store.set_signal_status(sig.id, "failed", res.message or res.status, result)
        self._halt("UNCERTAIN_EXECUTION: signal %s (%s) ended %s: %s" % (sig.id, desc, res.status, detail))
        msg = ("%s ended %s: %s. New entries are halted. Check MT5 now; if a position opened, make sure it has its "
               "stop-loss, then run `tvbridge resume`." % (desc, res.status, detail))
        log.error(msg)
        self._event("critical", "uncertain_execution", msg, {"id": sig.id, "status": res.status})
        self._notify("tvbridge: CHECK MT5 (%s %s)" % (res.status, sig.symbol), msg, "critical")
        self._poll_account("after %s execution" % res.status, adopt=(sig, plan))

    # ------------------------------------------------------------------ SYNC (mirror mode)

    def _mirror_scale(self, symbol: str) -> float:
        """Opened lots / requested lots of the mirrored position on ``symbol`` (1.0 when unset)."""
        raw = self.store.get_kv(MIRROR_SCALE_PREFIX + self._sym_key(symbol))
        try:
            v = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1.0
        return v if math.isfinite(v) and 0.0 < v <= 1.0 else 1.0

    def _reset_mirror_scale(self, symbol: str) -> None:
        self.store.set_kv(MIRROR_SCALE_PREFIX + self._sym_key(symbol), None)

    @staticmethod
    def _sync_desc(sig: Signal) -> str:
        if sig.target_side is None:
            return "sync %s -> flat" % sig.symbol
        return "sync %s -> %s %s units" % (sig.symbol, "long" if sig.target_side == "buy" else "short",
                                           sig.target_units)

    def _sync_superseded(self, sig: Signal) -> bool:
        """True (and the signal is marked expired) if a later sync for the symbol is stored:
        only the newest sync per symbol describes the strategy's position."""
        parent = str((sig.raw or {}).get(FAN_OUT_PARENT) or "")
        if parent:
            # a fan-out child: a newer alert on the PARENT's symbol supersedes it too, even before that
            # alert has fanned out (its own siblings do not count)
            newer = self.store.newer_sync_signal(sig.id) or self.store.newer_sync_signal(parent, parent + "@")
        else:
            # a fan-out parent's own children are stored after it on (maybe) the same symbol
            newer = self.store.newer_sync_signal(sig.id, sig.id + "@")
        if newer is None:
            return False
        if self._follows(sig, newer):
            return False
        reason = "SUPERSEDED_BY_SYNC: sync %s (fired %s) is newer; it decides the position on %s" % (
            newer.get("id"), newer.get("fired_at"), sig.symbol)
        self.store.set_signal_status(sig.id, "expired", reason)
        msg = "%s not acted on: %s" % (self._sync_desc(sig), reason)
        log.info(msg)
        self._event("info", "sync_superseded", msg, {"id": sig.id, "by": newer.get("id")})
        return True

    def _follows(self, sig: Signal, other: Dict[str, Any]) -> bool:
        """True if ``sig`` is the fill right after stored sync ``other`` although both carry the same
        second: ``sig`` starts from the position ``other`` ended with (prev_position/prev_size == its
        position/size). Two fills in one second (a partial exit, then the full exit) arrive in any
        order; the chain, not the arrival order, says which one is final."""
        row = self.store.get_signal_row(sig.id)
        if row is None or str(row.get("fired_at")) != str(other.get("fired_at")):
            return False
        payload = other.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        o_raw = (payload or {}).get("raw") if isinstance(payload, dict) else None
        s_raw = sig.raw or {}
        if not isinstance(o_raw, dict) or not s_raw:
            return False
        try:
            same_pos = str(s_raw.get("prev_position") or "").lower() == str(o_raw.get("position") or "").lower()
            same_size = abs(float(s_raw.get("prev_size")) - float(o_raw.get("size"))) < 1e-9
        except (TypeError, ValueError):
            return False
        if same_pos and same_size:
            log.info("sync %s follows %s (same second, chained sizes): it is the newer one", sig.id, other.get("id"))
            return True
        return False

    def _sync_fail(self, sig: Signal, status: str, reason: str, level: str, title: str, kind: str,
                   result: Optional[Dict[str, Any]] = None) -> None:
        self.store.set_signal_status(sig.id, status, reason, result)
        msg = "%s: %s" % (self._sync_desc(sig), reason)
        log.log(logging.ERROR if level == "critical" else logging.WARNING if level == "warn" else logging.INFO, msg)
        self._event(level, kind, msg, {"id": sig.id})
        self._notify(title, msg + (" Check MT5." if level == "critical" else ""), level)

    @staticmethod
    def _sync_is_reduction(sig: Signal) -> bool:
        """True if the alert says the fill reduced an existing strategy position (same side
        before, larger size before): a partial exit, never a reason to open a position."""
        raw = sig.raw or {}
        prev = str(raw.get("prev_position") if raw.get("prev_position") is not None
                   else raw.get("prev_market_position") or "").strip().lower()
        prev_side = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell"}.get(prev)
        if prev_side is None or prev_side != sig.target_side:
            return False
        value = raw.get("prev_size") if raw.get("prev_size") is not None else raw.get("prev_market_position_size")
        try:
            prev_size = abs(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return math.isfinite(prev_size) and prev_size > float(sig.target_units or 0.0) + LOTS_EPS

    def _fan_out_children(self, sig: Signal) -> List[Signal]:
        """The child signals of a sync whose TV symbol has a ``mirror.fan_out`` entry ([] if it
        has none or is itself a child): one per target, on the target's MT5 symbol."""
        if sig.action != "sync" or (sig.raw or {}).get(FAN_OUT_PARENT):
            return []
        out = []  # type: List[Signal]
        for target in self.cfg.mirror_fan_out(sig.tv_symbol or sig.symbol):
            key = self.cfg.tv_symbol_for(target) or self.cfg.normalize_tv_symbol(target)
            mt5 = self.cfg.mt5_symbol(key)
            raw = dict(sig.raw or {})
            raw[FAN_OUT_PARENT] = sig.id
            child = dataclasses.replace(sig, id="%s@%s" % (sig.id, mt5), symbol=mt5, tv_symbol=key, raw=raw)
            rate = self.cfg.mirror_quote_usd(key)
            if rate:
                # target quoted in another currency: the parent's (USD) price becomes that currency
                price = child.price
                if _finite(price) and float(price) > 0:     # type: ignore[arg-type]
                    price = float(price) / rate             # type: ignore[arg-type]
                child = dataclasses.replace(child, price=price, quote_usd=rate)
            out.append(child)
        return out

    def _fanned_out_complete(self, row: Dict[str, Any]) -> List[str]:
        """For a stored fan-out parent whose children all exist: their MT5 symbols; else []."""
        if row.get("action") != "sync":
            return []
        payload = row.get("payload")
        try:
            sig = Signal.from_dict(payload) if isinstance(payload, dict) else None
        except (KeyError, TypeError, ValueError):
            sig = None
        if sig is None or not self.cfg.mirror.enabled:
            return []
        children = self._fan_out_children(sig)
        if not children or any(self.store.get_signal_row(c.id) is None for c in children):
            return []
        return [c.symbol for c in children]

    def _sync_fan_out(self, sig: Signal, children: List[Signal]) -> None:
        """Store and queue one child sync per fan-out target; the parent is done. Idempotent:
        children that already exist are kept (and only run if they are still queued)."""
        for child in children:
            self.store.insert_signal(child)          # False = stored by an earlier (interrupted) run
        targets = ", ".join(c.symbol for c in children)
        self.store.set_signal_status(sig.id, "done", "FANNED_OUT: %s" % targets,
                                     {"fan_out": [c.id for c in children]})
        msg = "%s fanned out to %s" % (self._sync_desc(sig), targets)
        log.info(msg)
        self._event("info", "sync_fanned_out", msg, {"id": sig.id, "children": [c.id for c in children]})
        for child in children:
            row = self.store.get_signal_row(child.id)
            if row is not None and row.get("status") == "queued":
                self.submit_signal(child)

    def _do_sync(self, task: Task) -> None:
        """Mirror mode: make the MT5 position on the signal's symbol match the strategy's.

        Closing or reducing always runs (also paused/halted, also for a late alert); opening
        goes through the risk guard like any entry. MT5's own position list is the truth.
        """
        sig = task.signal
        assert sig is not None
        m = self.cfg.mirror
        if not m.enabled:
            self._sync_fail(sig, "rejected", "MIRROR_DISABLED: a sync alert arrived but mirror.enabled is false; "
                            "nothing was done", "warn", "tvbridge: sync alert refused (mirror mode is off)",
                            "sync_rejected")
            return
        children = self._fan_out_children(sig)
        if children:                                                     # fan-out parent: no order of its own
            if self._sync_superseded(sig):
                return
            self._sync_fan_out(sig, children)
            return
        if self._sync_superseded(sig):                                   # (a)
            return
        self.store.set_signal_status(sig.id, "processing")
        self._apply_price_hint(sig)
        snap = self._poll_account("sync %s" % sig.id)                    # (b)
        side = sig.target_side
        positions = snap.positions if snap is not None else None
        if positions is None:
            note = (snap.positions_note if snap is not None else self._last_read_error) or "account not readable"
            if side is None:
                self._sync_close_symbol(sig, "the MT5 position list is unknown (%s)" % note)   # exits fail open
                return
            self._sync_fail(sig, "failed", "POSITIONS_UNKNOWN: the MT5 position list could not be read (%s); "
                            "the position on %s was not changed" % (note, sig.symbol), "critical",
                            "tvbridge: SYNC FAILED (positions unknown)", "sync_failed")
            return

        key = self._sym_key(sig.symbol)                                  # (c)
        current = [p for p in positions if self._sym_key(p.symbol) == key]
        if not current:
            self._reset_mirror_scale(sig.symbol)                         # flat: a new position, a new scale
        if side is None:                                                 # (d)
            if not current:
                self.store.set_signal_status(sig.id, "done", "IN_SYNC: %s is flat" % sig.symbol)
                return
            self._sync_close_symbol(sig, "the strategy is flat")
            return
        spec = self.cfg.spec_for(sig.symbol)
        if spec is None:
            self._sync_fail(sig, "rejected", "NO_SPEC: no symbols.specs entry for %r" % (sig.symbol,), "warn",
                            "tvbridge: sync refused (NO_SPEC)", "sync_rejected")
            return
        full_units_lots = float(sig.target_units or 0.0) / self.cfg.mirror_units_per_lot(sig.symbol)
        full_lots = risk.round_lots_down(full_units_lots, spec.lot_step, spec.min_lot)
        target_lots = risk.round_lots_down(full_units_lots * self._mirror_scale(sig.symbol), spec.lot_step,
                                           spec.min_lot)

        rehearsed_flat = False
        if any(p.side != side for p in current):                         # (e) reversal: close first
            outcome = self._sync_close_symbol(sig, "the strategy reversed to %s" % side, final=False)
            if outcome == "failed":
                return
            rehearsed_flat = outcome == "rehearsed"
            snap = self._poll_account("after the sync reversal close for %s" % sig.id)
            positions = snap.positions if snap is not None else None
            if not rehearsed_flat:
                left = None if positions is None else [p for p in positions if self._sym_key(p.symbol) == key]
                if left is None or left:
                    detail = ("the MT5 position list could not be read after the close" if left is None else
                              "%s still open" % ", ".join("%s %s %s" % (p.side, p.lots, p.symbol) for p in left))
                    self._sync_fail(sig, "failed", "CLOSE_FAILED: %s; no new position was opened" % detail,
                                    "critical", "tvbridge: CLOSE FAILED", "close_failed")
                    return
            current = []
            self._reset_mirror_scale(sig.symbol)
            target_lots = full_lots

        tol = float(m.size_tolerance_lots)
        cur_lots = sum(float(p.lots) for p in current)
        if not current:                                                  # (i)
            self._sync_entry(sig, side, full_lots, 0.0, full_lots, rehearsed_flat)
            return
        if target_lots <= 0:
            # a non-zero target below the minimum lot while reducing: nothing sensible is left
            self._sync_close_symbol(sig, "the target %s units is below the minimum lot" % sig.target_units)
            return
        if cur_lots > target_lots + tol:                                 # (f)
            self._sync_reduce(sig, side, cur_lots, target_lots, spec)
            return
        if cur_lots >= target_lots - tol:                                # (g)
            self.store.set_signal_status(sig.id, "done", "IN_SYNC: %s %s %s matches the target %s" % (
                side, cur_lots, sig.symbol, target_lots))
            return
        if not m.allow_adds:                                             # (h)
            self._sync_fail(sig, "rejected", "MIRROR_ADD_REFUSED: MT5 has %s %s %s, the strategy wants %s lots; "
                            "adding to an open position is off (mirror.allow_adds)" % (
                                side, cur_lots, sig.symbol, target_lots), "warn",
                            "tvbridge: sync add refused", "sync_rejected")
            return
        self._sync_entry(sig, side, target_lots - cur_lots, cur_lots, full_lots, False)

    def _sync_close_symbol(self, sig: Signal, why: str, final: bool = True) -> str:
        """Close every position on the signal's symbol. Returns "closed", "none" (nothing was
        open), "rehearsed" or "failed" (status set, critical notification). With ``final`` the
        signal's status is also set for the other outcomes."""
        error = ""
        try:
            results = list(self.executor.close_positions(sig.symbol))
        except Exception as e:
            results = []
            error = _reason_of(e)
        with self._lock:
            self._account_dirty = True
        now = clock.utcnow()
        payload = {"results": _results_dict(results)}
        bad = [r for r in results if r.status not in ("filled", "rehearsed")]
        rehearsed = bool(results) and all(r.status == "rehearsed" for r in results)
        closed = 0
        if not error and not rehearsed:
            closed = self._close_ledger_for(results, sig.symbol, None, "sync_close", now, all_ok=not bad)
        if error or bad:
            detail = error or "%d of %d close(s) failed: %s" % (
                len(bad), len(results), "; ".join("%s: %s" % (r.status, r.message) for r in bad))
            self._sync_fail(sig, "failed", "CLOSE_FAILED: %s. The position on %s may still be open" % (
                detail, sig.symbol), "critical", "tvbridge: CLOSE FAILED", "close_failed", payload)
            return "failed"
        if not results:
            if final:
                self.store.set_signal_status(sig.id, "done", "NO_POSITION: nothing open on %s" % sig.symbol, payload)
            return "none"
        if rehearsed:
            if final:
                self.store.set_signal_status(sig.id, "rehearsed", "; ".join(r.message for r in results), payload)
                self._event("info", "close_rehearsed", "%s: close rehearsed (%d position(s))" % (
                    self._sync_desc(sig), len(results)), {"id": sig.id})
            return "rehearsed"
        self._reset_mirror_scale(sig.symbol)
        filled = [r for r in results if r.status == "filled"]
        msg = "%s: closed %d position(s) on %s (%d ledger row(s)) because %s: %s" % (
            self._sync_desc(sig), len(filled), sig.symbol, closed, why, "; ".join(r.message for r in filled))
        log.info(msg)
        self._event("info", "close_done", msg, {"id": sig.id})
        if final:
            self.store.set_signal_status(sig.id, "done", "", payload)
            self._notify("tvbridge: closed %s" % sig.symbol, msg, "info")
        return "closed"

    def _sync_reduce(self, sig: Signal, side: str, cur_lots: float, target_lots: float, spec: Any) -> None:
        """Partial close: bring ``cur_lots`` on the symbol/side down to ``target_lots``."""
        lots = round(cur_lots - target_lots, spec.lot_decimals)
        error = ""
        try:
            results = list(self.executor.close_partial(sig.symbol, side, lots))
        except Exception as e:
            results = []
            error = _reason_of(e)
        with self._lock:
            self._account_dirty = True
        now = clock.utcnow()
        payload = {"partial_lots": lots, "results": _results_dict(results)}
        filled = [r for r in results if r.status == "filled"]
        bad = [r for r in results if r.status not in ("filled", "rehearsed")]
        self._reduce_ledger(sig.symbol, side, filled, now)
        if error or bad:
            detail = error or "; ".join("%s: %s" % (r.status, r.message) for r in bad)
            self._sync_fail(sig, "failed", "CLOSE_FAILED: could not reduce %s %s by %s lots (%s). The position "
                            "is larger than the strategy's" % (side, sig.symbol, lots, detail), "critical",
                            "tvbridge: CLOSE FAILED", "close_failed", payload)
            return
        if not results:
            self.store.set_signal_status(sig.id, "done", "NO_POSITION: nothing open on %s %s" % (sig.symbol, side),
                                         payload)
            return
        if all(r.status == "rehearsed" for r in results):
            self.store.set_signal_status(sig.id, "rehearsed", "; ".join(r.message for r in results), payload)
            self._event("info", "close_rehearsed", "%s: partial close of %s lots rehearsed" % (
                self._sync_desc(sig), lots), {"id": sig.id})
            return
        done = sum(float(r.lots or 0.0) for r in filled)
        self.store.set_signal_status(sig.id, "done", "", payload)
        msg = "%s: reduced %s %s from %s to %s lots (closed %s): %s" % (
            self._sync_desc(sig), side, sig.symbol, cur_lots, round(cur_lots - done, 8), round(done, 8),
            "; ".join(r.message for r in filled))
        log.info(msg)
        self._event("info", "partial_close", msg, {"id": sig.id})
        self._notify("tvbridge: reduced %s" % sig.symbol, msg, "info")

    def _reduce_ledger(self, symbol: str, side: str, filled: Sequence[OrderResult], now: datetime) -> None:
        """Book partial/whole closes of ``symbol``/``side`` in the ledger: the row with the
        result's ticket first, then the other rows on that symbol and side."""
        key = self._sym_key(symbol)
        for r in filled:
            left = float(r.lots or 0.0)
            rows = [row for row in self.store.open_ledger_positions()
                    if self._sym_key(row.get("symbol")) == key and row.get("side") == side]
            rows.sort(key=lambda row: (0 if r.ticket and str(row.get("ticket") or "") == str(r.ticket) else 1,
                                       -float(row.get("lots") or 0.0)))
            for row in rows:
                if left <= LOTS_EPS:
                    break
                row_lots = float(row.get("lots") or 0.0)
                take = min(row_lots, left)
                if take >= row_lots - LOTS_EPS:
                    self.store.close_ledger_position(int(row["pid"]), "sync_partial", now)
                else:
                    self.store.reduce_ledger_position(int(row["pid"]), take)
                left -= take

    def _sync_entry(self, sig: Signal, side: str, want_lots: float, cur_lots: float, full_lots: float,
                    reversal_rehearsed: bool) -> None:
        """Open ``want_lots`` (capped by the risk guard) so MT5 follows the strategy into a position.

        The risk guard sees a synthetic entry: the alert price, a stop ``mirror.stop_distance``
        away and ``risk.max_risk_per_trade_pct``. The order itself carries the distances; the
        executor measures the stop from its own quote.
        """
        m = self.cfg.mirror
        r = self.cfg.risk
        price = float(sig.price) if _finite(sig.price) and float(sig.price) > 0 else None  # type: ignore[arg-type]
        sign = 1.0 if side == "buy" else -1.0
        # Stop distance: fixed, or (idea_risk_pct) the distance at which the strategy's own size loses
        # that share of the account -- a bigger size gets a proportionally tighter stop.
        stop_dist = self.cfg.mirror_stop_distance(sig.symbol)
        tp_dist = self.cfg.mirror_tp_distance(sig.symbol)
        spec0 = self.cfg.spec_for(sig.symbol)
        if not (_finite(sig.quote_usd) and float(sig.quote_usd) > 0):   # type: ignore[arg-type]
            rate = self.cfg.mirror_quote_usd(sig.symbol)
            if rate:
                sig = dataclasses.replace(sig, quote_usd=rate)
        q2usd = None  # type: Optional[float]
        if spec0 is not None:
            if str(spec0.quote).upper() == "USD":
                q2usd = 1.0
            elif _finite(sig.quote_usd) and float(sig.quote_usd) > 0:   # type: ignore[arg-type]
                q2usd = float(sig.quote_usd)                            # type: ignore[arg-type]
        if float(m.idea_risk_pct) > 0 and want_lots > 0 and spec0 is not None and q2usd:
            by_risk = (float(self.cfg.account.initial_balance) * float(m.idea_risk_pct) / 100.0
                       / (float(want_lots) * float(spec0.contract_size) * q2usd))
            floor = float(spec0.min_sl_points) * float(spec0.point)
            stop_dist = max(min(stop_dist, by_risk), floor)
            stop_dist = round(stop_dist, int(spec0.digits))
        entry = dataclasses.replace(
            sig, action=side, side=None, risk_pct=float(r.max_risk_per_trade_pct),
            sl=None if price is None else price - sign * stop_dist,
            tp=None if price is None or not tp_dist else price + sign * tp_dist)
        if cur_lots <= 0 and self._sync_is_reduction(sig):
            self._entry_rejected(entry, TradePlan(False, "MIRROR_NOT_AN_ENTRY: the strategy reduced its %s position "
                                                  "(%s -> %s units) but MT5 has none on %s; a partial exit never "
                                                  "opens a position" % (side, (sig.raw or {}).get("prev_size"),
                                                                        sig.target_units, sig.symbol)))
            return
        if price is None:
            self._entry_rejected(entry, TradePlan(False, "NO_PRICE: the sync alert has no valid price (%r); "
                                                  "nothing was opened" % (sig.price,)))
            return
        stale = self._sync_entry_block(sig)
        if stale is not None:
            self._entry_rejected(entry, stale)
            return
        if self._entry_superseded(entry) or self._sync_superseded(sig):
            return

        now = clock.utcnow()
        plan = risk.plan_entry(entry, self._risk_state(now), self.cfg)
        if not plan.approved:
            self._entry_rejected(entry, plan)
            return
        if plan.close_first and not reversal_rehearsed and self._opposite_still_visible(entry):
            desc = ", ".join("%s %s %s" % (p.side, p.lots, p.symbol) for p in plan.close_first)
            self._sync_fail(sig, "rejected", "REVERSAL_CLOSE_FAILED: %s still open; the %s entry was not placed"
                            % (desc, side), "critical", "tvbridge: REVERSAL CLOSE FAILED", "reversal_close_failed",
                            {"plan": _plan_dict(plan)})
            return
        spec = self.cfg.spec_for(sig.symbol)
        if spec is None:   # plan_entry refuses NO_SPEC first; only a guard
            raise ExecutorError("NO_SPEC", "no symbol spec for %s" % sig.symbol)
        lots = risk.round_lots_down(min(float(want_lots), float(plan.lots), float(r.max_lots)), spec.lot_step,
                                    spec.min_lot)
        if lots <= 0 or plan.lots <= 0:
            self._entry_rejected(entry, TradePlan(False, "SIZE_TOO_SMALL: the strategy's size (%s units = %s lots) "
                                                  "is below the minimum lot" % (sig.target_units, want_lots),
                                                  details=plan.details))
            return
        details = dict(plan.details or {})
        details["mirror"] = {"units": sig.target_units, "wanted_lots": want_lots, "risk_guard_lots": plan.lots,
                             "current_lots": cur_lots}
        plan = dataclasses.replace(plan, lots=lots, risk_usd=float(plan.risk_usd) * lots / float(plan.lots),
                                   details=details)

        # Last checks right before the order (the closes and reads above take a while).
        stale = self._sync_entry_block(sig)
        if stale is not None:
            self._entry_rejected(entry, stale)
            return
        if self._sync_superseded(sig):
            return

        req = OrderRequest(
            symbol=sig.symbol, side=side, lots=lots, sl=float(entry.sl),  # type: ignore[arg-type]
            tp=entry.tp if entry.tp else None, digits=int(spec.digits), lot_decimals=spec.lot_decimals,
            comment=sig.comment, price_hint=price, quote_usd=sig.quote_usd,
            sl_distance=stop_dist, tp_distance=tp_dist if tp_dist else None,
        )
        log.info("mirror: executing %s %s %s (stop %s from the quote, risk %s USD) for %s", req.side, req.lots,
                 req.symbol, req.sl_distance, _money(plan.risk_usd), sig.id)
        try:
            res = self.executor.open_market(req)
        except ExecutorError as e:
            res = OrderResult("error", e.reason)    # raised before the click: nothing was sent
        except Exception as e:
            log.exception("open_market raised for %s", sig.id)
            res = OrderResult("uncertain", "UNCERTAIN_EXECUTION: %s" % _reason_of(e))
        with self._lock:
            self._account_dirty = True
        if res.status not in ("rejected", "error", "rehearsed") and full_lots > 0:
            # a position exists (or may): later targets are scaled by what was really opened
            done = float(res.lots) if res.lots else lots
            scale = min(1.0, (cur_lots + done) / float(full_lots))
            self.store.set_kv(MIRROR_SCALE_PREFIX + self._sym_key(sig.symbol), repr(scale))
        self._entry_result(entry, plan, req, res)

    def _sync_entry_block(self, sig: Signal) -> Optional[TradePlan]:
        """Why a sync may not open a position right now (age, halt, pause, flatten), or None."""
        age = (clock.utcnow() - clock.ensure_utc(sig.fired_at)).total_seconds()
        if age > float(self.cfg.risk.entry_max_delay_s):
            return TradePlan(False, "STALE_SIGNAL: signal fired %.1f s ago (entry_max_delay_s=%s); nothing was "
                                    "opened" % (age, self.cfg.risk.entry_max_delay_s))
        if -age > float(self.cfg.server.max_future_skew_s):
            return TradePlan(False, "FUTURE: signal is dated %.1f s in the future (max_future_skew_s=%s); nothing "
                                    "was opened" % (-age, self.cfg.server.max_future_skew_s))
        block = self._entry_block_reason()
        if block:
            return TradePlan(False, block + "; the entry was not placed")
        return None

    # ------------------------------------------------------------------ CLOSE / CLOSE_ALL / FLATTEN

    def _close_ledger_for(self, results: Sequence[OrderResult], symbol: Optional[str], side: Optional[str],
                          reason: str, now: datetime, all_ok: bool) -> int:
        """Close ledger rows after closes: all matching rows if every close filled, else by ticket."""
        filled = [r for r in results if r.status == "filled"]
        if not filled:
            return 0
        if all_ok and len(filled) == len(results):
            return self.store.close_ledger_positions(symbol, side, reason, now)
        tickets = {str(r.ticket) for r in filled if r.ticket}
        n = 0
        for row in self.store.open_ledger_positions():
            if row.get("ticket") and str(row["ticket"]) in tickets:
                self.store.close_ledger_position(int(row["pid"]), reason, now)
                n += 1
        return n

    def _hold_rows(self, sig: Signal) -> List[Dict[str, Any]]:
        key = self._sym_key(sig.symbol)
        return [r for r in self.store.open_ledger_positions()
                if self._sym_key(r.get("symbol")) == key and not (sig.side and r.get("side") != sig.side)]

    def _do_close(self, task: Task) -> bool:
        """Close a symbol (and side). Returns False if the task was deferred (min hold)."""
        sig = task.signal
        assert sig is not None
        what = "%s%s" % (sig.symbol, " " + sig.side if sig.side else "")
        hold = float(self.cfg.risk.min_hold_s_for_signal_close or 0)
        if hold > 0:
            # Only positions that existed when the close alert fired count: a deferred close
            # must never close a position the strategy opened afterwards.
            rows = self._hold_rows(sig)
            fired = clock.ensure_utc(sig.fired_at)
            older = []  # type: List[datetime]
            for r in rows:
                try:
                    opened = clock.from_iso(r["opened_at"])
                except (KeyError, ValueError):
                    continue
                if opened <= fired:
                    older.append(opened)
            if rows and not older:
                reason = "NO_POSITION: positions on %s were opened after this close alert" % what
                self.store.set_signal_status(sig.id, "done", reason)
                self._event("info", "no_position", "close %s: %s" % (what, reason), {"id": sig.id})
                self._notify("tvbridge: nothing to close", "close %s: %s" % (what, reason), "info")
                return True
            due = max(older) + timedelta(seconds=hold) if older else None
            if due is not None and due > clock.utcnow():
                self.store.set_signal_status(sig.id, "queued", "DEFERRED: min_hold_s_for_signal_close until %s"
                                             % clock.iso(due))
                log.info("close %s deferred until %s (min hold)", sig.id, clock.iso(due))
                self._put(Task(CLOSE, sig, not_before=due, reason="min_hold"))
                return False
        self.store.set_signal_status(sig.id, "processing")
        self._apply_price_hint(sig)
        error = ""
        try:
            results = list(self.executor.close_positions(sig.symbol, sig.side))
        except Exception as e:
            results = []
            error = _reason_of(e)
        with self._lock:
            self._account_dirty = True
        now = clock.utcnow()
        payload = {"results": _results_dict(results)}
        if error:
            reason = "CLOSE_FAILED: %s" % error
            self.store.set_signal_status(sig.id, "failed", reason, payload)
            msg = "close %s failed: %s. The position may still be open: check MT5." % (what, error)
            log.error(msg)
            self._event("critical", "close_failed", msg, {"id": sig.id})
            self._notify("tvbridge: CLOSE FAILED", msg, "critical")
            return True
        if not results:
            self.store.set_signal_status(sig.id, "done", "NO_POSITION: nothing open on %s" % what, payload)
            msg = "close %s: no open position" % what
            log.info(msg)
            self._event("info", "no_position", msg, {"id": sig.id})
            self._notify("tvbridge: nothing to close", msg, "info")
            return True
        if all(r.status == "rehearsed" for r in results):
            self.store.set_signal_status(sig.id, "rehearsed", "; ".join(r.message for r in results), payload)
            self._event("info", "close_rehearsed", "close %s rehearsed (%d position(s))" % (what, len(results)),
                        {"id": sig.id})
            self._notify("tvbridge: close rehearsed", "close %s rehearsed" % what, "info")
            return True
        bad = [r for r in results if r.status not in ("filled", "rehearsed")]
        closed = self._close_ledger_for(results, sig.symbol, sig.side, "signal_close", now, all_ok=not bad)
        filled = [r for r in results if r.status == "filled"]
        if bad:
            reason = "CLOSE_FAILED: %d of %d close(s) failed: %s" % (
                len(bad), len(results), "; ".join("%s: %s" % (r.status, r.message) for r in bad))
            self.store.set_signal_status(sig.id, "failed", reason, payload)
            msg = "close %s: %s. Check MT5." % (what, reason)
            log.error(msg)
            self._event("critical", "close_failed", msg, {"id": sig.id})
            self._notify("tvbridge: CLOSE FAILED", msg, "critical")
            return True
        self.store.set_signal_status(sig.id, "done", "", payload)
        if not sig.side:
            self._reset_mirror_scale(sig.symbol)
        msg = "closed %d position(s) on %s (%d ledger row(s)): %s" % (
            len(filled), what, closed, "; ".join(r.message for r in filled))
        log.info(msg)
        self._event("info", "close_done", msg, {"id": sig.id})
        self._notify("tvbridge: closed %s" % what, msg, "info")
        return True

    def _do_close_all(self, task: Task) -> None:
        """FLATTEN (kill switch / CLI) or a close_all signal: close every position."""
        sig = task.signal
        tag = "flatten" if sig is None else "close_all"
        label = "flatten (%s)" % task.reason if sig is None else "close_all signal %s" % sig.id
        if sig is not None:
            self.store.set_signal_status(sig.id, "processing")
        log.warning("%s: closing every position", label)
        error = ""
        try:
            results = list(self.executor.close_all())
        except Exception as e:
            results = []
            error = _reason_of(e)
        with self._lock:
            self._account_dirty = True
            last = self._last_snapshot
        if not error and not results:
            # "Nothing to close" is only believed when nothing says otherwise.
            evidence = ""
            try:
                n_open = len(self.store.open_ledger_positions())
            except Exception:
                n_open = 0
            if n_open:
                evidence = "the ledger has %d open position(s)" % n_open
            elif last is not None and _finite(last.margin) and float(last.margin) > BALANCE_EPS:  # type: ignore[arg-type]
                evidence = "MT5 reports margin %s in use" % _money(last.margin)
            if evidence:
                error = "NO_ROWS_VISIBLE: no position rows were readable but %s" % evidence
        now = clock.utcnow()
        bad = [r for r in results if r.status not in ("filled", "rehearsed")]
        rehearsed = bool(results) and all(r.status == "rehearsed" for r in results)
        if not error and not bad and not rehearsed:
            closed = self.store.close_ledger_positions(None, None, tag, now)
        else:
            closed = self._close_ledger_for(results, None, None, tag, now, all_ok=False)
        filled = [r for r in results if r.status == "filled"]
        payload = {"results": _results_dict(results)}
        failed = bool(error or bad)
        if not rehearsed:
            self._note_flatten_outcome(failed)
        if failed:
            detail = error or "; ".join("%s: %s" % (r.status, r.message) for r in bad)
            reason = "CLOSE_FAILED: %s" % detail
            if sig is not None:
                self.store.set_signal_status(sig.id, "failed", reason, payload)
            msg = "%s: closed %d, failed: %s. Positions may still be open: check MT5 and close by hand." % (
                label, len(filled), detail)
            log.error(msg)
            self._event("critical", "close_failed", msg)
            if sig is not None or self._flatten_fail_notify_due(error, bad):
                self._notify("tvbridge: FLATTEN FAILED", msg, "critical")
        elif rehearsed:
            if sig is not None:
                self.store.set_signal_status(sig.id, "rehearsed", "; ".join(r.message for r in results), payload)
            self._event("info", "close_rehearsed", "%s rehearsed (%d position(s))" % (label, len(results)))
            self._notify("tvbridge: close_all rehearsed", "%s rehearsed" % label, "info")
        else:
            self._flatten_fail_notified = None
            try:
                self.store.delete_kv_prefix(MIRROR_SCALE_PREFIX)    # everything is flat
            except Exception as e:
                log.error("cannot reset the mirror scales: %s", e)
            if sig is not None:
                self.store.set_signal_status(sig.id, "done", "" if results else "NO_POSITION: nothing open", payload)
            msg = "%s: closed %d position(s) (%d ledger row(s))%s" % (
                label, len(filled), closed, (": " + "; ".join(r.message for r in filled)) if filled else "")
            level = "warn" if sig is None else "info"
            log.log(logging.WARNING if sig is None else logging.INFO, msg)
            self._event(level, tag, msg)
            self._notify("tvbridge: %s done" % tag, msg, level)
        self._poll_account("after %s" % tag)

    def _flatten_fail_notify_due(self, error: str, bad: Sequence[OrderResult]) -> bool:
        """The same FLATTEN FAILED (same failure codes) is notified at most every 5 minutes."""
        codes = sorted({(error.split(":", 1)[0] if error else "")} |
                       {"%s:%s" % (r.status, (r.message or "").split(":", 1)[0]) for r in bad})
        key = "|".join(codes)
        mono = time.monotonic()
        prev = self._flatten_fail_notified
        if prev is not None and prev[0] == key and mono - prev[1] < FLATTEN_FAIL_NOTIFY_S:
            return False
        self._flatten_fail_notified = (key, mono)
        return True

    # ------------------------------------------------------------------ scheduler

    def _scheduler_loop(self) -> None:
        while not self._stop_ev.wait(self.tick_s):
            try:
                self._tick()
            except Exception:
                log.exception("scheduler tick failed")

    def _tick(self) -> None:
        mono = time.monotonic()
        self._check_executor(mono)
        if mono - self._last_poll_mono >= float(self.cfg.executor.gui.account_poll_s):
            self._last_poll_mono = mono
            self.request_account_poll()
        self._consume_command()
        self._maybe_rollover()
        if mono - self._last_heartbeat_mono >= HEARTBEAT_S:
            self._write_heartbeat()
        if mono - self._last_cleanup_mono >= CLEANUP_INTERVAL_S:
            self._last_cleanup_mono = mono
            self._cleanup()
        if mono - self._last_disk_check_mono >= DISK_CHECK_S:
            self._last_disk_check_mono = mono
            self._check_disk(mono)

    def _check_executor(self, mono: float) -> None:
        """Watchdog: a dead executor thread or a task running far longer than any legitimate GUI
        sequence means the kill switch and flatten no longer run -> alert and restart."""
        if self._stall_reported or not self._running or self._stop_ev.is_set():
            return
        t = self._executor_thread
        why = ""
        if t is not None and not t.is_alive():
            why = "the executor thread died"
        else:
            with self._lock:
                cur, started = self._current, self._current_started_mono
            if cur is not None and started is not None:
                limit = float(self.stall_limits_s.get(cur.kind, self.stall_default_s))
                if mono - started > limit:
                    why = "task %s has been running for %.0f s (limit %.0f s)" % (cur.describe(), mono - started,
                                                                                 limit)
        if why:
            self._executor_stalled(why)

    def _executor_stalled(self, why: str) -> None:
        self._stall_reported = why
        msg = ("EXECUTOR STALLED: %s. Account polls, the kill switch and flatten are not running. The engine "
               "exits so launchd restarts it; check MT5 and the log." % why)
        log.critical(msg)
        self._event("critical", "executor_stalled", msg)
        self._notify("tvbridge: EXECUTOR STALLED", msg, "critical")
        self._write_heartbeat()
        flush = getattr(self.notifier, "flush", None)
        if callable(flush):
            try:
                flush(timeout=3.0)
            except Exception:  # pragma: no cover
                pass
        self.on_stall(why)

    @staticmethod
    def _exit_for_restart(why: str) -> None:  # pragma: no cover - ends the process
        logging.shutdown()
        os._exit(1)

    def _check_disk(self, mono: float) -> None:
        try:
            free = shutil.disk_usage(str(self.cfg.home)).free
        except OSError as e:
            log.warning("cannot check free disk space: %s", e)
            return
        if free >= self.disk_min_free_bytes:
            return
        last = self._disk_notify_mono
        if last is not None and mono - last < DISK_NOTIFY_S:
            return
        self._disk_notify_mono = mono
        msg = ("only %.1f GB free on the disk holding %s: screenshots and the database need space, and a full "
               "disk stops account reads, closes and the kill switch. Free some space." % (
                   free / 1024.0 ** 3, self.cfg.home))
        log.critical(msg)
        self._event("critical", "disk_low", msg)
        self._notify("tvbridge: DISK ALMOST FULL", msg, "critical")

    def _consume_command(self) -> None:
        """Run a command the CLI left in kv ``command`` (currently only ``{"cmd": "flatten"}``)."""
        raw = self.store.get_kv("command")
        if raw is None:
            return
        self.store.set_kv("command", None)
        try:
            cmd = json.loads(raw)
        except ValueError:
            cmd = {"cmd": raw}
        if not isinstance(cmd, dict):
            cmd = {"cmd": str(cmd)}
        name = str(cmd.get("cmd") or "").strip().lower()
        ts = cmd.get("ts")
        if ts:
            try:
                age = (clock.utcnow() - clock.from_iso(ts)).total_seconds()
            except (TypeError, ValueError):
                age = None
            if age is not None and age > COMMAND_MAX_AGE_S:
                msg = "discarded a %r command from %s: it is %.0f minutes old (was the engine stopped?)" % (
                    name, ts, age / 60.0)
                log.warning(msg)
                self._event("warn", "command_discarded", msg)
                self._notify("tvbridge: command discarded", msg, "warn")
                return
        if name == "flatten":
            msg = "flatten requested (%s): closing every position" % (cmd.get("by") or "cli")
            log.warning(msg)
            self._event("warn", "command", msg, {"cmd": name})
            self._notify("tvbridge: flatten requested", msg, "warn")
            self._enqueue_flatten("command")
        else:
            log.warning("ignoring unknown command %r", name)
            self._event("warn", "command", "ignored unknown command %r" % name)

    def _write_heartbeat(self, stopped: bool = False) -> None:
        now = clock.utcnow()
        with self._lock:
            current = self._current.describe() if self._current else None
            untracked = [p.to_dict() for p in self._untracked]
            delayed = len(self._delayed)
            snap = self._last_snapshot
        data = {
            "ts": clock.iso(now),
            "pid": os.getpid(),
            "version": __version__,
            "mode": self.cfg.executor.mode,
            "executor": getattr(self.executor, "name", "?"),
            "port": self.server.port if self.server is not None and self.server.running else None,
            "started_at": clock.iso(self._started_at) if self._started_at else None,
            "queue_size": self._queue.qsize() + delayed,
            "current_task": current,
            "untracked": untracked,
            "positions_uncertain": self._positions_uncertain_text(),
            "last_account_read": clock.iso(snap.ts) if snap is not None else None,
            "account_read_failures": self._read_failures,
            "last_account_error": self._last_read_error,
            "account_poll_crashes": self._crashed_polls,
            "executor_stalled": self._stall_reported,
            "stopped": stopped,
        }
        try:
            _atomic_write_json(self.cfg.heartbeat_path, data)
        except OSError as e:
            log.warning("cannot write %s: %s", self.cfg.heartbeat_path, e)
        self._last_heartbeat_mono = time.monotonic()

    def _cleanup(self) -> None:
        """Delete screenshot day folders older than keep_screenshots_days (and the oldest ones while
        shots/ is over its size cap); prune old snapshots."""
        keep = int(self.cfg.executor.gui.keep_screenshots_days)
        shots = Path(self.cfg.shots_dir)
        removed = 0
        if shots.is_dir():
            cutoff = clock.utcnow().date() - timedelta(days=keep)
            today = clock.utcnow().strftime("%Y%m%d")
            days = []  # type: List[Tuple[str, Path, int]]
            for d in shots.iterdir():
                if not d.is_dir() or not _DAY_DIR_RE.match(d.name):
                    continue   # e.g. shots/calibration is kept
                try:
                    day = datetime.strptime(d.name, "%Y%m%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    shutil.rmtree(str(d), ignore_errors=True)
                    removed += 1
                    continue
                days.append((d.name, d, _dir_size(d)))
            total = sum(size for _n, _d, size in days)
            for name, d, size in sorted(days):
                if total <= self.shots_max_bytes:
                    break
                if name == today:
                    continue   # today's evidence is kept
                shutil.rmtree(str(d), ignore_errors=True)
                total -= size
                removed += 1
                log.warning("screenshots over the %.1f GB cap: removed %s", self.shots_max_bytes / 1024.0 ** 3, d)
        pruned = 0
        try:
            pruned = self.store.prune_snapshots(clock.utcnow() - timedelta(days=SNAPSHOT_RETENTION_DAYS))
        except Exception as e:
            log.warning("cannot prune old snapshots: %s", e)
        if removed or pruned:
            log.info("cleanup: removed %d screenshot folder(s), %d old snapshot(s)", removed, pruned)


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
