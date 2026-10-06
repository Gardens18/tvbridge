import json
import unittest
from datetime import datetime, timezone

from tvbridge.models import (
    ACTIONS, SIDES, AccountSnapshot, ObservedPosition, OrderRequest, OrderResult, Signal, TradePlan,
    opposite_side,
)

UTC = timezone.utc


def make_signal(**kw):
    base = dict(
        id="buy:abc", action="buy", tv_symbol="EURUSD", symbol="EURUSD.h", side=None,
        price=1.085, sl=1.081, tp=1.089, risk_pct=0.5, quote_usd=None,
        fired_at=datetime(2026, 10, 1, 9, 56, tzinfo=UTC),
        received_at=datetime(2026, 10, 1, 9, 56, 1, 250000, tzinfo=UTC),
        strategy="s1", comment="tvb", raw={"action": "buy", "nested": {"a": [1, 2]}},
    )
    base.update(kw)
    return Signal(**base)


class ConstantsTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(ACTIONS, ("buy", "sell", "close", "close_all", "sync"))
        self.assertEqual(SIDES, ("buy", "sell"))

    def test_opposite_side(self):
        self.assertEqual(opposite_side("buy"), "sell")
        self.assertEqual(opposite_side("sell"), "buy")
        with self.assertRaises(ValueError):
            opposite_side("close")


class SignalTests(unittest.TestCase):
    def test_to_dict_is_json_safe(self):
        d = make_signal().to_dict()
        text = json.dumps(d)
        self.assertIn('"fired_at": "2026-10-01T09:56:00Z"', text)
        self.assertEqual(d["received_at"], "2026-10-01T09:56:01.250000Z")
        self.assertEqual(d["raw"], {"action": "buy", "nested": {"a": [1, 2]}})

    def test_round_trip(self):
        sig = make_signal()
        back = Signal.from_dict(json.loads(json.dumps(sig.to_dict())))
        self.assertEqual(back, sig)
        self.assertEqual(back.fired_at.utcoffset().total_seconds(), 0)

    def test_round_trip_close_all_with_nones(self):
        sig = make_signal(id="close_all:x", action="close_all", tv_symbol="", symbol="", price=None, sl=None,
                          tp=None, risk_pct=None, raw={})
        self.assertEqual(Signal.from_dict(sig.to_dict()), sig)

    def test_from_dict_defaults_for_missing_optionals(self):
        sig = Signal.from_dict({"id": "x", "action": "close", "symbol": "EURUSD.h",
                                "fired_at": "2026-10-01T09:56:00Z", "received_at": "2026-10-01 09:56:02"})
        self.assertIsNone(sig.side)
        self.assertIsNone(sig.price)
        self.assertEqual(sig.tv_symbol, "")
        self.assertEqual(sig.strategy, "")
        self.assertEqual(sig.raw, {})
        self.assertEqual(sig.received_at, datetime(2026, 10, 1, 9, 56, 2, tzinfo=UTC))

    def test_to_dict_does_not_share_raw(self):
        sig = make_signal()
        d = sig.to_dict()
        d["raw"]["action"] = "sell"
        self.assertEqual(sig.raw["action"], "buy")

    def test_is_entry(self):
        self.assertTrue(make_signal().is_entry)
        self.assertFalse(make_signal(action="close").is_entry)


class OtherModelTests(unittest.TestCase):
    def test_snapshot_round_trip(self):
        snap = AccountSnapshot(ts=datetime(2026, 10, 1, tzinfo=UTC), balance=50000.0, equity=49950.5,
                               margin=100.0, free_margin=49850.5,
                               positions=[ObservedPosition("EURUSD.h", "buy", 0.5, ticket="123", profit=-49.5)],
                               source="mt5gui")
        d = snap.to_dict()
        json.dumps(d)
        self.assertEqual(AccountSnapshot.from_dict(d), snap)
        snap2 = AccountSnapshot(ts=snap.ts, balance=1.0, equity=1.0)
        self.assertIsNone(AccountSnapshot.from_dict(snap2.to_dict()).positions)

    def test_defaults_are_independent(self):
        a, b = OrderResult("filled"), OrderResult("filled")
        a.evidence.append("x.png")
        self.assertEqual(b.evidence, [])
        p1, p2 = TradePlan(False), TradePlan(False)
        p1.close_first.append(ObservedPosition("EURUSD.h", "sell", 1.0))
        p1.details["x"] = 1
        self.assertEqual(p2.close_first, [])
        self.assertEqual(p2.details, {})

    def test_order_request_defaults(self):
        req = OrderRequest(symbol="EURUSD.h", side="buy", lots=0.5, sl=1.081, tp=None, digits=5)
        self.assertEqual(req.lot_decimals, 2)
        self.assertEqual(req.comment, "")
        self.assertIsNone(req.price_hint)
        self.assertEqual(req.to_dict()["symbol"], "EURUSD.h")

    def test_order_result_to_dict(self):
        r = OrderResult("uncertain", "dialog vanished", evidence=["/tmp/a.png"])
        d = r.to_dict()
        self.assertEqual(d["status"], "uncertain")
        self.assertEqual(d["evidence"], ["/tmp/a.png"])
        json.dumps(d)

    def test_trade_plan_code(self):
        self.assertEqual(TradePlan(False, "PAUSED: paused by user").code, "PAUSED")
        self.assertEqual(TradePlan(True).code, "")
        self.assertEqual(TradePlan(True).reason, "")


class ImportHygieneTests(unittest.TestCase):
    def test_core_modules_do_not_import_pyobjc(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        code = (
            "import sys\n"
            "import tvbridge, tvbridge.models, tvbridge.config, tvbridge.clock, tvbridge.store, tvbridge.notify\n"
            "bad = [m for m in ('objc', 'Quartz', 'AppKit', 'Vision', 'ApplicationServices', 'Foundation')"
            " if m in sys.modules]\n"
            "print(','.join(bad))\n"
            "print(tvbridge.__version__)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(root), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], "", "pyobjc imported by core modules: %s" % lines[0])
        self.assertEqual(lines[1], "0.1.0")


if __name__ == "__main__":
    unittest.main()
