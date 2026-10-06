"""End-to-end engine tests.

Paper mode with a real WebhookServer on 127.0.0.1 (port 0), a temp TVBRIDGE_HOME with a
file-backed Store, a NullNotifier and a frozen clock at Tuesday 2026-10-06 10:00 server time
(07:00 UTC, server UTC+3), inside the trading window. The uncertain-execution tests drive a
Mt5GuiExecutor against the simulated MetaTrader in tests/fakes.py (never the real desktop).
"""

import http.client
import json
import logging
import shutil
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone

from tests.fakes import FakeMt5Driver, make_calibration
from tvbridge import clock
from tvbridge.config import config_from_dict, deep_merge
from tvbridge.engine import (ACCOUNT, CLOSE, FLATTEN, OPEN, PRIORITIES, Engine, Task, build_status,
                             config_warnings, read_heartbeat)
from tvbridge.executors.mt5gui import Mt5GuiExecutor
from tvbridge.executors.paper import PaperExecutor
from tvbridge.models import AccountSnapshot, OrderRequest, Signal
from tvbridge.notify import NullNotifier
from tvbridge.store import Store

SECRET = "engine-test-secret-0123456789abcdef"
T0 = datetime(2026, 10, 6, 7, 0, 0, tzinfo=timezone.utc)   # Tue 10:00 server time (UTC+3)
DAY = date(2026, 10, 6)

_QUIET = logging.NullHandler()


def setUpModule() -> None:
    # Keep expected warnings/errors (kill switch, uncertain execution) out of the test output.
    logging.getLogger("tvbridge").addHandler(_QUIET)


def tearDownModule() -> None:
    logging.getLogger("tvbridge").removeHandler(_QUIET)


