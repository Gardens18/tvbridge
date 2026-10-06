import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tvbridge import clock
from tvbridge.models import AccountSnapshot, ObservedPosition, Signal
from tvbridge.store import Store

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)


def make_signal(sid="buy:1", action="buy", fired=T0, received=None, symbol="EURUSD.h"):
    return Signal(
        id=sid, action=action, tv_symbol="EURUSD" if symbol else "", symbol=symbol, side=None,
        price=1.085, sl=1.081, tp=None, risk_pct=None, quote_usd=None,
        fired_at=fired, received_at=received or fired + timedelta(seconds=1),
        strategy="strat", comment="tvb", raw={"action": action, "symbol": "OANDA:EURUSD"},
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        clock.set_clock(lambda: T0)
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()
        clock.set_clock(None)


class SignalStoreTests(StoreTestCase):
    def test_insert_and_round_trip(self):
        sig = make_signal()
        self.assertTrue(self.store.insert_signal(sig))
        row = self.store.get_signal_row(sig.id)
        self.assertEqual(row["id"], sig.id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["action"], "buy")
        self.assertEqual(row["symbol"], "EURUSD.h")
        self.assertEqual(row["reason"], "")
        self.assertIsNone(row["result"])
        self.assertIsInstance(row["payload"], dict)
        self.assertEqual(row["payload"]["raw"]["symbol"], "OANDA:EURUSD")
        self.assertEqual(clock.from_iso(row["fired_at"]), sig.fired_at)
        self.assertEqual(self.store.load_signal(sig.id), sig)

    def test_duplicate_insert_returns_false(self):
        sig = make_signal()
        self.assertTrue(self.store.insert_signal(sig))
        self.assertFalse(self.store.insert_signal(sig))
        self.assertFalse(self.store.insert_signal(make_signal(), status="done"))
        self.assertEqual(len(self.store.recent_signals()), 1)
        self.assertEqual(self.store.get_signal_row(sig.id)["status"], "queued")

    def test_insert_with_status(self):
        self.store.insert_signal(make_signal("x"), status="expired")
        self.assertEqual(self.store.get_signal_row("x")["status"], "expired")

    def test_missing_signal(self):
        self.assertIsNone(self.store.get_signal_row("nope"))
        self.assertIsNone(self.store.load_signal("nope"))

    def test_set_status_and_result(self):
        sig = make_signal()
        self.store.insert_signal(sig)
        self.store.set_signal_status(sig.id, "processing")
        self.assertEqual(self.store.get_signal_row(sig.id)["status"], "processing")
        self.store.set_signal_status(sig.id, "done", "", {"status": "filled", "fill_price": 1.0851})
        row = self.store.get_signal_row(sig.id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"], {"status": "filled", "fill_price": 1.0851})
        # result=None keeps the stored result
        self.store.set_signal_status(sig.id, "failed", "UNCERTAIN: check MT5")
        row = self.store.get_signal_row(sig.id)
        self.assertEqual((row["status"], row["reason"]), ("failed", "UNCERTAIN: check MT5"))
        self.assertEqual(row["result"]["status"], "filled")

    def test_signals_with_status_oldest_first(self):
        for i in range(4):
            self.store.insert_signal(make_signal("s%d" % i, received=T0 + timedelta(seconds=10 - i)))
        self.store.set_signal_status("s1", "processing")
        self.store.set_signal_status("s3", "done")
        rows = self.store.signals_with_status(["queued"])
        self.assertEqual([r["id"] for r in rows], ["s2", "s0"])
        rows = self.store.signals_with_status(("queued", "processing"))
        self.assertEqual([r["id"] for r in rows], ["s2", "s1", "s0"])
        self.assertIsInstance(rows[0]["payload"], dict)
        self.assertEqual(self.store.signals_with_status([]), [])
        self.assertEqual([r["id"] for r in self.store.signals_with_status("done")], ["s3"])

    def test_recent_signals_newest_first(self):
        for i in range(5):
            self.store.insert_signal(make_signal("s%d" % i, received=T0 + timedelta(seconds=i)))
        self.assertEqual([r["id"] for r in self.store.recent_signals(3)], ["s4", "s3", "s2"])
        self.assertEqual(len(self.store.recent_signals()), 5)


class LedgerTests(StoreTestCase):
    def add(self, symbol="EURUSD.h", side="buy", opened=T0, **kw):
        return self.store.add_position(kw.get("signal_id", "sig"), symbol, side, kw.get("lots", 0.5), 1.085,
                                       1.081, None, kw.get("risk", 230.0), opened, ticket=kw.get("ticket"))

    def test_add_and_list_open(self):
        p1 = self.add(ticket=123)
        p2 = self.add(symbol="XAUUSD.h", side="sell")
        self.assertNotEqual(p1, p2)
        rows = self.store.open_ledger_positions()
        self.assertEqual([r["pid"] for r in rows], [p1, p2])
        r = rows[0]
        self.assertEqual((r["symbol"], r["side"], r["lots"], r["entry_price"], r["sl"], r["risk_usd"], r["status"]),
                         ("EURUSD.h", "buy", 0.5, 1.085, 1.081, 230.0, "open"))
        self.assertIsNone(r["tp"])
        self.assertEqual(r["ticket"], "123")
        self.assertEqual(clock.from_iso(r["opened_at"]), T0)

    def test_close_ledger_positions_counts(self):
        self.add(side="buy")
        self.add(side="buy")
        self.add(side="sell")
        self.add(symbol="XAUUSD.h", side="buy")
        self.assertEqual(self.store.close_ledger_positions("EURUSD.h", "buy", "signal_close", T0), 2)
        self.assertEqual(self.store.close_ledger_positions("EURUSD.h", "buy", "signal_close", T0), 0)
        self.assertEqual(len(self.store.open_ledger_positions()), 2)
        self.assertEqual(self.store.close_ledger_positions("eurusd.H", None, "x", T0), 1)  # case-insensitive
        self.assertEqual([r["symbol"] for r in self.store.open_ledger_positions()], ["XAUUSD.h"])

    def test_close_ledger_positions_all_symbols(self):
        self.add()
        self.add(symbol="XAUUSD.h", side="sell")
        self.assertEqual(self.store.close_ledger_positions(None, None, "flatten", T0), 2)
        self.assertEqual(self.store.open_ledger_positions(), [])

    def test_close_single_position(self):
        p1 = self.add()
        p2 = self.add()
        self.store.close_ledger_position(p1, "closed_on_server", T0 + timedelta(minutes=5))
        rows = self.store.open_ledger_positions()
        self.assertEqual([r["pid"] for r in rows], [p2])
        closed = [r for r in self.store.all_ledger_positions() if r["pid"] == p1][0]
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["close_reason"], "closed_on_server")
        self.assertEqual(clock.from_iso(closed["closed_at"]), T0 + timedelta(minutes=5))

    def test_count_trades_on_server_day_gmt3(self):
        # server day 2026-10-01 at GMT+3 spans [2026-09-30T21:00Z, 2026-10-01T21:00Z)
        self.add(opened=datetime(2026, 9, 30, 20, 59, 59, 999000, tzinfo=UTC))  # 09-30 23:59:59.999 server
        self.add(opened=datetime(2026, 9, 30, 21, 0, tzinfo=UTC))                # 10-01 00:00 server
        self.add(opened=datetime(2026, 10, 1, 20, 59, tzinfo=UTC))               # 10-01 23:59 server
        self.add(opened=datetime(2026, 10, 1, 21, 0, tzinfo=UTC))                # 10-02 00:00 server
        self.add(opened=datetime(2026, 10, 1, 21, 0, 0, 1, tzinfo=UTC))           # 10-02
        self.assertEqual(self.store.count_trades_on_server_day(date(2026, 9, 30), 3), 1)
        self.assertEqual(self.store.count_trades_on_server_day(date(2026, 10, 1), 3), 2)
        self.assertEqual(self.store.count_trades_on_server_day(date(2026, 10, 2), 3.0), 2)
        self.assertEqual(self.store.count_trades_on_server_day(date(2026, 10, 3), 3), 0)
        # UTC day boundaries differ
        self.assertEqual(self.store.count_trades_on_server_day(date(2026, 10, 1), 0), 3)

    def test_count_includes_closed_positions(self):
        pid = self.add(opened=T0)
        self.store.close_ledger_position(pid, "x", T0)
        self.assertEqual(self.store.count_trades_on_server_day(clock.server_date(T0, 3), 3), 1)

    def test_last_trade_at(self):
        self.assertIsNone(self.store.last_trade_at())
        self.add(opened=T0 + timedelta(hours=1))
        self.add(opened=T0)
        self.assertEqual(self.store.last_trade_at(), T0 + timedelta(hours=1))

    def test_opened_at_accepts_iso_string(self):
        self.add(opened="2026-10-01T09:56:00Z")
        self.assertEqual(self.store.last_trade_at(), T0)


class SnapshotTests(StoreTestCase):
    def snap(self, ts, bal=50000.0, eq=50000.0, positions=None):
        return AccountSnapshot(ts=ts, balance=bal, equity=eq, margin=10.0, free_margin=49990.0,
                               positions=positions, source="paper")

    def test_has_any_and_latest(self):
        self.assertFalse(self.store.has_any_snapshot())
        self.assertIsNone(self.store.latest_snapshot())
        self.store.add_snapshot(self.snap(T0, eq=49900.0, positions=[ObservedPosition("EURUSD.h", "buy", 0.5)]))
        self.store.add_snapshot(self.snap(T0 - timedelta(minutes=1), eq=1.0))
        self.assertTrue(self.store.has_any_snapshot())
        latest = self.store.latest_snapshot()
        self.assertEqual(latest.ts, T0)
        self.assertEqual(latest.equity, 49900.0)
        self.assertEqual((latest.margin, latest.free_margin, latest.source), (10.0, 49990.0, "paper"))
        self.assertIsNone(latest.positions)

    def test_snapshots_between_inclusive_and_ordered(self):
        times = [T0 + timedelta(seconds=15 * i) for i in range(6)]
        for t in reversed(times):
            self.store.add_snapshot(self.snap(t, eq=float(t.second)))
        got = self.store.snapshots_between(times[1], times[4])
        self.assertEqual([s.ts for s in got], times[1:5])
        self.assertEqual(self.store.snapshots_between(times[5] + timedelta(seconds=1), times[5] + timedelta(hours=1)),
                         [])

    def test_snapshots_between_subsecond_ordering(self):
        a = T0 + timedelta(microseconds=500000)
        b = T0 + timedelta(seconds=1)
        self.store.add_snapshot(self.snap(b))
        self.store.add_snapshot(self.snap(a))
        self.store.add_snapshot(self.snap(T0))
        self.assertEqual([s.ts for s in self.store.snapshots_between(T0, b)], [T0, a, b])
        self.assertEqual(self.store.latest_snapshot().ts, b)

    def test_last_snapshot_before_midnight(self):
        midnight = clock.server_midnight_utc(date(2026, 10, 2), 3)
        self.store.add_snapshot(self.snap(midnight - timedelta(seconds=10), bal=1.0))
        self.store.add_snapshot(self.snap(midnight, bal=2.0))
        self.assertEqual(self.store.last_snapshot_before(midnight).balance, 1.0)
        self.assertIsNone(self.store.last_snapshot_before(midnight - timedelta(hours=1)))

    def test_n_positions_column(self):
        self.store.add_snapshot(self.snap(T0, positions=[ObservedPosition("EURUSD.h", "buy", 0.5)]))
        self.store.add_snapshot(self.snap(T0 + timedelta(seconds=1)))
        rows = self.store._all("SELECT n_positions FROM snapshots ORDER BY id")
        self.assertEqual([r["n_positions"] for r in rows], [1, None])


class DayStateTests(StoreTestCase):
    def test_get_missing(self):
        self.assertIsNone(self.store.get_day_state(date(2026, 10, 1)))

    def test_reference_is_max_and_upsert(self):
        d = date(2026, 10, 1)
        self.store.set_day_state(d, 50000.0, 50250.0, "rollover")
        st = self.store.get_day_state(d)
        self.assertEqual(st["server_date"], "2026-10-01")
        self.assertEqual((st["ref_balance"], st["ref_equity"], st["reference"], st["source"]),
                         (50000.0, 50250.0, 50250.0, "rollover"))
        self.store.set_day_state(d, 51000.0, 50500.0, "manual")
        st = self.store.get_day_state(d)
        self.assertEqual((st["reference"], st["source"]), (51000.0, "manual"))
        self.assertEqual(self.store._one("SELECT COUNT(*) AS n FROM day_state")["n"], 1)
        self.assertIsNone(self.store.get_day_state(date(2026, 10, 2)))


class EventKvTests(StoreTestCase):
    def test_events(self):
        self.store.log_event("info", "startup", "hello")
        self.store.log_event("warn", "ip_blocked", "blocked 1.2.3.4", {"ip": "1.2.3.4", "when": T0})
        ev = self.store.recent_events()
        self.assertEqual([e["kind"] for e in ev], ["ip_blocked", "startup"])
        self.assertEqual(ev[0]["data"]["ip"], "1.2.3.4")
        self.assertIsNone(ev[1]["data"])
        self.assertEqual(ev[0]["level"], "warn")
        self.assertEqual(len(self.store.recent_events(limit=1)), 1)

    def test_log_event_never_raises(self):
        self.store.close()
        with self.assertLogs("tvbridge.store", level="ERROR"):
            self.store.log_event("info", "x", "after close")

    def test_kv(self):
        self.assertIsNone(self.store.get_kv("paused"))
        self.assertEqual(self.store.get_kv("paused", "0"), "0")
        self.store.set_kv("paused", "1")
        self.assertEqual(self.store.get_kv("paused"), "1")
        self.store.set_kv("paused", "2")
        self.assertEqual(self.store.get_kv("paused"), "2")
        self.store.set_kv("paused", None)
        self.assertIsNone(self.store.get_kv("paused"))
        self.assertEqual(self.store.get_kv("paused", "dflt"), "dflt")
        self.store.set_kv("missing", None)  # deleting a missing key is fine
        self.store.set_kv("command", '{"cmd": "flatten"}')
        self.assertEqual(self.store.get_kv("command"), '{"cmd": "flatten"}')


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "tvbridge.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_persistence_and_wal(self):
        s = Store(self.path)
        mode = s._one("PRAGMA journal_mode")
        self.assertEqual(list(mode.values())[0].lower(), "wal")
        self.assertEqual(list(s._one("PRAGMA busy_timeout").values())[0], 5000)
        s.insert_signal(make_signal())
        s.set_kv("halted", "UNCERTAIN_EXECUTION test")
        s.close()
        s.close()  # idempotent
        s2 = Store(str(self.path))
        self.assertEqual(s2.get_kv("halted"), "UNCERTAIN_EXECUTION test")
        self.assertEqual(s2.load_signal("buy:1"), make_signal())
        s2.close()

    def test_concurrent_writes(self):
        s = Store(self.path)
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    s.insert_signal(make_signal("t%d-%d" % (n, i)))
                    s.set_kv("k%d" % n, str(i))
                    s.add_snapshot(AccountSnapshot(ts=T0 + timedelta(seconds=i), balance=1.0, equity=1.0))
                    s.log_event("debug", "t", "m")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(s.recent_signals(limit=1000)), 100)
        self.assertEqual(len(s.snapshots_between(T0, T0 + timedelta(minutes=1))), 100)
        s.close()

    def test_schema_tables(self):
        s = Store(self.path)
        s.close()
        conn = sqlite3.connect(str(self.path))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertTrue({"signals", "positions", "snapshots", "day_state", "events", "kv"} <= names)


if __name__ == "__main__":
    unittest.main()
