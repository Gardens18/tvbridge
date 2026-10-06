"""Engine safety tests for the review fixes (kill switch, reconciliation, rollover, reversals,
pre/post-click failures, watchdog ...).

Most tests drive a :class:`ScriptedExecutor` (no GUI at all); the GUI ones use the simulated
MetaTrader in tests/fakes.py. The clock is frozen at Tuesday 2026-10-06 10:00 server time
(07:00 UTC, server UTC+3) unless a test moves it.
"""

import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone

from tests.fakes import FakeMt5Driver, make_calibration
from tvbridge import clock, risk
from tvbridge.config import config_from_dict, deep_merge
from tvbridge.engine import ACCOUNT, OPEN, Engine, Task, config_warnings, engine_alive, read_heartbeat
from tvbridge.executors.base import Executor, ExecutorError
from tvbridge.executors.mt5gui import Mt5GuiExecutor
from tvbridge.models import AccountSnapshot, ObservedPosition, OrderResult, Signal
from tvbridge.notify import NullNotifier
from tvbridge.store import Store

SECRET = "engine-safety-secret-0123456789"
T0 = datetime(2026, 10, 6, 7, 0, 0, tzinfo=timezone.utc)   # Tue 10:00 server time
UTC = timezone.utc

_QUIET = logging.NullHandler()


def setUpModule() -> None:
    logging.getLogger("tvbridge").addHandler(_QUIET)


def tearDownModule() -> None:
    logging.getLogger("tvbridge").removeHandler(_QUIET)


def server_time(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    """UTC instant of a server (UTC+3) wall-clock time."""
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=UTC) - timedelta(hours=3)


class ScriptedExecutor(Executor):
    """A deterministic broker: positions, balance and failures are set by the test."""

    name = "scripted"

    def __init__(self) -> None:
        self.balance = 50000.0
        self.equity = None            # None: balance + sum(profit)
        self.margin = None            # None: 1,000 per lot of all positions (hidden ones too)
        self.positions = []           # list of ObservedPosition
        self.hidden = set()           # tickets the "Toolbox" cannot show
        self.note = ""                # positions_note -> positions None
        self.read_error = None        # ExecutorError to raise from read_account
        self.read_block = None        # threading.Event: read_account waits for it
        self.reads = 0
        self.fill_price = None        # None: the alert price
        self.open_hook = None         # callable(req) -> OrderResult
        self.close_hook = None        # callable(symbol, side) -> list (or None for the default)
        self.close_all_script = []    # items: list of results, or callable() -> list
        self.opens = []
        self.close_calls = []
        self.close_all_calls = 0
        self._next = 1

    # -- helpers
    def add(self, symbol, side, lots, open_price=1.1, sl=1.09, profit=0.0, ticket=None):
        if ticket is None:
            ticket = "T%d" % self._next
            self._next += 1
        p = ObservedPosition(symbol, side, lots, ticket=ticket, open_price=open_price, sl=sl, profit=profit)
        self.positions.append(p)
        return p

    def visible(self):
        return [p for p in self.positions if p.ticket not in self.hidden]

    # -- Executor API
    def read_account(self):
        self.reads += 1
        if self.read_block is not None:
            self.read_block.wait(30)
        if self.read_error is not None:
            raise self.read_error
        margin = self.margin if self.margin is not None else 1000.0 * sum(p.lots for p in self.positions)
        equity = self.equity if self.equity is not None else self.balance + sum(p.profit or 0 for p in self.positions)
        return AccountSnapshot(ts=clock.utcnow(), balance=self.balance, equity=equity, margin=margin,
                               positions=None if self.note else [ObservedPosition(**p.to_dict()) for p in self.visible()],
                               positions_note=self.note, source="scripted")

    def open_market(self, req):
        self.opens.append(req)
        if self.open_hook is not None:
            return self.open_hook(req)
        price = self.fill_price if self.fill_price is not None else req.price_hint
        p = self.add(req.symbol, req.side, req.lots, open_price=price, sl=req.sl)
        return OrderResult("filled", "Done %s" % p.ticket, fill_price=price, ticket=p.ticket, lots=req.lots)

    def _close(self, p):
        self.positions.remove(p)
        self.balance += float(p.profit or 0.0) - 5.0 * p.lots
        return OrderResult("filled", "closed #%s" % p.ticket, ticket=p.ticket, lots=p.lots)

    def close_positions(self, symbol, side=None):
        self.close_calls.append((symbol, side))
        if self.close_hook is not None:
            res = self.close_hook(symbol, side)
            if res is not None:
                return res
        key = symbol.split(".")[0].upper()
        return [self._close(p) for p in list(self.visible())
                if p.symbol.split(".")[0].upper() == key and (side is None or p.side == side)]

    def close_all(self):
        self.close_all_calls += 1
        if self.close_all_script:
            item = self.close_all_script.pop(0)
            return item() if callable(item) else list(item)
        return [self._close(p) for p in list(self.visible())]

    def health(self):
        return {"ok": True, "detail": "scripted"}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tvb-safety-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = T0
        clock.set_clock(lambda: self.now)
        self.addCleanup(clock.set_clock, None)
        self.engine = None
        self.store = None

    def tearDown(self) -> None:
        if self.engine is not None:
            if getattr(self, "executor", None) is not None and getattr(self.executor, "read_block", None):
                self.executor.read_block.set()
            self.engine.stop(timeout=5)
        if self.store is not None:
            self.store.close()

    def make_cfg(self, **sections):
        base = {"server": {"secret": SECRET, "port": 0}, "notify": {"macos": False},
                "executor": {"mode": "paper", "gui": {"account_poll_s": 3600}}}
        return config_from_dict(deep_merge(base, sections), self.tmp)

    def build(self, cfg=None, executor=None, store_cls=Store):
        self.cfg = cfg or self.make_cfg()
        self.store = store_cls(self.cfg.db_path)
        self.executor = executor if executor is not None else ScriptedExecutor()
        self.notifier = NullNotifier()
        self.engine = Engine(self.cfg, self.store, self.executor, self.notifier, start_server=False)
        self.engine.tick_s = 0.02
        self.engine.kill_confirm_delay_s = 0.0
        return self.engine

    def start(self, cfg=None, executor=None, **kw):
        self.build(cfg, executor, **kw)
        self.engine.start()
        return self.engine

    def signal(self, action, sig_id, symbol="EURUSD", price=1.10000, sl=1.09800, tp=None, side=None,
               fired=None, received=None):
        tv = symbol
        return Signal(id="%s:%s" % (action, sig_id), action=action, tv_symbol=tv if action != "close_all" else "",
                      symbol=(tv + ".h") if action != "close_all" else "", side=side, price=price, sl=sl, tp=tp,
                      risk_pct=None, quote_usd=None, fired_at=fired or self.now, received_at=received or self.now)

    def submit(self, sig):
        self.store.insert_signal(sig)
        self.engine.submit_signal(sig)
        self.assertTrue(self.engine.wait_idle(10), "engine did not become idle")
        return self.store.get_signal_row(sig.id)

    def poll(self):
        self.engine.request_account_poll()
        self.assertTrue(self.engine.wait_idle(10))

    def ledger(self):
        return self.store.open_ledger_positions()

    def events(self, kind):
        return [e for e in self.store.recent_events(500) if e["kind"] == kind]

    def sent(self, level=None, title=None):
        return [n for n in self.notifier.sent
                if (level is None or n.level == level) and (title is None or title in n.title)]


