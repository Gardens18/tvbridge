"""Re-entry after a broker-side stop (mirror.reenter_after_stop)."""

import time
import unittest

from tests.test_mirror import MirrorEngineBase
from tvbridge.engine import REENTRY_OF


class ReentryTests(MirrorEngineBase):
    def cfg(self, **mirror):
        m = {"reenter_after_stop": True, "reenter_cooldown_s": 0.0, "reenter_max": 1}
        m.update(mirror)
        return self.mirror_cfg(mirror=m, risk={})

    def stop_out(self, price):
        """Move the paper market through the stop and let two polls book the close."""
        self.executor.set_price_hint("XAUUSD.h", price)
        for _ in range(2):
            self.assertTrue(self.engine.request_account_poll())
            self.assertTrue(self.engine.wait_idle(10))

    def settle(self):
        """Run the engine loop until the re-entry (if any) has been processed."""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            self.engine._tick()
            self.assertTrue(self.engine.wait_idle(10))
            if not self.engine._pending_reentries:
                break
        self.assertTrue(self.engine.wait_idle(10))

    def test_stop_out_while_the_strategy_stays_long_reenters_once(self):
        self.start(self.cfg())
        row = self.sync("long", 28, price=4176.5)
        self.assertEqual([(p.side, p.lots) for p in self.xau()], [("buy", 0.28)])
        self.stop_out(4160.0)                                   # below the 8.5 stop
        self.assertEqual(self.xau(), [])
        self.assertEqual(self.all_ledger()[0]["close_reason"], "closed_on_server")
        self.assertTrue(self.events("reentry_scheduled"))
        self.settle()
        self.assertTrue(self.events("reentry_submitted"))
        re_id = row["id"] + "~re1"
        re_row = self.row(re_id)
        self.assertIsNotNone(re_row)
        self.assertEqual((re_row["status"], re_row["reason"]), ("done", ""), re_row)
        stopped = [r for r in self.all_ledger() if r["close_reason"] == "closed_on_server"]
        self.assertEqual(re_row["payload"]["raw"][REENTRY_OF], stopped[0]["pid"])
        self.assertEqual([(p.side, p.lots) for p in self.xau()], [("buy", 0.28)])
        self.assertEqual(len(self.all_ledger()), 2)
        self.assertEqual(self.ledger()[0]["signal_id"], re_id)
        # a second stop-out: reenter_max reached, no further re-entry
        self.stop_out(4140.0)
        self.assertEqual(self.xau(), [])
        self.assertTrue(self.events("reentry_exhausted"))
        self.settle()
        self.assertEqual(self.xau(), [])
        self.assertEqual(len(self.all_ledger()), 2)

    def test_close_in_profit_never_reenters(self):
        self.start(self.cfg(tp_distance=12.0))
        self.sync("long", 28, price=4176.5)
        self.stop_out(4190.0)                                   # take-profit hit
        self.assertEqual(self.xau(), [])
        self.settle()
        self.assertEqual(self.events("reentry_scheduled"), [])
        self.assertEqual(self.xau(), [])

    def test_no_reentry_when_the_strategy_went_flat(self):
        self.start(self.cfg(reenter_cooldown_s=30.0))
        self.sync("long", 28, price=4176.5)
        self.stop_out(4160.0)
        self.assertTrue(self.events("reentry_scheduled"))
        row = self.sync("flat", 0, price=4160.0, order_id="exit")   # strategy exits during the cooldown
        self.assertTrue(row["reason"].startswith("IN_SYNC"), row["reason"])
        self.engine._pending_reentries = [(0.0, c, p) for _, c, p in self.engine._pending_reentries]
        self.settle()
        self.assertTrue(self.events("reentry_dropped"))
        self.assertEqual(self.xau(), [])

    def test_strategy_flat_before_the_stop_is_booked_never_reenters(self):
        self.start(self.cfg())
        self.sync("long", 28, price=4176.5)
        self.executor.set_price_hint("XAUUSD.h", 4160.0)        # the stop fills on the server ...
        self.sync("flat", 0, price=4160.0, order_id="exit")     # ... and the strategy exits too
        self.stop_out(4160.0)
        self.settle()
        self.assertEqual(self.events("reentry_scheduled"), [])
        self.assertEqual(self.xau(), [])

    def test_disabled_by_default(self):
        self.start(self.mirror_cfg(risk={}))
        self.sync("long", 28, price=4176.5)
        self.stop_out(4160.0)
        self.settle()
        self.assertEqual(self.events("reentry_scheduled"), [])
        self.assertEqual(self.xau(), [])


if __name__ == "__main__":
    unittest.main()