class EngineTestBase(unittest.TestCase):
    """Builds a paper engine around a temp home; helpers post alerts over HTTP."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tvb-engine-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = T0
        clock.set_clock(lambda: self.now)
        self.addCleanup(clock.set_clock, None)
        self.engine = None  # type: Engine
        self.store = None  # type: Store

    def tearDown(self) -> None:
        if self.engine is not None:
            self.engine.stop(timeout=5)
        if self.store is not None:
            self.store.close()

    # ------------------------------------------------------------------ building

    def make_cfg(self, **sections):
        base = {
            "server": {"secret": SECRET, "port": 0},
            "notify": {"macos": False},
            "executor": {"mode": "paper", "gui": {"account_poll_s": 3600}},
        }
        return config_from_dict(deep_merge(base, sections), self.tmp)

    def build(self, cfg=None, executor=None):
        self.cfg = cfg or self.make_cfg()
        self.store = Store(self.cfg.db_path)
        self.executor = executor if executor is not None else PaperExecutor(self.cfg, self.store)
        self.notifier = NullNotifier()
        self.engine = Engine(self.cfg, self.store, self.executor, self.notifier, start_server=True)
        self.engine.tick_s = 0.02
        return self.engine

    def start(self, cfg=None, executor=None):
        self.build(cfg, executor)
        self.engine.start()
        return self.engine

    # ------------------------------------------------------------------ helpers

    def post(self, payload, with_secret=True):
        d = dict(payload)
        d.setdefault("time", clock.iso(self.now))
        if with_secret:
            d["secret"] = SECRET
        body = json.dumps(d).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.engine.server.port, timeout=10)
        try:
            conn.request("POST", self.cfg.server.path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def send(self, payload):
        status, data = self.post(payload)
        self.assertTrue(self.engine.wait_idle(15), "engine did not become idle")
        return status, data

    def buy(self, sig_id="b1", symbol="EURUSD", price=1.10000, sl=1.09800, tp=1.10500, **extra):
        payload = {"action": "buy", "symbol": symbol, "price": price, "sl": sl, "id": sig_id}
        if tp is not None:
            payload["tp"] = tp
        payload.update(extra)
        return self.send(payload)

    def row(self, sig_id):
        return self.store.get_signal_row(sig_id)

    def ledger(self):
        return self.store.open_ledger_positions()

    def all_ledger(self):
        return self.store.all_ledger_positions()

    def paper_positions(self):
        return self.executor.read_account().positions

    def events(self, kind):
        return [e for e in self.store.recent_events(500) if e["kind"] == kind]

    def notes(self, level=None):
        return self.notifier.messages(level)

    def wait_for(self, pred, timeout=5.0, msg="condition not reached"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        self.fail(msg)


# ---------------------------------------------------------------------------------- startup


class StartupTests(EngineTestBase):
    def test_startup_rollover_creates_day_state_from_initial_balance(self):
        self.start()
        ds = self.store.get_day_state(DAY)
        self.assertIsNotNone(ds)
        self.assertAlmostEqual(ds["reference"], 50000.0)
        self.assertEqual(ds["source"], "startup")
        self.assertEqual(self.store.get_kv("day"), "2026-10-06")
        snap = self.store.latest_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual((snap.balance, snap.equity, snap.source), (50000.0, 50000.0, "paper"))
        self.assertTrue(self.events("day_rollover"))
        self.assertTrue(any("daily reference 50,000.00" in m for m in self.notes("info")))
        self.assertTrue(any("engine started" in m for m in self.notes()))

    def test_first_day_reference_keeps_initial_balance_when_account_is_down(self):
        # fresh install, account already below the initial balance: the startup read happens
        # before the rollover, but today's snapshot is no "history" -> initial balance still counts
        self.start(self.make_cfg(executor={"paper_start_balance": 49000}))
        self.assertAlmostEqual(self.store.get_day_state(DAY)["reference"], 50000.0)

    def test_reference_with_history_excludes_initial_balance(self):
        self.build(self.make_cfg(executor={"paper_start_balance": 49000}))
        yesterday = T0 - timedelta(hours=12)        # 2026-10-05 19:00 UTC, before server midnight
        self.store.add_snapshot(AccountSnapshot(ts=yesterday, balance=49000.0, equity=48900.0, source="paper"))
        self.engine.start()
        ds = self.store.get_day_state(DAY)
        self.assertAlmostEqual(ds["reference"], 49000.0)
        self.assertEqual(ds["source"], "startup")

    def test_existing_reference_is_kept(self):
        self.build()
        self.store.set_day_state(DAY, 50500.0, 50500.0, "manual")  # `tvbridge set-reference` before start
        self.engine.start()
        ds = self.store.get_day_state(DAY)
        self.assertEqual((ds["reference"], ds["source"]), (50500.0, "manual"))
        self.assertEqual(self.store.get_kv("day"), "2026-10-06")

    def test_rollover_at_server_midnight_uses_highest_candidate(self):
        self.start()
        # a later snapshot with higher equity, still before server midnight (21:00 UTC)
        self.store.add_snapshot(AccountSnapshot(ts=T0 + timedelta(hours=10), balance=50100.0, equity=50300.0,
                                                source="paper"))
        self.now = datetime(2026, 10, 6, 21, 0, 30, tzinfo=timezone.utc)   # Wed 00:00:30 server time
        self.wait_for(lambda: self.store.get_day_state(date(2026, 10, 7)) is not None, msg="no rollover")
        ds = self.store.get_day_state(date(2026, 10, 7))
        self.assertEqual(ds["source"], "rollover")
        self.assertAlmostEqual(ds["reference"], 50300.0)
        self.assertEqual(self.store.get_kv("day"), "2026-10-07")

    def test_heartbeat_and_status(self):
        self.start()
        self.wait_for(lambda: read_heartbeat(self.cfg.heartbeat_path) is not None)
        hb = read_heartbeat(self.cfg.heartbeat_path)
        self.assertEqual(hb["mode"], "paper")
        self.assertEqual(hb["port"], self.engine.server.port)
        st = self.engine.status()
        for key in ("mode", "paused", "halted", "snapshot", "floors", "open_positions", "untracked",
                    "queue_size", "trades_today"):
            self.assertIn(key, st)
        self.assertEqual(st["mode"], "paper")
        self.assertAlmostEqual(st["floors"]["kill_floor"], 48150.0)
        self.assertAlmostEqual(st["floors"]["entry_floor"], 48500.0)
        self.assertEqual(st["snapshot"]["age_s"], 0.0)
        self.assertEqual(st["snapshot"]["positions"], [])
        self.assertTrue(st["engine"]["running"])
        self.engine.stop()
        self.assertTrue(read_heartbeat(self.cfg.heartbeat_path)["stopped"])

    def test_priorities(self):
        self.assertLess(PRIORITIES[FLATTEN], PRIORITIES[CLOSE])
        self.assertLess(PRIORITIES[CLOSE], PRIORITIES[OPEN])
        self.assertLess(PRIORITIES[OPEN], PRIORITIES[ACCOUNT])

    def test_config_warning_when_kill_buffer_not_below_entry_buffers(self):
        self.assertEqual(config_warnings(self.make_cfg()), [])
        self.assertTrue(config_warnings(self.make_cfg(risk={"daily_buffer_pct": 0.2})))


# ---------------------------------------------------------------------------------- entries & closes


class SignalFlowTests(EngineTestBase):
    def test_buy_over_http_is_filled_and_lands_in_ledger_with_risk(self):
        self.start()
        status, data = self.buy("b1")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "id": "buy:b1"})
        row = self.row("buy:b1")
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertTrue(row["result"]["plan"]["approved"])
        led = self.ledger()
        self.assertEqual(len(led), 1)
        pos = led[0]
        self.assertEqual((pos["symbol"], pos["side"], pos["ticket"], pos["signal_id"]),
                         ("EURUSD.h", "buy", "P1", "buy:b1"))
        self.assertAlmostEqual(pos["lots"], 1.06)
        self.assertAlmostEqual(pos["entry_price"], 1.1)
        self.assertAlmostEqual(pos["sl"], 1.098)
        self.assertAlmostEqual(pos["tp"], 1.105)
        # (0.002 * 100000 + 5) * 1.15 = 235.75 per lot; 250 / 235.75 -> 1.06 lots
        self.assertAlmostEqual(pos["risk_usd"], 1.06 * 235.75, places=6)
        self.assertEqual(len(self.paper_positions()), 1)
        self.assertEqual(self.engine.status()["trades_today"], 1)
        self.assertTrue(any("filled" in m for m in self.notes("info")))

    def test_duplicate_post_is_ignored(self):
        self.start()
        self.buy("dup")
        status, data = self.send({"action": "buy", "symbol": "EURUSD", "price": 1.1, "sl": 1.098, "tp": 1.105,
                                  "id": "dup"})
        self.assertEqual((status, data), (200, {"ok": True, "duplicate": True}))
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(len(self.paper_positions()), 1)

    def test_close_signal_closes_position(self):
        self.start()
        self.buy("b1")
        status, data = self.send({"action": "close", "symbol": "EURUSD", "price": 1.1010, "id": "c1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.row("close:c1")["status"], "done")
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.all_ledger()[0]["close_reason"], "signal_close")
        self.assertEqual(self.paper_positions(), [])
        # +10 pips on 1.06 lots = 106.00, commission 5.30
        self.assertAlmostEqual(self.executor.read_account().balance, 50100.70, places=2)

    def test_close_with_nothing_open_is_no_position(self):
        self.start()
        self.send({"action": "close", "symbol": "EURUSD", "id": "c0"})
        row = self.row("close:c0")
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["reason"].startswith("NO_POSITION"), row["reason"])

    def test_reversal_sell_while_long_closes_then_opens(self):
        self.start()
        self.buy("b1")
        status, _ = self.send({"action": "sell", "symbol": "EURUSD", "price": 1.1000, "sl": 1.1020,
                               "tp": 1.0950, "id": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.row("sell:s1")["status"], "done", self.row("sell:s1")["reason"])
        led = self.ledger()
        self.assertEqual([(p["side"], p["symbol"]) for p in led], [("sell", "EURUSD.h")])
        closed = [p for p in self.all_ledger() if p["status"] == "closed"]
        self.assertEqual([(p["side"], p["close_reason"]) for p in closed], [("buy", "reversal")])
        self.assertEqual([(p.side, p.symbol) for p in self.paper_positions()], [("sell", "EURUSD.h")])
        self.assertEqual(self.engine.status()["trades_today"], 2)

    def test_risk_rejection_is_recorded_with_reason(self):
        self.start()
        self.buy("wrong", sl=1.1020)                       # buy with the stop above the price
        row = self.row("buy:wrong")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("SL_WRONG_SIDE:"), row["reason"])
        self.assertFalse(row["result"]["plan"]["approved"])
        self.send({"action": "buy", "symbol": "EURUSD", "price": 1.1, "id": "nosl"})
        self.assertTrue(self.row("buy:nosl")["reason"].startswith("SL_MISSING:"))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.paper_positions(), [])
        self.assertEqual(len(self.events("entry_rejected")), 2)

    def test_stale_entry_is_expired(self):
        self.start()
        self.send({"action": "buy", "symbol": "EURUSD", "price": 1.1, "sl": 1.098, "id": "old",
                   "time": clock.iso(self.now - timedelta(seconds=60))})   # fresh for the server, not for risk
        row = self.row("buy:old")
        self.assertEqual(row["status"], "expired")
        self.assertTrue(row["reason"].startswith("STALE_SIGNAL"))

    def test_close_all_signal_closes_every_symbol(self):
        self.start()
        self.buy("b1")
        self.buy("b2", symbol="GBPUSD", price=1.30000, sl=1.29700, tp=None)
        self.assertEqual(len(self.ledger()), 2)
        self.send({"action": "close_all", "id": "all"})
        self.assertEqual(self.row("close_all:all")["status"], "done")
        self.assertEqual(self.ledger(), [])
        self.assertEqual({p["close_reason"] for p in self.all_ledger()}, {"close_all"})
        self.assertEqual(self.paper_positions(), [])

    def test_min_hold_defers_a_close(self):
        self.start(self.make_cfg(risk={"min_hold_s_for_signal_close": 60}))
        self.buy("b1")
        self.send({"action": "close", "symbol": "EURUSD", "price": 1.1005, "id": "early"})
        row = self.row("close:early")
        self.assertEqual(row["status"], "queued")
        self.assertTrue(row["reason"].startswith("DEFERRED"))
        self.assertEqual(len(self.ledger()), 1)
        self.now = T0 + timedelta(seconds=61)
        self.wait_for(lambda: self.row("close:early")["status"] == "done", msg="deferred close never ran")
        self.assertEqual(self.ledger(), [])

    def test_paused_blocks_entries_but_allows_closes(self):
        self.start()
        self.buy("b1")
        self.store.set_kv("paused", "1")
        self.buy("b2", symbol="GBPUSD", price=1.30000, sl=1.29700, tp=None)
        row = self.row("buy:b2")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("PAUSED"), row["reason"])
        self.send({"action": "close", "symbol": "EURUSD", "price": 1.1002, "id": "c1"})
        self.assertEqual(self.row("close:c1")["status"], "done")
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.paper_positions(), [])


# ---------------------------------------------------------------------------------- safety


class SafetyTests(EngineTestBase):
    def test_kill_switch_price_hint_drives_equity_below_kill_floor(self):
        self.start()
        self.buy("b1")                                                   # 1.06 lots at 1.10000
        # like `tvbridge set-reference 51900`: kill floor 51900*0.96 + 150 = 49,974.00
        self.store.set_day_state(DAY, 51900.0, 51900.0, "manual")
        self.now = T0 + timedelta(seconds=10)
        # the next alert's price moves the paper market: floating -159.00 -> equity 49,841.00
        self.buy("b2", price=1.09850, sl=1.09600, tp=None)
        halted = self.store.get_kv("halted") or ""
        self.assertTrue(halted.startswith("KILL:"), halted)
        row = self.row("buy:b2")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("HALTED"), row["reason"])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.all_ledger()[0]["close_reason"], "flatten")
        self.assertEqual(self.paper_positions(), [])
        self.assertAlmostEqual(self.executor.read_account().balance, 50000 - 159.0 - 5.30, places=2)
        self.assertTrue(self.events("kill_switch"))
        self.assertTrue(any("KILL" in m for m in self.notes("critical")))
        # entries stay blocked until a human resumes
        self.now = T0 + timedelta(seconds=20)
        self.buy("b3", symbol="GBPUSD", price=1.3, sl=1.297, tp=None)
        self.assertTrue(self.row("buy:b3")["reason"].startswith("HALTED"))

    def test_flatten_command_from_kv_is_consumed(self):
        self.start()
        self.buy("b1")
        self.buy("b2", symbol="GBPUSD", price=1.30000, sl=1.29700, tp=None)
        self.store.set_kv("command", json.dumps({"cmd": "flatten", "ts": clock.iso(self.now), "by": "test"}))
        self.wait_for(lambda: self.store.get_kv("command") is None and not self.ledger(), msg="flatten not run")
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.paper_positions(), [])
        self.assertEqual({p["close_reason"] for p in self.all_ledger()}, {"flatten"})
        self.assertTrue(self.events("command"))
        self.assertIsNone(self.store.get_kv("halted"))   # a manual flatten does not halt entries

    def test_old_flatten_command_is_discarded(self):
        self.start()
        self.buy("b1")
        old = clock.iso(self.now - timedelta(hours=2))
        self.store.set_kv("command", json.dumps({"cmd": "flatten", "ts": old}))
        self.wait_for(lambda: self.store.get_kv("command") is None)
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(len(self.ledger()), 1)
        self.assertTrue(self.events("command_discarded"))

    def test_startup_recovery_fails_processing_signal_and_halts(self):
        self.build()

        def sig(action, sig_id, **kw):
            d = dict(id=sig_id, action=action, tv_symbol="EURUSD", symbol="EURUSD.h", side=None, price=1.1,
                     sl=None, tp=None, risk_pct=None, quote_usd=None, fired_at=self.now, received_at=self.now)
            d.update(kw)
            return Signal(**d)

        self.store.insert_signal(sig("buy", "buy:stuck", sl=1.098), status="processing")
        self.store.insert_signal(sig("buy", "buy:waiting", sl=1.098))           # queued entry
        self.store.insert_signal(sig("close", "close:waiting", tv_symbol="GBPUSD", symbol="GBPUSD.h"))
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(10))
        stuck = self.row("buy:stuck")
        self.assertEqual(stuck["status"], "failed")
        self.assertTrue(stuck["reason"].startswith("INTERRUPTED"), stuck["reason"])
        self.assertTrue((self.store.get_kv("halted") or "").startswith("INTERRUPTED"))
        self.assertTrue(any("INTERRUPTED" in t or "interrupted" in m.lower()
                            for t, m, lv in self.notifier.sent if lv == "critical"))
        # the queued entry is re-submitted and refused (halted); the queued close still runs
        self.assertEqual(self.row("buy:waiting")["status"], "rejected")
        self.assertTrue(self.row("buy:waiting")["reason"].startswith("HALTED"))
        self.assertEqual(self.row("close:waiting")["status"], "done")
        self.assertEqual(self.paper_positions(), [])

    def test_untracked_position_blocks_entries(self):
        self.build()
        # a position opened behind tvbridge's back (not in the ledger)
        self.executor.open_market(OrderRequest(symbol="EURUSD.h", side="buy", lots=0.10, sl=1.09, tp=None,
                                               digits=5, price_hint=1.1))
        self.engine.start()
        self.buy("b1", symbol="GBPUSD", price=1.30000, sl=1.29700, tp=None)
        row = self.row("buy:b1")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("UNTRACKED_POSITIONS"), row["reason"])
        self.assertTrue(self.events("untracked_position"))
        self.assertEqual(len(self.engine.status()["untracked"]), 1)

    def test_position_closed_on_server_needs_two_reads(self):
        self.start()
        self.buy("b1")
        self.executor.set_price_hint("EURUSD.h", 1.09790)   # stop-loss hit in the paper market
        self.assertTrue(self.engine.request_account_poll())
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(len(self.ledger()), 1)              # one miss: not yet
        self.engine.request_account_poll()
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.all_ledger()[0]["close_reason"], "closed_on_server")

    def test_internal_signal_without_store_row_is_inserted(self):
        self.start(self.make_cfg())
        sig = Signal(id="buy:direct", action="buy", tv_symbol="EURUSD", symbol="EURUSD.h", side=None,
                     price=1.1, sl=1.098, tp=None, risk_pct=None, quote_usd=None, fired_at=self.now,
                     received_at=self.now)
        self.engine.submit_signal(sig)
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.row("buy:direct")["status"], "done")


# ---------------------------------------------------------------------------------- GUI executor


class UncertainExecutionTests(EngineTestBase):
    """Mt5GuiExecutor + FakeMt5Driver: an uncertain fill halts entries and is never retried."""

    def gui_engine(self, risk=None, **driver_kw):
        cfg = self.make_cfg(executor={"mode": "live"}, **({"risk": risk} if risk else {}))
        self.driver = FakeMt5Driver(**driver_kw)
        ex = Mt5GuiExecutor(cfg, self.driver, make_calibration(), rehearsal=False, shots_dir=cfg.shots_dir)
        return self.start(cfg, ex)

    def post_buy(self, sig_id):
        return self.send({"action": "buy", "symbol": "EURUSD", "price": 1.08345, "sl": 1.08045, "tp": 1.08945,
                          "id": sig_id})

    def test_verified_fill_resumes_entries_when_enabled(self):
        self.gui_engine(risk={"resume_after_verified_fill": True}, raise_after_click="click", result_delay_s=0.0)
        self.post_buy("v1")
        self.assertIn("UNCERTAIN_EXECUTION", self.row("buy:v1")["reason"])
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.store.get_kv("halted") or "", "")
        self.assertTrue(self.events("adopted_position"))

    def test_exception_after_click_halts_adopts_and_never_retries(self):
        self.gui_engine(raise_after_click="click", result_delay_s=0.0)
        self.assertEqual(self.store.latest_snapshot().source, "mt5gui")
        self.post_buy("u1")
        row = self.row("buy:u1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("UNCERTAIN_EXECUTION", row["reason"])
        halted = self.store.get_kv("halted") or ""
        self.assertTrue(halted.startswith("UNCERTAIN_EXECUTION"), halted)
        self.assertEqual(self.driver.order_button_clicks, ["buy"])
        self.assertEqual(len(self.driver.orders_sent), 1)
        # the position that did open was adopted into the ledger with the planned risk
        led = self.ledger()
        self.assertEqual(len(led), 1)
        self.assertEqual((led[0]["symbol"], led[0]["side"], led[0]["signal_id"]), ("EURUSD.h", "buy", "buy:u1"))
        plan = row["result"]["plan"]
        self.assertAlmostEqual(led[0]["risk_usd"], plan["risk_usd"], places=1)
        self.assertEqual(led[0]["ticket"], "52390671")
        self.assertTrue(self.events("adopted_position"))
        self.assertEqual(self.engine.status()["untracked"], [])
        self.assertTrue(any("CHECK MT5" in t for t, _m, lv in self.notifier.sent if lv == "critical"))

        # no retry: the failed signal is never processed again ...
        self.engine.submit_signal(self.store.load_signal("buy:u1"))
        self.assertTrue(self.engine.wait_idle(10))
        # ... and a new entry is refused while halted
        self.now = T0 + timedelta(seconds=5)
        self.post_buy("u2")
        self.assertTrue(self.row("buy:u2")["reason"].startswith("HALTED"))
        self.assertEqual(len(self.driver.orders_sent), 1)
        self.assertEqual(self.driver.order_button_clicks, ["buy"])

    def test_no_result_after_click_is_uncertain_and_halts(self):
        self.gui_engine(order_outcome="nothing")
        self.post_buy("u1")
        row = self.row("buy:u1")
        self.assertEqual(row["status"], "failed")
        self.assertTrue((self.store.get_kv("halted") or "").startswith("UNCERTAIN_EXECUTION"))
        self.assertEqual(self.ledger(), [])          # nothing appeared in MT5, nothing adopted
        self.assertEqual(len(self.driver.orders_sent), 1)

    def test_gui_fill_lands_in_ledger(self):
        self.gui_engine()
        self.post_buy("ok")
        row = self.row("buy:ok")
        self.assertEqual(row["status"], "done", row["reason"])
        led = self.ledger()
        self.assertEqual(len(led), 1)
        self.assertEqual(led[0]["ticket"], "52390671")
        self.assertAlmostEqual(led[0]["entry_price"], 1.08345)
        self.assertIsNone(self.store.get_kv("halted"))
        # the next account read matches the MT5 row to the ledger (no untracked position)
        self.engine.request_account_poll()
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.engine.status()["untracked"], [])
        self.assertEqual(len(self.ledger()), 1)


class BuildStatusTests(EngineTestBase):
    def test_status_from_database_without_engine(self):
        self.start()
        self.buy("b1")
        self.store.set_kv("paused", "1")
        self.store.set_kv("pause_reason", "news")
        self.engine.stop()
        st = build_status(self.cfg, self.store)
        self.assertEqual(st["mode"], "paper")
        self.assertTrue(st["paused"])
        self.assertEqual(st["pause_reason"], "news")
        self.assertFalse(st["entries_allowed"])
        self.assertEqual(len(st["open_positions"]), 1)
        self.assertAlmostEqual(st["open_risk_usd"], 249.9, places=1)
        self.assertEqual(st["day_reference"]["reference"], 50000.0)
        self.assertEqual(st["server_date"], "2026-10-06")
        self.assertEqual(st["trading_window"], "open")
        self.assertEqual(st["trades_today"], 1)


class TaskTests(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(Task(FLATTEN, reason="kill switch").describe(), "FLATTEN (kill switch)")


if __name__ == "__main__":
    unittest.main()