# ---------------------------------------------------------------------------------- flatten / kill switch


class FlattenEvidenceTests(Base):
    """An empty close_all result is not "flat" while the ledger or margin says otherwise."""

    def open_and_hide(self):
        self.start()
        row = self.submit(self.signal("buy", "b1"))
        self.assertEqual(row["status"], "done", row["reason"])
        self.executor.hidden = {self.executor.positions[0].ticket}      # Toolbox stops showing the row

    def test_flatten_with_invisible_rows_keeps_the_ledger_and_fails_loudly(self):
        self.open_and_hide()
        self.engine.request_flatten("test")
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(len(self.ledger()), 1)                          # not wiped
        fails = self.events("close_failed")
        self.assertTrue(any("NO_ROWS_VISIBLE" in e["message"] for e in fails), fails)
        self.assertTrue(self.sent("critical", "FLATTEN FAILED"))

    def test_close_all_signal_with_invisible_rows_is_failed(self):
        self.open_and_hide()
        row = self.submit(self.signal("close_all", "all"))
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("CLOSE_FAILED: NO_ROWS_VISIBLE"), row["reason"])
        self.assertEqual(len(self.ledger()), 1)

    def test_margin_alone_is_evidence(self):
        self.start()
        self.executor.margin = 1100.0             # MT5 says margin is used, no row readable
        self.poll()
        self.engine.request_flatten("test")
        self.assertTrue(self.engine.wait_idle(10))
        self.assertTrue(any("margin" in e["message"] for e in self.events("close_failed")))

    def test_nothing_open_is_still_done(self):
        self.start()
        row = self.submit(self.signal("close_all", "all"))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["reason"], "NO_POSITION: nothing open")


