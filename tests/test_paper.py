import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tvbridge import clock
from tvbridge.config import config_from_dict, deep_merge
from tvbridge.executors import ExecutorError, make_executor
from tvbridge.executors.paper import STATE_KEY, PaperExecutor
from tvbridge.models import OrderRequest
from tvbridge.store import Store

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)
SECRET = "s" * 24


def make_cfg(home, **sections):
    d = deep_merge({"server": {"secret": SECRET}}, sections)
    return config_from_dict(d, home)


def req(symbol="EURUSD.h", side="buy", lots=1.0, sl=1.08000, tp=1.09500, price=1.08500, digits=5):
    return OrderRequest(symbol=symbol, side=side, lots=lots, sl=sl, tp=tp, digits=digits, lot_decimals=2,
                        comment="tvb", price_hint=price)


class PaperTestCase(unittest.TestCase):
    def setUp(self):
        clock.set_clock(lambda: T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = make_cfg(self.home)
        self.store = Store(":memory:")
        self.ex = PaperExecutor(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        clock.set_clock(None)


class OpenTests(PaperTestCase):
    def test_initial_account(self):
        snap = self.ex.read_account()
        self.assertEqual(snap.balance, 50000.0)
        self.assertEqual(snap.equity, 50000.0)
        self.assertEqual(snap.positions, [])
        self.assertEqual(snap.source, "paper")
        self.assertEqual(snap.ts, T0)

    def test_fills_at_price_hint(self):
        res = self.ex.open_market(req(lots=0.5))
        self.assertEqual(res.status, "filled")
        self.assertEqual(res.fill_price, 1.085)
        self.assertEqual(res.ticket, "P1")
        self.assertEqual(res.lots, 0.5)
        snap = self.ex.read_account()
        self.assertEqual(len(snap.positions), 1)
        pos = snap.positions[0]
        self.assertEqual((pos.symbol, pos.side, pos.lots, pos.ticket), ("EURUSD.h", "buy", 0.5, "P1"))
        self.assertEqual((pos.open_price, pos.sl, pos.tp), (1.085, 1.08, 1.095))
        self.assertEqual(pos.profit, 0.0)
        # no commission until close
        self.assertEqual(snap.balance, 50000.0)
        self.assertEqual(snap.equity, 50000.0)

    def test_tickets_increment(self):
        self.assertEqual(self.ex.open_market(req()).ticket, "P1")
        self.assertEqual(self.ex.open_market(req(symbol="GBPUSD.h", price=1.3, sl=1.29, tp=None)).ticket, "P2")

    def test_error_when_no_price_hint(self):
        res = self.ex.open_market(req(price=None))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("NO_PRICE"))
        self.assertEqual(self.ex.read_account().positions, [])

    def test_bad_request(self):
        self.assertEqual(self.ex.open_market(req(side="close")).status, "error")
        self.assertEqual(self.ex.open_market(req(lots=0)).status, "error")
        res = self.ex.open_market(req(symbol="DOGEUSD.h"))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("NO_SPEC"))

    def test_start_balance_override(self):
        cfg = make_cfg(self.home, executor={"paper_start_balance": 10000.0})
        with Store(":memory:") as st:
            self.assertEqual(PaperExecutor(cfg, st).read_account().balance, 10000.0)

    def test_tv_symbol_accepted(self):
        res = self.ex.open_market(req(symbol="EURUSD"))
        self.assertEqual(res.status, "filled")
        self.assertEqual(self.ex.read_account().positions[0].symbol, "EURUSD.h")