class KillSwitchRetryTests(Base):
    def kill_setup(self):
        self.start()
        self.assertEqual(self.submit(self.signal("buy", "b1"))["status"], "done")
        self.executor.equity = 48100.0                    # below the 48,150 kill floor

    def test_failed_kill_flatten_is_retried_within_seconds(self):
        self.kill_setup()
        self.executor.close_all_script = [[OrderResult("error", "CLOSE_BUTTON_NOT_FOUND: test")]]
        self.poll()
        self.assertTrue((self.store.get_kv("halted") or "").startswith("KILL"))
        self.assertEqual(self.executor.close_all_calls, 1)
        wait = self.engine._kill_next_retry_mono - time.monotonic()
        self.assertTrue(10 < wait <= 15.5, wait)              # not 300 s
        self.poll()                                           # too early: nothing new
        self.assertEqual(self.executor.close_all_calls, 1)
        self.engine._kill_next_retry_mono = time.monotonic() - 1   # 15 s later
        self.poll()
        self.assertEqual(self.executor.close_all_calls, 2)
        self.assertEqual(self.executor.positions, [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.engine._kill_fail_streak, 0)

    def test_backoff_grows_and_notifications_are_throttled(self):
        self.kill_setup()
        fail = [OrderResult("error", "CLOSE_BUTTON_NOT_FOUND: test")]
        self.executor.close_all_script = [list(fail) for _ in range(8)]
        delays = []
        self.poll()
        for _ in range(6):
            delays.append(round(self.engine._kill_next_retry_mono - time.monotonic()))
            self.engine._kill_next_retry_mono = time.monotonic() - 1
            self.poll()
        self.assertEqual(delays[:4], [15, 30, 60, 120])
        self.assertEqual(delays[-1], 300)
        self.assertEqual(len(self.sent("critical", "FLATTEN FAILED")), 1)

    def test_kill_detected_by_a_manual_flattens_own_read_queues_a_follow_up(self):
        self.start()
        self.assertEqual(self.submit(self.signal("buy", "b1"))["status"], "done")

        def failing_close_all():
            self.executor.equity = 48100.0               # the market falls while closing fails
            return [OrderResult("error", "POSITION_DIALOG_NOT_OPENED: test")]

        self.executor.close_all_script = [failing_close_all]
        self.store.set_kv("command", json.dumps({"cmd": "flatten", "ts": clock.iso(self.now)}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.executor.close_all_calls < 2:
            time.sleep(0.02)
        self.assertTrue(self.engine.wait_idle(10))
        self.assertTrue((self.store.get_kv("halted") or "").startswith("KILL"))
        self.assertEqual(self.executor.close_all_calls, 2)     # the kill's own FLATTEN ran
        self.assertEqual(self.executor.positions, [])

    def test_implausible_misread_is_reread_before_killing(self):
        self.kill_setup()
        self.executor.equity = None
        self.poll()                                     # previous read: 50,000
        reads = self.executor.reads
        real = self.executor.read_account

        def misread_once():
            self.executor.read_account = real
            snap = real()
            snap.equity = 4981.23                       # a dropped digit
            return snap

        self.executor.read_account = misread_once
        self.poll()
        self.assertIsNone(self.store.get_kv("halted"))
        self.assertEqual(self.executor.reads, reads + 2)  # the misread + one confirming read
        self.assertTrue(self.events("equity_misread"))
        self.assertEqual(self.executor.close_all_calls, 0)

    def test_plausible_drop_kills_without_a_reread(self):
        self.kill_setup()
        self.executor.equity = 48300.0
        self.poll()
        self.assertIsNone(self.store.get_kv("halted"))
        reads = self.executor.reads
        self.executor.equity = 48100.0
        self.poll()
        self.assertTrue((self.store.get_kv("halted") or "").startswith("KILL"))
        self.assertGreaterEqual(self.executor.close_all_calls, 1)
        self.assertEqual(self.executor.reads, reads + 2)   # the poll + the read after the flatten


class FailingSnapshotStore(Store):
    fail_snapshots = False
    fail_halted = False

    def add_snapshot(self, snap):
        if self.fail_snapshots:
            raise __import__("sqlite3").OperationalError("database or disk is full")
        return super().add_snapshot(snap)

    def set_kv(self, key, value):
        if self.fail_halted and key == "halted":
            raise __import__("sqlite3").OperationalError("database is locked")
        return super().set_kv(key, value)


class DatabaseFailureTests(Base):
    def test_kill_switch_runs_when_the_snapshot_cannot_be_saved(self):
        self.start(store_cls=FailingSnapshotStore)
        self.assertEqual(self.submit(self.signal("buy", "b1"))["status"], "done")
        self.store.fail_snapshots = True
        self.executor.equity = 48000.0
        self.poll()
        self.assertTrue((self.store.get_kv("halted") or "").startswith("KILL"))
        self.assertGreaterEqual(self.executor.close_all_calls, 1)
        self.assertTrue(self.sent("critical", "DATABASE ERROR"))

    def test_flatten_is_queued_even_if_the_halt_cannot_be_written(self):
        self.start(store_cls=FailingSnapshotStore)
        self.assertEqual(self.submit(self.signal("buy", "b1"))["status"], "done")
        self.store.fail_halted = True
        self.executor.equity = 48000.0
        self.poll()
        self.assertGreaterEqual(self.executor.close_all_calls, 1)
        self.assertEqual(self.executor.positions, [])
        self.assertEqual(len(self.sent("critical", "KILL SWITCH")), 1)    # not re-fired on every poll
        self.poll()
        self.assertEqual(len(self.sent("critical", "KILL SWITCH")), 1)
        self.store.fail_halted = False
        self.poll()
        self.assertTrue((self.store.get_kv("halted") or "").startswith("KILL"))   # written once possible

    def test_crashed_account_poll_is_critical_and_counted(self):
        self.build()
        self.engine._task_crashed(Task(ACCOUNT, reason="poll"), RuntimeError("boom"))
        self.assertEqual(self.engine._crashed_polls, 1)
        self.assertTrue(self.sent("critical", "ACCOUNT POLL CRASHED"))
        self.assertEqual(self.engine.status()["engine"]["account_poll_crashes"], 1)


# ---------------------------------------------------------------------------------- reconciliation


class ReconcileTests(Base):
    def two_positions(self):
        self.start()
        self.assertEqual(self.submit(self.signal("buy", "e"))["status"], "done")
        self.assertEqual(self.submit(self.signal("buy", "g", symbol="GBPUSD", price=1.3, sl=1.297))["status"], "done")
        self.poll()
        return self.executor.positions[0]

    def test_invisible_row_without_balance_change_stays_booked_and_blocks_entries(self):
        eur = self.two_positions()
        self.executor.hidden = {eur.ticket}
        self.poll()
        self.poll()
        self.poll()
        self.assertEqual(len(self.ledger()), 2)                 # risk still booked
        self.assertEqual(len(self.sent("warn", "not visible")), 1)
        self.assertIn("not visible", self.engine.status()["positions_uncertain"])
        row = self.submit(self.signal("buy", "a", symbol="AUDUSD", price=0.65, sl=0.648))
        self.assertTrue(row["reason"].startswith("POSITIONS_UNCERTAIN"), row["reason"])
        # the row shows again: cleared
        self.executor.hidden = set()
        self.poll()
        self.assertEqual(self.engine.status()["positions_uncertain"], "")
        row = self.submit(self.signal("buy", "a2", symbol="AUDUSD", price=0.65, sl=0.648))
        self.assertEqual(row["status"], "done", row["reason"])

    def test_invisible_row_with_a_booked_close_is_closed(self):
        eur = self.two_positions()
        self.executor.positions.remove(eur)                     # SL hit on the server
        self.executor.balance -= 250.0
        self.poll()
        self.assertEqual(len(self.ledger()), 2)                 # one miss is not enough
        self.poll()
        self.assertEqual([r["symbol"] for r in self.ledger()], ["GBPUSD.h"])
        closed = [r for r in self.store.all_ledger_positions() if r["status"] == "closed"]
        self.assertEqual(closed[0]["close_reason"], "closed_on_server")

    def test_resume_confirms_invisible_rows_as_closed(self):
        eur = self.two_positions()
        self.executor.hidden = {eur.ticket}
        self.poll()
        self.poll()
        self.assertTrue(self.engine.status()["positions_uncertain"])
        self.store.set_kv("resume_ack", clock.iso(self.now))   # `tvbridge resume`
        self.poll()
        self.assertEqual([r["symbol"] for r in self.ledger()], ["GBPUSD.h"])
        self.assertEqual(self.engine.status()["positions_uncertain"], "")
        self.assertIsNone(self.store.get_kv("resume_ack"))

    def test_empty_list_with_margin_is_not_counted_as_missing(self):
        self.two_positions()
        self.executor.hidden = {p.ticket for p in self.executor.positions}   # nothing readable, margin > 0
        for _ in range(4):
            self.poll()
        self.assertEqual(len(self.ledger()), 2)
        self.assertEqual(self.engine.status()["positions_uncertain"], "")
        self.assertEqual(self.engine._missing_counts, {})

    def test_incomplete_position_list_refuses_entries(self):
        self.start()
        self.executor.note = "TOOLBOX_INCOMPLETE: the Trade list header is not readable"
        for _ in range(4):
            self.poll()
        row = self.submit(self.signal("buy", "x"))
        self.assertTrue(row["reason"].startswith("POSITIONS_UNCERTAIN"), row["reason"])
        self.assertIn("TOOLBOX_INCOMPLETE", row["reason"])
        self.assertTrue(self.sent("warn", "not verifiable"))

    def test_ticket_on_another_symbol_is_a_critical_mismatch(self):
        self.start()
        self.assertEqual(self.submit(self.signal("buy", "e"))["status"], "done")
        self.executor.positions[0].symbol = "USDJPY.h"          # MT5 shows that ticket on another instrument
        self.poll()
        self.poll()
        self.assertEqual(len(self.sent("critical", "TICKET/SYMBOL MISMATCH")), 1)

    def test_confirmed_balance_is_the_lower_of_two_reads(self):
        self.start()
        self.executor.balance = 49000.0
        self.poll()
        self.executor.balance = 59000.0                         # one high misread
        self.poll()
        st = self.engine._risk_state(clock.utcnow())
        self.assertEqual(st.confirmed_balance, 49000.0)


# ---------------------------------------------------------------------------------- rollover


class RolloverTests(Base):
    MON = date(2026, 10, 5)
    TUE = date(2026, 10, 6)

    def setUp(self) -> None:
        super().setUp()
        self.build(self.make_cfg(executor={"gui": {"account_poll_s": 15}}))

    def at(self, day, hh, mm, ss=0):
        self.now = server_time(day, hh, mm, ss)

    def test_stale_basis_defers_and_needs_set_reference(self):
        self.at(self.MON, 18, 0)
        self.engine._poll_account("test")                       # last good read: 50,000
        self.executor.read_error = ExecutorError("ACCOUNT_UNREADABLE", "screen locked")
        self.at(self.MON, 23, 30)
        self.engine._poll_account("test")
        self.at(self.TUE, 0, 0, 1)
        self.assertIsNone(self.engine._maybe_rollover())        # no fresh read yet: wait
        self.assertIsNone(self.store.get_day_state(self.TUE))
        self.executor.read_error = None
        self.executor.balance = 51000.0                         # a TP filled at 23:00
        self.at(self.TUE, 0, 0, 15)
        self.engine._poll_account("test")
        ds = self.store.get_day_state(self.TUE)
        self.assertEqual((ds["reference"], ds["source"]), (51000.0, "stale_estimate"))
        self.assertTrue(self.sent("critical", "new server day"))
        self.at(self.TUE, 10, 0)
        self.engine._poll_account("test")
        plan = risk.plan_entry(self.signal("buy", "x"), self.engine._risk_state(clock.utcnow()), self.cfg)
        self.assertEqual(plan.code, "NO_DAY_REFERENCE")
        self.assertIn("stale", plan.reason)
        self.store.set_day_state(self.TUE, 51000.0, 51000.0, "manual")   # `tvbridge set-reference 51000`
        plan = risk.plan_entry(self.signal("buy", "x"), self.engine._risk_state(clock.utcnow()), self.cfg)
        self.assertTrue(plan.approved, plan.reason)

    def test_stale_basis_without_positions_and_same_balance_is_exact(self):
        self.at(self.MON, 18, 0)
        self.engine._poll_account("test")
        self.at(self.TUE, 0, 0, 15)
        self.engine._poll_account("test")
        self.assertEqual(self.store.get_day_state(self.TUE)["source"], "rollover")

    def test_first_reads_after_midnight_raise_a_rollover_reference(self):
        self.at(self.MON, 23, 59, 50)
        self.engine._poll_account("test")
        self.at(self.TUE, 0, 0, 1)
        self.engine._maybe_rollover()
        self.assertEqual(self.store.get_day_state(self.TUE)["reference"], 50000.0)
        self.executor.balance = 51000.0                         # TP at 23:59:59
        self.at(self.TUE, 0, 0, 15)
        self.engine._poll_account("test")
        ds = self.store.get_day_state(self.TUE)
        self.assertEqual((ds["reference"], ds["source"]), (51000.0, "rollover"))
        self.executor.balance = 52000.0
        self.at(self.TUE, 0, 5)                                 # outside the refine window: never lowered/raised
        self.engine._poll_account("test")
        self.assertEqual(self.store.get_day_state(self.TUE)["reference"], 51000.0)

    def test_manual_reference_is_never_refined(self):
        self.at(self.MON, 23, 59, 50)
        self.engine._poll_account("test")
        self.store.set_day_state(self.TUE, 50500.0, 50500.0, "manual")
        self.executor.balance = 51000.0
        self.at(self.TUE, 0, 0, 15)
        self.engine._poll_account("test")
        self.assertEqual(self.store.get_day_state(self.TUE)["reference"], 50500.0)

    def test_outlier_before_midnight_is_ignored(self):
        self.executor.balance = 50812.35
        self.at(self.MON, 23, 59, 30)
        self.engine._poll_account("test")
        self.store.add_snapshot(AccountSnapshot(ts=server_time(self.MON, 23, 59, 45), balance=50812.35,
                                                equity=56812.35, source="scripted"))   # misread 5 -> 6
        self.at(self.TUE, 0, 0, 1)
        self.engine._maybe_rollover()
        self.assertEqual(self.store.get_day_state(self.TUE)["reference"], 50812.35)
        self.assertTrue(self.events("reference_outlier"))


# ---------------------------------------------------------------------------------- entries


class ReversalFirstTests(Base):
    """Owner decision D1: the opposite position is closed first, whatever happens to the entry."""

    def long_open(self, **cfg):
        self.start(self.make_cfg(**cfg) if cfg else None)
        self.assertEqual(self.submit(self.signal("buy", "b1"))["status"], "done")
        self.assertEqual(len(self.executor.positions), 1)

    def assertReversed(self):
        self.assertEqual(self.executor.close_calls[-1], ("EURUSD.h", "buy"))
        self.assertEqual([p for p in self.executor.positions if p.side == "buy"], [])
        closed = [r for r in self.store.all_ledger_positions() if r["side"] == "buy"]
        self.assertEqual(closed[0]["close_reason"], "reversal")

    def test_paused_entry_still_closes_the_opposite_position(self):
        self.long_open()
        self.store.set_kv("paused", "1")
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertTrue(row["reason"].startswith("PAUSED"), row["reason"])
        self.assertReversed()
        self.assertEqual(self.executor.positions, [])

    def test_entry_without_sl_still_closes_the_opposite_position(self):
        self.long_open()
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=None))
        self.assertTrue(row["reason"].startswith("SL_MISSING"), row["reason"])
        self.assertReversed()

    def test_stale_entry_still_closes_the_opposite_position(self):
        self.long_open()
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102, fired=self.now - timedelta(seconds=60)))
        self.assertEqual(row["status"], "expired")
        self.assertReversed()

    def test_reversal_older_than_the_exit_limit_closes_nothing(self):
        self.long_open()
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102, fired=self.now - timedelta(minutes=20)))
        self.assertEqual(row["status"], "expired")
        self.assertEqual(self.executor.close_calls, [])
        self.assertEqual(len(self.executor.positions), 1)
        self.assertTrue(self.sent("warn", "late reversal"))

    def test_entry_outside_the_window_still_closes_the_opposite_position(self):
        self.long_open()
        self.now = server_time(date(2026, 10, 6), 23, 55)
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertTrue(row["reason"].startswith("WINDOW"), row["reason"])
        self.assertReversed()

    def test_without_reverse_on_opposite_nothing_is_closed(self):
        self.long_open(risk={"reverse_on_opposite": False})
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertTrue(row["reason"].startswith("OPPOSITE_OPEN"), row["reason"])
        self.assertEqual(self.executor.close_calls, [])
        self.assertEqual(len(self.executor.positions), 1)

    def test_failed_reversal_close_places_no_entry(self):
        self.long_open()
        self.executor.close_hook = lambda s, side: [OrderResult("rejected", "REJECTED: Market is closed")]
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertTrue(row["reason"].startswith("REVERSAL_CLOSE_FAILED"), row["reason"])
        self.assertEqual(len(self.executor.opens), 1)
        self.assertTrue(self.sent("critical", "REVERSAL CLOSE FAILED"))

    def test_slow_reversal_makes_the_entry_stale(self):
        self.long_open()

        def slow_close(symbol, side):
            self.now = self.now + timedelta(seconds=50)
            return None   # then the default close

        self.executor.close_hook = slow_close
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertEqual(row["status"], "expired", row["reason"])
        self.assertTrue(row["reason"].startswith("STALE_SIGNAL"))
        self.assertEqual(len(self.executor.opens), 1)
        self.assertReversed()

    def test_reversal_after_the_opposite_was_stopped_out_unreconciled(self):
        # the long was stopped out on the server a moment ago: MT5 no longer shows it, the ledger
        # still does (not reconciled yet). The sell must go ahead, not be refused.
        self.long_open()
        long_pos = self.executor.positions[0]
        self.executor.positions.remove(long_pos)
        self.executor.balance -= 200.0
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual([p.side for p in self.executor.positions], ["sell"])

    def test_opposite_still_shown_after_the_close_refuses_the_entry(self):
        for block in (True, False):
            with self.subTest(block_untracked_positions=block):
                if self.engine is not None:
                    self.engine.stop(timeout=5)
                    self.store.close()
                    shutil.rmtree(self.tmp, True)
                    os.makedirs(self.tmp)
                self.long_open(risk={"block_untracked_positions": block})
                # MT5 reports the close as done, but the position stays listed
                self.executor.close_hook = lambda s, side: [OrderResult("filled", "closed", ticket="T1")]
                row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
                want = "UNTRACKED_POSITIONS" if block else "REVERSAL_CLOSE_FAILED"
                self.assertTrue(row["reason"].startswith(want), row["reason"])
                self.assertEqual(len(self.executor.opens), 1)

    def test_reversal_then_entry(self):
        self.long_open()
        row = self.submit(self.signal("sell", "s1", price=1.1, sl=1.102))
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual([p.side for p in self.executor.positions], ["sell"])
        self.assertReversed()


class SupersededEntryTests(Base):
    def test_close_received_after_an_entry_cancels_it(self):
        self.build()
        self.store.insert_signal(self.signal("buy", "e1", received=self.now))
        self.store.insert_signal(self.signal("close", "x1", side="buy", price=None, sl=None,
                                             received=self.now + timedelta(seconds=1)))
        self.engine.start()                                # both re-submitted; the close runs first
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.store.get_signal_row("close:x1")["status"], "done")
        row = self.store.get_signal_row("buy:e1")
        self.assertEqual(row["status"], "expired")
        self.assertTrue(row["reason"].startswith("SUPERSEDED_BY_CLOSE"), row["reason"])
        self.assertEqual(self.executor.opens, [])

    def test_close_all_after_an_entry_cancels_it(self):
        self.build()
        self.store.insert_signal(self.signal("buy", "e1"))
        self.store.insert_signal(self.signal("close_all", "all", price=None, sl=None))   # same instant, later row
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.store.get_signal_row("buy:e1")["status"], "expired")

    def test_close_of_the_other_side_does_not_cancel(self):
        self.build()
        self.store.insert_signal(self.signal("buy", "e1"))
        self.store.insert_signal(self.signal("close", "x1", side="sell", price=None, sl=None,
                                             received=self.now + timedelta(seconds=1)))
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.store.get_signal_row("buy:e1")["status"], "done")