class PriceHintTests(PaperTestCase):
    def test_sl_hit_on_buy_realizes_loss_with_commission(self):
        self.ex.open_market(req(lots=1.0, sl=1.08000, tp=1.09500, price=1.08500))
        self.ex.set_price_hint("EURUSD.h", 1.07900)
        snap = self.ex.read_account()
        self.assertEqual(snap.positions, [])
        # (1.08000 - 1.08500) * 100000 * 1.0 = -500, minus 5 commission
        self.assertAlmostEqual(snap.balance, 49495.0, places=2)
        self.assertAlmostEqual(snap.equity, 49495.0, places=2)
        hist = json.loads(self.store.get_kv(STATE_KEY))["history"]
        self.assertEqual(hist[-1]["reason"], "sl")
        self.assertEqual(hist[-1]["exit"], 1.08)

    def test_tp_hit_on_buy(self):
        self.ex.open_market(req(lots=0.5, sl=1.08000, tp=1.09500, price=1.08500))
        self.ex.set_price_hint("EURUSD", 1.09600)  # TV form works too
        snap = self.ex.read_account()
        self.assertEqual(snap.positions, [])
        self.assertAlmostEqual(snap.balance, 50000 + 500.0 - 2.5, places=2)

    def test_sell_sl_and_tp(self):
        self.ex.open_market(req(side="sell", lots=0.5, sl=1.09000, tp=1.08000, price=1.08500))
        self.ex.set_price_hint("EURUSD.h", 1.07950)
        snap = self.ex.read_account()
        self.assertEqual(snap.positions, [])
        self.assertAlmostEqual(snap.balance, 50000 + 250.0 - 2.5, places=2)

        self.ex.open_market(req(side="sell", lots=1.0, sl=1.08500, tp=1.07000, price=1.08000))
        self.ex.set_price_hint("EURUSD.h", 1.08600)
        snap = self.ex.read_account()
        self.assertEqual(snap.positions, [])
        self.assertAlmostEqual(snap.balance, 50247.5 - 500.0 - 5.0, places=2)

    def test_price_between_sl_and_tp_does_not_close(self):
        self.ex.open_market(req())
        self.ex.set_price_hint("EURUSD.h", 1.08200)
        self.ex.set_price_hint("EURUSD.h", 1.09400)
        self.assertEqual(len(self.ex.read_account().positions), 1)

    def test_other_symbol_hint_does_not_close(self):
        self.ex.open_market(req())
        self.ex.set_price_hint("GBPUSD.h", 0.5)
        self.assertEqual(len(self.ex.read_account().positions), 1)

    def test_invalid_hint_ignored(self):
        self.ex.open_market(req())
        with self.assertLogs("tvbridge.executors.paper", "WARNING"):
            self.ex.set_price_hint("EURUSD.h", 0)
            self.ex.set_price_hint("EURUSD.h", float("nan"))
        self.assertEqual(len(self.ex.read_account().positions), 1)

    def test_equity_marks_to_market(self):
        self.ex.open_market(req(lots=1.0))
        self.ex.open_market(req(symbol="GBPUSD.h", side="sell", lots=0.5, price=1.30000, sl=1.31, tp=None))
        self.ex.set_price_hint("EURUSD.h", 1.08700)   # +200
        self.ex.set_price_hint("GBPUSD.h", 1.30100)   # -50
        snap = self.ex.read_account()
        self.assertEqual(snap.balance, 50000.0)
        self.assertAlmostEqual(snap.equity, 50150.0, places=2)
        profits = {p.symbol: p.profit for p in snap.positions}
        self.assertAlmostEqual(profits["EURUSD.h"], 200.0, places=2)
        self.assertAlmostEqual(profits["GBPUSD.h"], -50.0, places=2)


class CloseTests(PaperTestCase):
    def test_close_returns_one_result_per_position(self):
        self.ex.open_market(req(lots=0.5))
        self.ex.open_market(req(lots=0.3))
        self.ex.set_price_hint("EURUSD.h", 1.08600)
        results = self.ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["filled", "filled"])
        self.assertEqual([r.ticket for r in results], ["P1", "P2"])
        self.assertEqual([r.lots for r in results], [0.5, 0.3])
        self.assertTrue(all(r.fill_price == 1.086 for r in results))
        snap = self.ex.read_account()
        self.assertEqual(snap.positions, [])
        # +50 - 2.5 and +30 - 1.5
        self.assertAlmostEqual(snap.balance, 50076.0, places=2)

    def test_close_without_hint_uses_entry_price(self):
        self.ex.open_market(req(lots=1.0))
        results = self.ex.close_positions("EURUSD.h")
        self.assertEqual(results[0].fill_price, 1.085)
        self.assertAlmostEqual(self.ex.read_account().balance, 49995.0, places=2)

    def test_side_filter(self):
        self.ex.open_market(req(side="buy"))
        self.ex.open_market(req(side="sell", sl=1.09, tp=1.07))
        results = self.ex.close_positions("EURUSD", side="sell")
        self.assertEqual([r.ticket for r in results], ["P2"])
        remaining = self.ex.read_account().positions
        self.assertEqual([(p.ticket, p.side) for p in remaining], [("P1", "buy")])

    def test_returns_empty_when_nothing_matches(self):
        self.assertEqual(self.ex.close_positions("EURUSD.h"), [])
        self.ex.open_market(req(side="buy"))
        self.assertEqual(self.ex.close_positions("EURUSD.h", side="sell"), [])
        self.assertEqual(self.ex.close_positions("GBPUSD.h"), [])
        self.assertEqual(len(self.ex.read_account().positions), 1)

    def test_close_all(self):
        self.ex.open_market(req())
        self.ex.open_market(req(symbol="GBPUSD.h", price=1.3, sl=1.29, tp=None))
        self.ex.open_market(req(symbol="USDJPY.h", price=150.0, sl=149.0, tp=None, digits=3))
        results = self.ex.close_all()
        self.assertEqual(sorted(r.ticket for r in results), ["P1", "P2", "P3"])
        self.assertTrue(all(r.status == "filled" for r in results))
        self.assertEqual(self.ex.read_account().positions, [])
        self.assertEqual(self.ex.close_all(), [])