class EntryGuardTests(Base):
    def test_fill_worse_than_planned_books_the_real_risk(self):
        self.start()
        self.executor.fill_price = 1.10030                 # 3 pips worse on a 5-pip stop
        row = self.submit(self.signal("buy", "b1", price=1.10000, sl=1.09950))
        self.assertEqual(row["status"], "done", row["reason"])
        plan_risk = row["result"]["plan"]["risk_usd"]
        led = self.ledger()[0]
        lots = led["lots"]
        actual = lots * (0.0008 * 100000 + 5.0)
        self.assertAlmostEqual(led["risk_usd"], actual, places=4)
        self.assertGreater(led["risk_usd"], plan_risk)
        self.assertTrue(self.sent(title="fill worse than planned"))

    def test_better_fill_never_books_less(self):
        self.start()
        self.executor.fill_price = 1.09990
        row = self.submit(self.signal("buy", "b1", price=1.10000, sl=1.09800))
        self.assertAlmostEqual(self.ledger()[0]["risk_usd"], row["result"]["plan"]["risk_usd"], places=2)

    def test_flatten_requested_blocks_an_approved_entry(self):
        self.build()
        self.engine._poll_account("test")
        self.engine._maybe_rollover()
        self.store.set_kv("command", json.dumps({"cmd": "flatten", "ts": clock.iso(self.now)}))
        sig = self.signal("buy", "b1")
        self.store.insert_signal(sig)
        self.engine._do_open(Task(OPEN, sig))
        row = self.store.get_signal_row(sig.id)
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("FLATTEN_PENDING"), row["reason"])
        self.assertEqual(self.executor.opens, [])

    def test_unexpected_exception_from_the_executor_halts(self):
        self.start()

        def boom(req):
            raise RuntimeError("driver exploded")

        self.executor.open_hook = boom
        row = self.submit(self.signal("buy", "b1"))
        self.assertEqual(row["status"], "failed")
        self.assertTrue((self.store.get_kv("halted") or "").startswith("UNCERTAIN_EXECUTION"))

    def test_scripted_preclick_errors_do_not_halt_but_escalate(self):
        self.start()
        self.executor.open_hook = lambda req: OrderResult("error", "VERIFY_FAILED: test; nothing was sent")
        for i in range(3):
            self.now = T0 + timedelta(seconds=i)
            row = self.submit(self.signal("buy", "b%d" % i))
            self.assertEqual(row["status"], "failed")
        self.assertIsNone(self.store.get_kv("halted"))
        levels = [e["level"] for e in self.events("entry_failed")]
        self.assertEqual(sorted(levels), ["critical", "warn", "warn"])


class MinHoldTests(Base):
    def test_deferred_close_never_closes_a_newer_position(self):
        self.start(self.make_cfg(risk={"min_hold_s_for_signal_close": 180}))
        self.assertEqual(self.submit(self.signal("buy", "a"))["status"], "done")
        self.now = T0 + timedelta(seconds=60)
        row = self.submit(self.signal("close", "c1", price=None, sl=None))
        self.assertTrue(row["reason"].startswith("DEFERRED"), row["reason"])
        self.now = T0 + timedelta(seconds=90)
        row = self.submit(self.signal("sell", "b", price=1.1, sl=1.102))   # reversal: closes A (no min hold)
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual([p.side for p in self.executor.positions], ["sell"])
        self.now = T0 + timedelta(seconds=185)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.store.get_signal_row("close:c1")["status"] == "queued":
            time.sleep(0.02)
        self.assertTrue(self.engine.wait_idle(10))
        row = self.store.get_signal_row("close:c1")
        self.assertEqual(row["status"], "done")
        self.assertIn("opened after this close alert", row["reason"])
        self.assertEqual([p.side for p in self.executor.positions], ["sell"])     # B untouched


# ---------------------------------------------------------------------------------- GUI executor


class GuiEngineTests(Base):
    def gui(self, **driver_kw):
        cfg = self.make_cfg(executor={"mode": "live"}, account={"account_login": "12345678"})
        self.driver = FakeMt5Driver(**driver_kw)
        ex = Mt5GuiExecutor(cfg, self.driver, make_calibration(), rehearsal=False, shots_dir=cfg.shots_dir)
        self.start(cfg, ex)
        return ex

    def buy(self, sig_id, **kw):
        kw.setdefault("price", 1.08345)
        kw.setdefault("sl", 1.08045)
        return self.submit(self.signal("buy", sig_id, **kw))

    def test_preclick_verification_failure_does_not_halt(self):
        self.gui(volume_ignores_typing=True)
        row = self.buy("v1")
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("VERIFY_FAILED"), row["reason"])
        self.assertIsNone(self.store.get_kv("halted"))
        self.assertEqual(self.driver.order_button_clicks, [])
        self.assertTrue(self.sent("warn", "nothing sent"))
        for i in (2, 3):
            self.now = T0 + timedelta(seconds=i)
            self.buy("v%d" % i)
        self.assertTrue(self.sent("critical", "nothing sent"))
        self.assertIsNone(self.store.get_kv("halted"))

    def test_dialog_not_opened_does_not_halt(self):
        self.gui(dialog_opens=False)
        row = self.buy("d1")
        self.assertTrue(row["reason"].startswith("ORDER_DIALOG_NOT_OPENED"), row["reason"])
        self.assertIsNone(self.store.get_kv("halted"))

    def test_result_mismatch_halts(self):
        self.gui(order_outcome="wrong_side")
        row = self.buy("w1")
        self.assertIn("RESULT_MISMATCH", row["reason"])
        self.assertTrue((self.store.get_kv("halted") or "").startswith("UNCERTAIN_EXECUTION"))

    def test_abort_check_is_wired_to_the_engine(self):
        ex = self.gui()
        self.assertEqual(ex.abort_check, self.engine._entry_block_reason)

    def test_position_without_spec_blocks_entries_and_is_flattened(self):
        self.gui(positions=[{"ticket": "777777", "symbol": "BTCUSD.h", "side": "buy", "lots": 0.1,
                             "open_price": 60000.0, "sl": 59000.0, "tp": 0.0, "price": 60000.0, "profit": 0.0}])
        self.assertEqual([p["symbol"] for p in self.engine.status()["untracked"]], ["BTCUSD.h"])
        row = self.buy("u1")
        self.assertTrue(row["reason"].startswith("UNTRACKED_POSITIONS"), row["reason"])
        self.engine.request_flatten("test")
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.driver.positions, [])

    def test_stop_loss_removed_in_mt5_blocks_entries(self):
        self.gui()
        self.assertEqual(self.buy("b1")["status"], "done")
        self.driver.positions[0]["sl"] = 0.0
        self.poll()
        self.assertTrue(self.sent("critical", "STOP-LOSS MISSING"))
        self.now = T0 + timedelta(seconds=5)
        row = self.submit(self.signal("buy", "g1", symbol="GBPUSD", price=1.3, sl=1.297))
        self.assertTrue(row["reason"].startswith("SL_MISSING_ON_SERVER"), row["reason"])
        self.driver.positions[0]["sl"] = 1.08045
        self.poll()
        self.now = T0 + timedelta(seconds=10)
        row = self.submit(self.signal("buy", "g2", symbol="GBPUSD", price=1.3, sl=1.297))
        self.assertNotIn("SL_MISSING_ON_SERVER", row["reason"] or "")

    def test_widened_stop_loss_rebooks_the_risk(self):
        self.gui()
        self.assertEqual(self.buy("b1")["status"], "done")
        before = self.ledger()[0]["risk_usd"]
        self.driver.positions[0]["sl"] = 1.07745               # 60 pips instead of 30
        self.poll()
        led = self.ledger()[0]
        self.assertAlmostEqual(led["sl"], 1.07745)
        self.assertAlmostEqual(led["risk_usd"], before * 2, places=4)
        self.assertTrue(self.sent("critical", "stop-loss widened"))

    def test_uncertain_entry_is_adopted_when_it_appears_later(self):
        self.gui(order_outcome="vanish", hidden_tickets=["52390671"])
        row = self.buy("u1")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.ledger(), [])                     # not visible yet
        self.driver.hidden_tickets = set()
        self.poll()
        led = self.ledger()
        self.assertEqual([(r["signal_id"], r["ticket"]) for r in led], [("buy:u1", "52390671")])
        self.assertTrue(self.events("adopted_position"))