class ConversionTests(PaperTestCase):
    def test_usdjpy_pnl_converted_at_exit_price(self):
        self.ex.open_market(req(symbol="USDJPY.h", lots=1.0, price=150.000, sl=149.000, tp=None, digits=3))
        self.ex.set_price_hint("USDJPY.h", 151.000)
        snap = self.ex.read_account()
        # 1.000 JPY * 100000 / 151 = 662.25 USD floating
        self.assertAlmostEqual(snap.positions[0].profit, 662.25, places=2)
        self.ex.close_positions("USDJPY.h")
        self.assertAlmostEqual(self.ex.read_account().balance, 50000 + 662.25 - 5.0, places=2)

    def test_usdjpy_sl_hit(self):
        self.ex.open_market(req(symbol="USDJPY.h", side="sell", lots=0.5, price=150.000, sl=150.500, tp=None,
                                digits=3))
        self.ex.set_price_hint("USDJPY.h", 150.700)
        # realized at SL 150.500: -0.5 * 100000 * 0.5 / 150.5
        expected = -0.5 * 100000 * 0.5 / 150.5 - 2.5
        self.assertAlmostEqual(self.ex.read_account().balance, 50000 + expected, places=2)

    def test_cross_converted_with_other_hint(self):
        cfg = make_cfg(self.home, symbols={"specs": {"EURGBP": {"quote": "GBP"}}})
        with Store(":memory:") as st:
            ex = PaperExecutor(cfg, st)
            ex.set_price_hint("GBPUSD.h", 1.25)
            res = ex.open_market(req(symbol="EURGBP.h", lots=1.0, price=0.85000, sl=0.84, tp=None))
            self.assertEqual(res.status, "filled")
            self.assertNotIn("warning", res.message)
            ex.set_price_hint("EURGBP.h", 0.86000)
            self.assertAlmostEqual(ex.read_account().positions[0].profit, 1250.0, places=2)

    def test_cross_without_rate_warns(self):
        cfg = make_cfg(self.home, symbols={"specs": {"EURGBP": {"quote": "GBP"}}})
        with Store(":memory:") as st:
            with self.assertLogs("tvbridge.executors.paper", "WARNING"):
                res = PaperExecutor(cfg, st).open_market(req(symbol="EURGBP.h", price=0.85, sl=0.84, tp=None))
            self.assertEqual(res.status, "filled")
            self.assertIn("no USD rate", res.message)


class PersistenceTests(PaperTestCase):
    def test_state_shared_across_instances(self):
        self.ex.open_market(req(lots=0.5))
        other = PaperExecutor(self.cfg, self.store)
        snap = other.read_account()
        self.assertEqual([p.ticket for p in snap.positions], ["P1"])
        self.assertEqual(other.open_market(req(lots=0.2)).ticket, "P2")
        other.set_price_hint("EURUSD.h", 1.08600)
        self.assertAlmostEqual(self.ex.read_account().equity, 50000 + 50.0 + 20.0, places=2)
        self.ex.close_all()
        self.assertEqual(PaperExecutor(self.cfg, self.store).read_account().positions, [])

    def test_state_survives_reopening_the_database(self):
        db = self.home / "tvbridge.db"
        with Store(db) as st:
            PaperExecutor(self.cfg, st).open_market(req(lots=0.5))
            PaperExecutor(self.cfg, st).set_price_hint("EURUSD.h", 1.0790)  # SL hit
        with Store(db) as st:
            snap = PaperExecutor(self.cfg, st).read_account()
            self.assertEqual(snap.positions, [])
            self.assertAlmostEqual(snap.balance, 50000 - 250.0 - 2.5, places=2)

    def test_corrupt_state_fails_closed(self):
        self.store.set_kv(STATE_KEY, "{not json")
        with self.assertRaises(ExecutorError) as cm:
            self.ex.read_account()
        self.assertEqual(cm.exception.code, "PAPER_STATE_INVALID")
        self.assertFalse(self.ex.health()["ok"])

    def test_health(self):
        h = self.ex.health()
        self.assertTrue(h["ok"])
        self.assertIn("balance 50000.00", h["detail"])


class MakeExecutorPaperTests(PaperTestCase):
    def test_paper_mode(self):
        ex = make_executor(self.cfg, self.store)
        self.assertIsInstance(ex, PaperExecutor)
        self.assertEqual(ex.name, "paper")

    def test_bad_mode(self):
        self.cfg.executor.mode = "yolo"
        with self.assertRaises(ExecutorError) as cm:
            make_executor(self.cfg, self.store)
        self.assertEqual(cm.exception.code, "BAD_MODE")


class QuoteUsdTests(PaperTestCase):
    def test_cross_pair_uses_the_request_quote_usd(self):
        cfg = make_cfg(self.home, symbols={"specs": {"EURGBP": {"quote": "GBP"}}})
        ex = PaperExecutor(cfg, self.store)
        res = ex.open_market(OrderRequest(symbol="EURGBP.h", side="buy", lots=1.0, sl=0.86000, tp=None, digits=5,
                                          price_hint=0.87000, quote_usd=1.25))
        self.assertEqual(res.status, "filled")
        self.assertNotIn("warning", res.message)
        ex.set_price_hint("EURGBP.h", 0.87100)
        snap = ex.read_account()
        # +10 pips x 100,000 x 1.25 USD/GBP = +125.00 USD floating
        self.assertAlmostEqual(snap.equity - snap.balance, 125.0, places=2)


if __name__ == "__main__":
    unittest.main()