# ---------------------------------------------------------------------------------- watchdog & housekeeping


class WatchdogTests(Base):
    def test_stuck_executor_is_reported_and_restarted(self):
        self.start()
        stalls = []
        self.engine.on_stall = stalls.append
        self.engine.stall_default_s = 0.3
        self.executor.read_block = threading.Event()
        self.engine.request_account_poll()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not stalls:
            time.sleep(0.02)
        self.assertEqual(len(stalls), 1, "watchdog did not fire")
        self.assertIn("ACCOUNT", stalls[0])
        self.assertTrue(self.sent("critical", "EXECUTOR STALLED"))
        hb = read_heartbeat(self.cfg.heartbeat_path)
        self.assertTrue(hb["executor_stalled"])
        self.assertFalse(engine_alive(hb))
        self.executor.read_block.set()


class HousekeepingTests(Base):
    def test_screenshot_folders_are_capped_oldest_first(self):
        self.build()
        shots = self.cfg.shots_dir
        for day in ("20261001", "20261005", "20261006"):
            (shots / day).mkdir(parents=True)
            (shots / day / "x.png").write_bytes(b"x" * 200)
        (shots / "calibration").mkdir()
        self.engine.shots_max_bytes = 450
        self.engine._cleanup()
        self.assertEqual(sorted(p.name for p in shots.iterdir()), ["20261005", "20261006", "calibration"])

    def test_low_disk_space_is_critical_once_an_hour(self):
        self.build()
        self.engine.disk_min_free_bytes = 10 ** 18
        self.engine._check_disk(1000.0)
        self.engine._check_disk(1500.0)
        self.assertEqual(len(self.sent("critical", "DISK ALMOST FULL")), 1)

    def test_listener_bind_failure_starts_no_threads(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        cfg = self.make_cfg(server={"port": blocker.getsockname()[1]})
        self.store = Store(cfg.db_path)
        self.cfg = cfg
        self.notifier = NullNotifier()
        engine = Engine(cfg, self.store, ScriptedExecutor(), self.notifier, start_server=True)
        with self.assertRaises(OSError):
            engine.start()
        self.assertEqual(engine._threads, [])
        self.assertFalse(engine.running)

    def test_config_warnings(self):
        self.assertTrue(any("block_untracked_positions" in w
                            for w in config_warnings(self.make_cfg(risk={"block_untracked_positions": False}))))
        self.assertTrue(any("account_login" in w for w in config_warnings(self.make_cfg(executor={"mode": "live"}))))
        self.assertEqual(config_warnings(self.make_cfg()), [])


if __name__ == "__main__":
    unittest.main()
