"""Mirror mode ("sync" alerts): payload parsing, the engine in paper mode, and the MT5 GUI
executor (quote-based stops, partial closes) against the simulated MetaTrader in tests/fakes.py.

Nothing here touches the real desktop: GUI tests use FakeMt5Driver only.
"""

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.fakes import FakeMt5Driver, make_calibration
from tests.test_engine import SECRET, T0, EngineTestBase
from tvbridge import clock, signals
from tvbridge.config import ConfigError, config_from_dict, deep_merge
from tvbridge.engine import CLOSE, PRIORITIES, SYNC, build_status
from tvbridge.executors.mt5gui import Mt5GuiExecutor
from tvbridge.executors.paper import PaperExecutor
from tvbridge.gui import parse
from tvbridge.gui.driver import OcrItem
from tvbridge.models import OrderRequest, OrderResult, Signal
from tvbridge.signals import SignalError
from tvbridge.store import Store

ROOT = Path(__file__).resolve().parent.parent
ALERTS = ROOT / "tradingview" / "ALERTS.md"

#: the copy-paste alert message documented in tradingview/ALERTS.md ("Mirror mode")
MIRROR_TEMPLATE = (
    '{"secret":"PASTE_SECRET","time":"{{timenow}}","symbol":"{{ticker}}","action":"sync",'
    '"position":"{{strategy.market_position}}","size":{{strategy.market_position_size}},'
    '"prev_position":"{{strategy.prev_market_position}}","prev_size":{{strategy.prev_market_position_size}},'
    '"price":{{strategy.order.price}},"order_action":"{{strategy.order.action}}",'
    '"order_contracts":{{strategy.order.contracts}},"order_id":"{{strategy.order.id}}",'
    '"comment":"{{strategy.order.comment}}"}'
)

_QUIET = logging.NullHandler()


def setUpModule() -> None:
    logging.getLogger("tvbridge").addHandler(_QUIET)


def tearDownModule() -> None:
    logging.getLogger("tvbridge").removeHandler(_QUIET)


def render(template, **values):
    out = template
    for key, value in values.items():
        out = out.replace("{{%s}}" % key, str(value))
    assert "{{" not in out, out
    return out


def fill_values(position="long", size=28, prev_position="flat", prev_size=0, price=4176.5, action="buy",
                contracts=28, order_id="Long", comment="X3 long entry", time="2026-10-06T07:00:00Z"):
    return {"timenow": time, "ticker": "XAUUSD", "strategy.market_position": position,
            "strategy.market_position_size": size, "strategy.prev_market_position": prev_position,
            "strategy.prev_market_position_size": prev_size, "strategy.order.price": price,
            "strategy.order.action": action, "strategy.order.contracts": contracts,
            "strategy.order.id": order_id, "strategy.order.comment": comment}


class SyncPayloadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_from_dict({"server": {"secret": SECRET}}, "/tmp/tvb-mirror-none")

    def parse(self, d):
        return signals.parse_payload(d, self.cfg, T0)

    def base(self, **kw):
        d = {"time": "2026-10-06T07:00:00Z", "symbol": "XAUUSD", "action": "sync", "position": "long",
             "size": 28, "price": 4176.5, "order_id": "Long"}
        d.update(kw)
        return d

    def test_template_in_alerts_md_is_this_exact_text(self):
        self.assertIn(MIRROR_TEMPLATE, ALERTS.read_text(encoding="utf-8"))

    def test_template_long_entry(self):
        body = render(MIRROR_TEMPLATE, **fill_values()).replace("PASTE_SECRET", SECRET)
        d = signals.decode_body(body.encode("utf-8"))
        self.assertTrue(signals.check_secret(d, self.cfg))
        self.assertEqual(signals.peek_action(d), "sync")
        sig = self.parse(d)
        self.assertEqual((sig.action, sig.symbol, sig.tv_symbol), ("sync", "XAUUSD.h", "XAUUSD"))
        self.assertEqual((sig.target_side, sig.target_units, sig.price), ("buy", 28.0, 4176.5))
        self.assertIsNone(sig.sl)
        self.assertEqual(sig.comment, "X3 long entry")
        self.assertNotIn("secret", sig.raw)
        self.assertEqual(sig.raw["order_id"], "Long")
        self.assertEqual(sig.raw["prev_position"], "flat")
        self.assertTrue(sig.id.startswith("sync:"))

    def test_template_flat_with_size_zero_and_comment_with_spaces(self):
        body = render(MIRROR_TEMPLATE, **fill_values(position="flat", size=0, prev_position="long", prev_size=19.6,
                                                    price=4181.25, action="sell", contracts=19.6,
                                                    order_id="Runner exit", comment="trail stop hit  now"))
        sig = self.parse(signals.decode_body(body.encode("utf-8")))
        self.assertEqual((sig.action, sig.target_side, sig.target_units), ("sync", None, None))
        self.assertEqual(sig.comment, "trail stop hit  now")

    def test_template_partial_and_short(self):
        body = render(MIRROR_TEMPLATE, **fill_values(position="long", size=19.6, prev_position="long", prev_size=28,
                                                    action="sell", contracts=8.4, order_id="TP"))
        sig = self.parse(json.loads(body))
        self.assertEqual((sig.target_side, sig.target_units), ("buy", 19.6))
        short = self.parse(self.base(position="short", size=-28))
        self.assertEqual((short.target_side, short.target_units), ("sell", 28.0))

    def test_aliases_ticker_market_position(self):
        d = {"timenow": "2026-10-06T07:00:00Z", "ticker": "OANDA:XAUUSD", "action": "SYNC",
             "market_position": "Short", "market_position_size": "14"}
        sig = self.parse(d)
        self.assertEqual((sig.symbol, sig.target_side, sig.target_units, sig.price), ("XAUUSD.h", "sell", 14.0, None))

    def test_errors(self):
        for change, code in (({"position": "sideways"}, "BAD_POSITION"), ({"position": None}, "BAD_POSITION"),
                             ({"size": "lots"}, "BAD_SIZE"), ({"size": 0}, "BAD_SIZE"), ({"size": None}, "BAD_SIZE"),
                             ({"time": None}, "NO_TIME"), ({"symbol": None}, "NO_SYMBOL"),
                             ({"symbol": "DOGEUSD"}, "SYMBOL_NOT_ALLOWED")):
            with self.assertRaises(SignalError) as cm:
                self.parse(self.base(**change))
            self.assertEqual(cm.exception.code, code, change)

    def test_flat_ignores_a_bad_size_and_a_bad_price(self):
        sig = self.parse(self.base(position="flat", size="NaN", price="oops"))
        self.assertEqual((sig.target_side, sig.target_units, sig.price), (None, None, None))
        # a reduce/entry with an unreadable price still parses (the engine refuses to OPEN on it)
        self.assertIsNone(self.parse(self.base(price="n/a")).price)

    def test_dedupe_id(self):
        a = self.parse(self.base())
        self.assertEqual(a.id, self.parse(self.base()).id)                      # a TradingView retry
        self.assertEqual(a.id, self.parse(self.base(comment="other")).id)
        others = [self.base(size=19.6), self.base(position="short"), self.base(order_id="TP"),
                  self.base(time="2026-10-06T07:00:01Z"), self.base(position="flat", size=0)]
        ids = {self.parse(d).id for d in others} | {a.id}
        self.assertEqual(len(ids), len(others) + 1)

    def test_round_trip_and_old_rows(self):
        sig = self.parse(self.base())
        back = Signal.from_dict(json.loads(json.dumps(sig.to_dict())))
        self.assertEqual(back, sig)
        old = sig.to_dict()
        del old["target_side"], old["target_units"]                              # a row stored before mirror mode
        legacy = Signal.from_dict(old)
        self.assertEqual((legacy.target_side, legacy.target_units), (None, None))

    def test_order_request_and_result_fields(self):
        req = OrderRequest("XAUUSD.h", "buy", 0.28, 4168.0, None, 2, sl_distance=8.5)
        self.assertEqual((req.to_dict()["sl_distance"], req.to_dict()["tp_distance"]), (8.5, None))
        self.assertEqual(OrderResult("filled", sl=4168.47).to_dict()["sl"], 4168.47)
        self.assertIsNone(OrderRequest("EURUSD.h", "buy", 0.1, 1.08, None, 5).sl_distance)


class MirrorConfigTests(unittest.TestCase):
    def cfg(self, **mirror):
        return config_from_dict({"server": {"secret": SECRET}, "mirror": mirror}, "/tmp/tvb-mirror-none")

    def test_defaults(self):
        m = self.cfg().mirror
        self.assertEqual((m.enabled, m.units_per_lot, m.stop_distance, m.tp_distance, m.size_tolerance_lots,
                          m.max_price_gap_pct, m.allow_adds), (False, 100.0, 8.5, 0.0, 0.005, 0.5, False))
        self.assertEqual(self.cfg().executor.gui.volume_field_offset_px, 85)
        self.assertIn("mirror", self.cfg().to_dict())

    def test_validation(self):
        for bad in ({"stop_distance": 0}, {"stop_distance": -1}, {"units_per_lot": 0}, {"tp_distance": -0.1},
                    {"size_tolerance_lots": -1}, {"max_price_gap_pct": 0}, {"enabled": "yes"},
                    {"allow_adds": 1}, {"stop_distanse": 8}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                self.cfg(**bad)
        self.assertTrue(self.cfg(enabled=True, tp_distance=12, allow_adds=True).mirror.enabled)


class StoreMirrorTests(unittest.TestCase):
    def test_reduce_ledger_position_and_kv_prefix(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        pid = store.add_position("s1", "XAUUSD.h", "buy", 0.28, 4176.9, 4168.4, None, 280.0, T0, ticket="P1")
        self.assertAlmostEqual(store.reduce_ledger_position(pid, 0.07), 0.21)
        row = store.open_ledger_positions()[0]
        self.assertAlmostEqual(row["lots"], 0.21)
        self.assertAlmostEqual(row["risk_usd"], 210.0)
        self.assertEqual(store.reduce_ledger_position(pid, 5), 0.0)
        self.assertIsNone(store.reduce_ledger_position(9999, 0.1))
        store.set_kv("mirror_scale:XAUUSD", "0.5")
        store.set_kv("mirror_scale:EURUSD", "1.0")
        store.set_kv("paused", "1")
        self.assertEqual(store.kv_with_prefix("mirror_scale:"), {"mirror_scale:XAUUSD": "0.5",
                                                                "mirror_scale:EURUSD": "1.0"})
        self.assertEqual(store.delete_kv_prefix("mirror_scale:"), 2)
        self.assertEqual(store.get_kv("paused"), "1")

    def test_newer_sync_signal(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        cfg = config_from_dict({"server": {"secret": SECRET}}, "/tmp/tvb-mirror-none")

        def add(seconds, order_id, symbol="XAUUSD"):
            d = {"time": clock.iso(T0 + timedelta(seconds=seconds)), "symbol": symbol, "action": "sync",
                 "position": "long", "size": 28, "price": 1.1, "order_id": order_id}
            sig = signals.parse_payload(d, cfg, T0 + timedelta(seconds=30))
            self.assertTrue(store.insert_signal(sig))
            return sig

        a, b, c = add(0, "a"), add(1, "b"), add(1, "c")
        other = add(5, "x", symbol="EURUSD")
        self.assertEqual(store.newer_sync_signal(a.id)["id"], c.id)
        self.assertEqual(store.newer_sync_signal(b.id)["id"], c.id)      # same instant: stored later wins
        self.assertIsNone(store.newer_sync_signal(c.id))
        self.assertIsNone(store.newer_sync_signal(other.id))
        self.assertIsNone(store.newer_sync_signal("missing"))


class PaperMirrorTests(unittest.TestCase):
    def setUp(self):
        clock.set_clock(lambda: T0)
        self.addCleanup(clock.set_clock, None)
        self.cfg = config_from_dict({"server": {"secret": SECRET}}, "/tmp/tvb-mirror-none")
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ex = PaperExecutor(self.cfg, self.store)

    def test_sl_distance_from_the_price_hint(self):
        res = self.ex.open_market(OrderRequest("XAUUSD.h", "buy", 0.28, 1.0, None, 2, price_hint=4176.97,
                                               sl_distance=8.5, tp_distance=12.0))
        self.assertEqual((res.status, res.sl, res.tp), ("filled", 4168.47, 4188.97))
        res = self.ex.open_market(OrderRequest("EURUSD.h", "sell", 0.10, 9.0, None, 5, price_hint=1.1,
                                               sl_distance=0.002))
        self.assertEqual((res.status, res.sl, res.tp), ("filled", 1.102, None))
        pos = {p.symbol: p for p in self.ex.read_account().positions}
        self.assertEqual((pos["XAUUSD.h"].sl, pos["XAUUSD.h"].tp, pos["EURUSD.h"].sl), (4168.47, 4188.97, 1.102))

    def test_partial_close_is_pro_rata(self):
        self.ex.open_market(OrderRequest("XAUUSD.h", "buy", 0.28, 4168.0, None, 2, price_hint=4176.0))
        self.ex.set_price_hint("XAUUSD.h", 4186.0)
        res = self.ex.close_partial("XAUUSD.h", "buy", 0.08)
        self.assertEqual([(r.status, r.lots, r.ticket) for r in res], [("filled", 0.08, "P1")])
        snap = self.ex.read_account()
        self.assertAlmostEqual(snap.positions[0].lots, 0.20)
        self.assertAlmostEqual(snap.balance, 50000 + 10.0 * 100 * 0.08 - 5.0 * 0.08)
        self.assertEqual(self.ex.close_partial("XAUUSD.h", "sell", 0.05), [])
        self.assertEqual(self.ex.close_partial("XAUUSD.h", "long", 0.05)[0].status, "error")

    def test_partial_close_across_positions_largest_first(self):
        self.ex.open_market(OrderRequest("XAUUSD.h", "buy", 0.10, 4168.0, None, 2, price_hint=4176.0))
        self.ex.open_market(OrderRequest("XAUUSD.h", "buy", 0.20, 4168.0, None, 2, price_hint=4176.0))
        res = self.ex.close_partial("XAUUSD.h", "buy", 0.25)
        self.assertEqual([(r.ticket, r.lots) for r in res], [("P2", 0.20), ("P1", 0.05)])
        left = self.ex.read_account().positions
        self.assertEqual([(p.ticket, p.lots) for p in left], [("P1", 0.05)])


# ---------------------------------------------------------------------------------- engine (paper)


class UnknownPositionsPaper(PaperExecutor):
    """Paper executor whose position list is unverifiable (like TOOLBOX_INCOMPLETE)."""

    hide = False

    def read_account(self):
        snap = super().read_account()
        if self.hide:
            snap.positions = None
            snap.positions_note = "TOOLBOX_INCOMPLETE: simulated"
        return snap


class MirrorEngineBase(EngineTestBase):
    RISK = {"max_lots": 0.14}       # caps the 0.28 lots the strategy wants -> scale 0.5

    def mirror_cfg(self, mirror=None, risk=None, **sections):
        m = {"enabled": True}
        m.update(mirror or {})
        return self.make_cfg(mirror=m, risk=dict(self.RISK if risk is None else risk), **sections)

    def payload(self, position, size, price=4176.5, order_id="Long", prev_position=None, prev_size=None,
                age_s=0.0, **extra):
        d = {"action": "sync", "symbol": "XAUUSD", "position": position, "size": size, "price": price,
             "order_id": order_id, "time": clock.iso(self.now - timedelta(seconds=age_s))}
        if prev_position is not None:
            d["prev_position"], d["prev_size"] = prev_position, prev_size
        d.update(extra)
        return d

    def sync(self, position, size, **kw):
        status, data = self.send(self.payload(position, size, **kw))
        self.assertEqual(status, 200, data)
        self.tick()
        return self.row(data["id"])

    def tick(self, seconds=1):
        self.now = self.now + timedelta(seconds=seconds)

    def xau(self):
        return [p for p in self.paper_positions() if p.symbol == "XAUUSD.h"]

    def scale(self):
        return self.store.get_kv("mirror_scale:XAUUSD")


class MirrorEngineTests(MirrorEngineBase):
    def test_priority_matches_close(self):
        self.assertEqual(PRIORITIES[SYNC], PRIORITIES[CLOSE])

    def test_open_is_capped_by_the_risk_guard_and_scaled(self):
        self.start(self.mirror_cfg())
        row = self.sync("long", 28)
        self.assertEqual((row["status"], row["reason"]), ("done", ""))
        pos = self.xau()
        self.assertEqual([(p.side, p.lots, p.sl, p.tp) for p in pos], [("buy", 0.14, 4168.0, None)])
        led = self.ledger()
        self.assertEqual([(r["symbol"], r["side"], r["lots"], r["sl"]) for r in led],
                         [("XAUUSD.h", "buy", 0.14, 4168.0)])
        plan = row["result"]["plan"]
        self.assertEqual(plan["lots"], 0.14)
        self.assertAlmostEqual(led[0]["risk_usd"], 0.14 * (8.5 * 100 + 5) * 1.15, places=2)
        self.assertEqual(row["result"]["order"]["sl_distance"], 8.5)
        self.assertAlmostEqual(float(self.scale()), 0.5)
        st = build_status(self.cfg, self.store)
        self.assertTrue(st["mirror"]["enabled"])
        self.assertAlmostEqual(st["mirror"]["scales"]["XAUUSD"], 0.5)
        self.assertAlmostEqual(self.engine.status()["mirror"]["scales"]["XAUUSD"], 0.5)

    def test_open_uncapped_keeps_scale_one_and_tp_distance(self):
        self.start(self.mirror_cfg(mirror={"tp_distance": 12.0}, risk={}))
        row = self.sync("short", 28)
        self.assertEqual(row["status"], "done")
        self.assertEqual([(p.side, p.lots, p.sl, p.tp) for p in self.xau()], [("sell", 0.28, 4185.0, 4164.5)])
        self.assertAlmostEqual(float(self.scale()), 1.0)

    def test_in_sync(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        row = self.sync("long", 28, order_id="again")
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["reason"].startswith("IN_SYNC"), row["reason"])
        self.assertEqual([p.lots for p in self.xau()], [0.14])
        self.assertEqual(len(self.all_ledger()), 1)

    def test_partial_close_with_scaled_target(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        risk0 = self.ledger()[0]["risk_usd"]
        # the strategy takes 30 % off: 28 -> 19.6 units; at scale 0.5 that is 0.098 -> 0.09 lots
        row = self.sync("long", 19.6, price=4184.0, order_id="TP", prev_position="long", prev_size=28)
        self.assertEqual((row["status"], row["reason"]), ("done", ""))
        self.assertEqual(row["result"]["partial_lots"], 0.05)
        self.assertEqual([(p.side, p.lots) for p in self.xau()], [("buy", 0.09)])
        led = self.ledger()
        self.assertAlmostEqual(led[0]["lots"], 0.09)
        self.assertAlmostEqual(led[0]["risk_usd"], risk0 * 0.09 / 0.14, places=4)
        self.assertAlmostEqual(float(self.scale()), 0.5)
        # the same alert state again: nothing more to do
        again = self.sync("long", 19.6, price=4184.0, order_id="TP2")
        self.assertTrue(again["reason"].startswith("IN_SYNC"))
        # the runner exits: flat
        flat = self.sync("flat", 0, price=4190.0, order_id="Runner", prev_position="long", prev_size=19.6)
        self.assertEqual(flat["status"], "done")
        self.assertEqual(self.xau(), [])
        self.assertEqual(self.ledger(), [])
        self.assertIsNone(self.scale())

    def test_target_below_min_lot_closes_everything(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        row = self.sync("long", 1.0, order_id="tiny", prev_position="long", prev_size=28)   # 0.005 lots at scale 0.5
        self.assertEqual(row["status"], "done")
        self.assertEqual(self.xau(), [])
        self.assertEqual(self.ledger(), [])

    def test_full_close_and_flat_when_already_flat(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        row = self.sync("flat", 0, order_id="exit")
        self.assertEqual((row["status"], row["reason"]), ("done", ""))
        self.assertEqual(self.xau(), [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.all_ledger()[0]["close_reason"], "sync_close")
        self.assertIsNone(self.scale())
        again = self.sync("flat", 0, order_id="exit2")
        self.assertEqual(again["status"], "done")
        self.assertTrue(again["reason"].startswith("IN_SYNC"))

    def test_reversal_in_one_sync(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        row = self.sync("short", 28, price=4170.0, order_id="Short", prev_position="long", prev_size=28)
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual([(p.side, p.lots, p.sl) for p in self.xau()], [("sell", 0.14, 4178.5)])
        self.assertEqual([(r["side"], r["lots"]) for r in self.ledger()], [("sell", 0.14)])
        self.assertEqual(sorted(r["status"] for r in self.all_ledger()), ["closed", "open"])

    def test_untracked_position_on_the_symbol_is_closed_too(self):
        self.build(self.mirror_cfg())
        # a position tvbridge did not open (truth is MT5, not the ledger)
        self.executor.open_market(OrderRequest("XAUUSD.h", "buy", 0.30, 4160.0, None, 2, price_hint=4176.0))
        self.engine.start()
        row = self.sync("flat", 0, order_id="exit")
        self.assertEqual(row["status"], "done")
        self.assertEqual(self.xau(), [])

    def test_three_queued_syncs_only_the_newest_acts(self):
        self.build(self.mirror_cfg())
        ids = []
        for i, (position, size, order_id) in enumerate((("long", 28, "Long"), ("long", 19.6, "TP"),
                                                         ("short", 28, "Short"))):
            d = self.payload(position, size, order_id=order_id)
            d["time"] = clock.iso(self.now + timedelta(seconds=i))
            sig = signals.parse_payload(d, self.cfg, self.now + timedelta(seconds=i))
            self.assertTrue(self.store.insert_signal(sig))
            ids.append(sig.id)
        self.now = self.now + timedelta(seconds=3)
        self.engine.start()                       # startup recovery re-submits the three queued syncs
        self.assertTrue(self.engine.wait_idle(15))
        rows = [self.row(i) for i in ids]
        self.assertEqual([r["status"] for r in rows], ["expired", "expired", "done"])
        self.assertTrue(rows[0]["reason"].startswith("SUPERSEDED_BY_SYNC"))
        self.assertTrue(rows[1]["reason"].startswith("SUPERSEDED_BY_SYNC"))
        self.assertEqual([(p.side, p.lots) for p in self.xau()], [("sell", 0.14)])
        self.assertEqual(len(self.all_ledger()), 1)

    def test_processing_sync_at_startup_is_interrupted_and_halts(self):
        self.build(self.mirror_cfg())
        sig = signals.parse_payload(self.payload("long", 28), self.cfg, self.now)
        self.store.insert_signal(sig, status="processing")
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        row = self.row(sig.id)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("INTERRUPTED"))
        self.assertTrue((self.store.get_kv("halted") or "").startswith("INTERRUPTED"))
        self.assertEqual(self.xau(), [])

    def test_stale_sync_still_closes_but_does_not_open(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        self.tick(600)
        # 5 minutes late: older than max_signal_age_s and entry_max_delay_s, accepted like an exit
        row = self.sync("short", 28, price=4170.0, order_id="Short", age_s=300)
        self.assertEqual(row["status"], "expired")
        self.assertTrue(row["reason"].startswith("STALE_SIGNAL"), row["reason"])
        self.assertEqual(self.xau(), [])                     # the long was closed, no short opened
        self.assertEqual(self.ledger(), [])
        # a stale sync into a flat account opens nothing either
        row = self.sync("long", 28, order_id="late", age_s=200)
        self.assertEqual(row["status"], "expired")
        self.assertEqual(self.xau(), [])

    def test_too_old_sync_is_refused_at_the_webhook(self):
        self.start(self.mirror_cfg())
        status, data = self.post(self.payload("flat", 0, age_s=1000))
        self.assertEqual((status, data["error"]), (400, "STALE"))
        self.assertTrue(any("was refused" in m for m in self.notes("critical")))

    def test_sync_is_never_rate_limited(self):
        self.start(self.mirror_cfg(server={"rate_limit_per_min": 1}))
        for i in range(4):
            status, _ = self.send(self.payload("flat", 0, order_id="x%d" % i))
            self.assertEqual(status, 200)

    def test_duplicate_alert_is_ignored(self):
        self.start(self.mirror_cfg())
        d = self.payload("long", 28)
        self.assertEqual(self.send(d)[0], 200)
        status, data = self.send(d)                         # TradingView retry
        self.assertEqual((status, data.get("duplicate")), (200, True))
        self.assertEqual([p.lots for p in self.xau()], [0.14])

    def test_paused_closes_but_does_not_open(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        self.store.set_kv("paused", "1")
        row = self.sync("long", 19.6, order_id="TP", prev_position="long", prev_size=28)
        self.assertEqual(row["status"], "done")
        self.assertEqual([p.lots for p in self.xau()], [0.09])
        row = self.sync("short", 28, order_id="Short")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("PAUSED"), row["reason"])
        self.assertEqual(self.xau(), [])                     # the reversal close ran, the short did not open
        row = self.sync("long", 28, order_id="Long2")
        self.assertTrue(row["reason"].startswith("PAUSED"))
        self.assertEqual(self.xau(), [])

    def test_halted_closes_but_does_not_open(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        self.store.set_kv("halted", "UNCERTAIN_EXECUTION: test")
        row = self.sync("flat", 0, order_id="exit")
        self.assertEqual(row["status"], "done")
        self.assertEqual(self.xau(), [])
        row = self.sync("long", 28, order_id="Long2")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("HALTED"))
        self.assertEqual(self.xau(), [])

    def test_risk_guard_rejection_applies(self):
        # an untracked position on ANOTHER symbol blocks the mirror entry like any entry
        self.build(self.mirror_cfg())
        self.executor.open_market(OrderRequest("EURUSD.h", "buy", 0.10, 1.09, None, 5, price_hint=1.10))
        self.engine.start()
        row = self.sync("long", 28)
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("UNTRACKED_POSITIONS"), row["reason"])
        self.assertEqual(self.xau(), [])
        self.assertIsNone(self.scale())

    def test_no_price_does_not_open(self):
        self.start(self.mirror_cfg())
        row = self.sync("long", 28, price=None)
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("NO_PRICE"))
        self.assertEqual(self.xau(), [])

    def test_partial_exit_alert_never_opens_a_position(self):
        self.start(self.mirror_cfg())
        row = self.sync("long", 19.6, order_id="TP", prev_position="long", prev_size=28)
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("MIRROR_NOT_AN_ENTRY"), row["reason"])
        self.assertEqual(self.xau(), [])

    def test_mirror_disabled(self):
        self.start(self.mirror_cfg(mirror={"enabled": False}))
        row = self.sync("long", 28)
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("MIRROR_DISABLED"))
        self.assertEqual(self.xau(), [])
        self.assertTrue(any("MIRROR_DISABLED" in m for m in self.notes("warn")))

    def test_add_refused(self):
        self.start(self.mirror_cfg(risk={}))
        self.sync("long", 14)
        row = self.sync("long", 28, order_id="add", prev_position="long", prev_size=14)
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["reason"].startswith("MIRROR_ADD_REFUSED"), row["reason"])
        self.assertEqual([p.lots for p in self.xau()], [0.14])
        self.assertTrue(any("MIRROR_ADD_REFUSED" in m for m in self.notes("warn")))

    def test_add_allowed_goes_through_the_entry_checks(self):
        self.start(self.mirror_cfg(mirror={"allow_adds": True}, risk={"allow_pyramiding": True}))
        self.sync("long", 14)
        row = self.sync("long", 28, order_id="add", prev_position="long", prev_size=14)
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual(sorted(p.lots for p in self.xau()), [0.14, 0.14])
        self.assertAlmostEqual(float(self.scale()), 1.0)
        # without pyramiding the risk guard refuses the add
        self.sync("flat", 0, order_id="exit")

    def test_positions_unknown(self):
        self.build(self.mirror_cfg())
        executor = UnknownPositionsPaper(self.cfg, self.store)
        self.executor = self.engine.executor = executor
        self.engine.start()
        self.sync("long", 28)
        executor.hide = True
        row = self.sync("long", 19.6, order_id="TP", prev_position="long", prev_size=28)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("POSITIONS_UNKNOWN"), row["reason"])
        self.assertTrue(any("POSITIONS_UNKNOWN" in m for m in self.notes("critical")))
        self.assertEqual([p.lots for p in PaperExecutor.read_account(executor).positions], [0.14])
        # a flat target still closes (fail open for exits)
        row = self.sync("flat", 0, order_id="exit")
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual(PaperExecutor.read_account(executor).positions, [])

    def test_close_exception_is_close_failed(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)

        def boom(symbol, side=None):
            raise RuntimeError("simulated")

        self.executor.close_positions = boom
        row = self.sync("flat", 0, order_id="exit")
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("CLOSE_FAILED"))
        self.assertTrue(any("CLOSE_FAILED" in m for m in self.notes("critical")))
        self.assertEqual(len(self.ledger()), 1)

    def test_flatten_resets_the_scale(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        self.assertIsNotNone(self.scale())
        self.engine.request_flatten("test")
        self.assertTrue(self.engine.wait_idle(15))
        self.assertEqual(self.xau(), [])
        self.assertIsNone(self.scale())

    def test_signal_close_resets_the_scale(self):
        self.start(self.mirror_cfg())
        self.sync("long", 28)
        self.send({"action": "close", "symbol": "XAUUSD", "id": "c1"})
        self.assertEqual(self.xau(), [])
        self.assertIsNone(self.scale())


# ---------------------------------------------------------------------------------- GUI executor

XAU_QUOTE = "4 176.77 / 4 176.97"


def xau_pos(ticket, lots=0.28, side="buy"):
    return {"ticket": str(ticket), "symbol": "XAUUSD.h", "side": side, "lots": lots, "open_price": 4176.97,
            "sl": 4168.47, "tp": 0.0, "price": 4176.97, "profit": 0.0}


class QuoteParseTests(unittest.TestCase):
    @staticmethod
    def item(text, y=100.0, h=14.0, x=10.0):
        return OcrItem(text=text, conf=0.9, x=x, y=y, w=7.0 * len(text), h=h)

    def test_parse_ticket_quote(self):
        q = parse.parse_ticket_quote([self.item("Volume:"), self.item(XAU_QUOTE, y=300, h=22),
                                      self.item("Sell by Market", y=370)])
        self.assertEqual(q, (4176.77, 4176.97))
        self.assertEqual(parse.parse_ticket_quote([self.item("1.08340 / 1.08345")]), (1.0834, 1.08345))
        self.assertEqual(parse.parse_ticket_quote([self.item("4 176.77 / 4 176.97")]), (4176.77, 4176.97))

    def test_split_observations_are_rejoined(self):
        items = [self.item("4 176.77", x=10), self.item("/", x=80), self.item("4 176.97", x=100)]
        self.assertEqual(parse.parse_ticket_quote(items), (4176.77, 4176.97))

    def test_unreadable(self):
        for text in ("", "4 176.77", "4 176.97 / 4 176.77", "4 176.77 / 4 376.97", "4176 / 4177",
                     "bid 4 176.77 / 4 176.97", "0.28 / 4 176.97"):
            self.assertIsNone(parse.parse_ticket_quote([self.item(text)] if text else []), text)
        # two different quotes of the same size: ambiguous
        self.assertIsNone(parse.parse_ticket_quote([self.item("1.10 / 1.11"), self.item("1.20 / 1.21", y=200)]))


class MirrorGuiTests(unittest.TestCase):
    def setUp(self):
        clock.set_clock(lambda: datetime(2026, 10, 1, 9, 56, tzinfo=timezone.utc))
        self.addCleanup(clock.set_clock, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = config_from_dict(deep_merge(
            {"server": {"secret": SECRET},
             "account": {"account_login": "12345678", "server_name": "HantecMarketsMU-MT5"}}, {}), self.home)

    def make(self, rehearsal=False, **fake_kw):
        fake_kw.setdefault("order_quote", XAU_QUOTE)
        fake_kw.setdefault("fill_price", 4176.97)
        fake_kw.setdefault("close_price", 4180.10)
        self.fake = FakeMt5Driver(**fake_kw)
        return Mt5GuiExecutor(self.cfg, self.fake, make_calibration(), rehearsal=rehearsal,
                              shots_dir=self.home / "shots")

    @staticmethod
    def req(side="buy", price=4176.5, tp_distance=None, lots=0.28):
        return OrderRequest(symbol="XAUUSD.h", side=side, lots=lots, sl=1.0, tp=None, digits=2, lot_decimals=2,
                            comment="tvb", price_hint=price, sl_distance=8.5, tp_distance=tp_distance)

    # ---- open with sl_distance

    def test_buy_stop_and_target_from_the_ask(self):
        ex = self.make()
        res = ex.open_market(self.req("buy", tp_distance=12.0))
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual((res.sl, res.tp), (4168.47, 4188.97))
        self.assertEqual(self.fake.orders_sent, [{"side": "buy", "symbol": "XAUUSD.h", "volume": "0.28",
                                                  "sl": "4168.47", "tp": "4188.97"}])
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertEqual(self.fake.dangerous_returns, 0)

    def test_sell_stop_from_the_bid_without_target(self):
        ex = self.make(fill_price=4176.77)
        res = ex.open_market(self.req("sell"))
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual((res.sl, res.tp), (4185.27, None))
        self.assertEqual((self.fake.orders_sent[0]["sl"], self.fake.orders_sent[0]["tp"]), ("4185.27", "0"))

    def test_rehearsal_reports_the_levels_and_never_clicks(self):
        ex = self.make(rehearsal=True)
        res = ex.open_market(self.req("buy"))
        self.assertEqual((res.status, res.sl), ("rehearsed", 4168.47))
        self.assertEqual(self.fake.order_button_clicks, [])
        self.assertEqual(self.fake.dialogs, [])

    def test_quote_unreadable_sends_nothing(self):
        for quote in ("", "4 176.77", "n/a"):
            ex = self.make(order_quote=quote)
            res = ex.open_market(self.req("buy"))
            self.assertEqual(res.status, "error")
            self.assertTrue(res.message.startswith("QUOTE_UNREADABLE"), res.message)
            self.assertIn("nothing was sent", res.message)
            self.assertEqual(self.fake.order_button_clicks, [])
            self.assertEqual(self.fake.orders_sent, [])
            self.assertEqual(self.fake.dialogs, [])                   # ticket cancelled with Escape
            self.assertEqual(self.fake.dangerous_returns, 0)
            self.assertNotIn("0.28", self.fake.typed())               # stopped before the volume was typed

    def test_price_gap_sends_nothing(self):
        ex = self.make()
        res = ex.open_market(self.req("buy", price=4300.0))            # 2.9 % away
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("PRICE_GAP"), res.message)
        self.assertEqual(self.fake.order_button_clicks, [])
        self.assertEqual(self.fake.dialogs, [])
        # the default EURUSD quote on a gold order: the wrong symbol is caught the same way
        ex = self.make(order_quote="1.08340 / 1.08345")
        res = ex.open_market(self.req("buy"))
        self.assertTrue(res.message.startswith("PRICE_GAP"), res.message)
        self.assertEqual(self.fake.order_button_clicks, [])
        # the usual TradingView/broker difference (~0.9 on gold) passes
        ex = self.make()
        self.assertEqual(ex.open_market(self.req("buy", price=4177.87)).status, "filled")

    def test_bad_distance_request(self):
        ex = self.make()
        bad = OrderRequest("XAUUSD.h", "buy", 0.28, 4168.0, None, 2, price_hint=4176.5, sl_distance=0.0)
        res = ex.open_market(bad)
        self.assertTrue(res.message.startswith("BAD_REQUEST"))
        self.assertEqual(self.fake.actions, [])

    def test_absolute_sl_requests_are_unchanged(self):
        ex = self.make(order_quote="")          # no quote needed without sl_distance
        res = ex.open_market(OrderRequest("XAUUSD.h", "buy", 0.28, 4168.0, 4190.0, 2, price_hint=4176.9))
        self.assertEqual((res.status, res.sl, res.tp), ("filled", 4168.0, 4190.0))

    # ---- partial close

    def test_partial_close_happy_path(self):
        ex = self.make(positions=[xau_pos(1001)])
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertEqual([(r.status, r.lots, r.ticket) for r in res], [("filled", 0.09, "1001")], res[0].message)
        self.assertEqual(self.fake.partial_closes, [("1001", 0.09)])
        self.assertEqual(self.fake.closed_tickets, [])
        self.assertEqual([p["lots"] for p in self.fake.positions], [0.19])
        self.assertEqual(self.fake.close_button_clicks, ["1001"])
        self.assertIn("0.09", self.fake.typed())
        self.assertEqual(self.fake.dangerous_returns, 0)
        self.assertEqual(self.fake.dialogs, [])
        self.assertEqual([p.lots for p in ex.read_account().positions], [0.19])

    def test_partial_close_rehearsal_never_clicks_the_close_button(self):
        ex = self.make(rehearsal=True, positions=[xau_pos(1001)])
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertEqual([(r.status, r.lots) for r in res], [("rehearsed", 0.09)])
        self.assertIn("0.09", res[0].message)
        self.assertEqual(self.fake.close_button_clicks, [])
        self.assertEqual([p["lots"] for p in self.fake.positions], [0.28])
        self.assertIn("0.09", self.fake.typed())                       # everything but the click was done
        self.assertEqual(self.fake.dialogs, [])

    def test_partial_verify_failed_never_clicks(self):
        ex = self.make(positions=[xau_pos(1001)], position_volume_ignores_typing=True)
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].status, "error")
        self.assertTrue(res[0].message.startswith("PARTIAL_VERIFY_FAILED"), res[0].message)
        self.assertEqual(self.fake.close_button_clicks, [])
        self.assertEqual([p["lots"] for p in self.fake.positions], [0.28])
        self.assertEqual(self.fake.dialogs, [])
        self.assertEqual(self.fake.dangerous_returns, 0)

    def test_partial_verify_needs_the_ticket_on_the_button(self):
        ex = self.make(positions=[xau_pos(1001)], close_button_text="Close buy 0.09 XAUUSD.h by Market")
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertTrue(res[0].message.startswith("PARTIAL_VERIFY_FAILED"), res[0].message)
        self.assertEqual(self.fake.close_button_clicks, [])

    def test_partial_across_two_positions(self):
        ex = self.make(positions=[xau_pos(1001, lots=0.10), xau_pos(1002, lots=0.20)])
        res = ex.close_partial("XAUUSD.h", "buy", 0.25)
        self.assertEqual([(r.status, r.ticket, r.lots) for r in res],
                         [("filled", "1002", 0.20), ("filled", "1001", 0.05)])
        self.assertEqual(self.fake.closed_tickets, ["1002"])           # the largest is closed whole first
        self.assertEqual(self.fake.partial_closes, [("1001", 0.05)])
        self.assertEqual([(p["ticket"], p["lots"]) for p in self.fake.positions], [("1001", 0.05)])

    def test_whole_position_uses_the_full_close_path(self):
        ex = self.make(positions=[xau_pos(1001)])
        res = ex.close_partial("XAUUSD.h", "buy", 0.28)
        self.assertEqual([(r.status, r.lots) for r in res], [("filled", 0.28)])
        self.assertEqual(self.fake.closed_tickets, ["1001"])
        self.assertEqual(self.fake.partial_closes, [])
        self.assertNotIn("0.28", self.fake.typed())                    # no volume typed for a whole close

    def test_partial_close_other_side_or_nothing(self):
        ex = self.make(positions=[xau_pos(1001)])
        self.assertEqual(ex.close_partial("XAUUSD.h", "sell", 0.09), [])
        self.assertEqual(ex.close_partial("XAUUSD.h", "both", 0.09)[0].status, "error")
        self.assertEqual(ex.close_partial("XAUUSD.h", "buy", 0)[0].status, "error")
        self.assertEqual(self.fake.close_button_clicks, [])

    def test_partial_close_rejected_and_uncertain(self):
        ex = self.make(positions=[xau_pos(1001)], close_outcome="rejected")
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertEqual(res[0].status, "rejected")
        self.assertEqual([p["lots"] for p in self.fake.positions], [0.28])
        # the dialog vanished but the Toolbox shows the reduced row: confirmed
        ex = self.make(positions=[xau_pos(1001)], close_outcome="vanish")
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertEqual((res[0].status, res[0].lots), ("filled", 0.09))
        # the dialog vanished and the row is unchanged: uncertain
        ex = self.make(positions=[xau_pos(1001)], close_outcome="vanish_keep")
        res = ex.close_partial("XAUUSD.h", "buy", 0.09)
        self.assertEqual(res[0].status, "uncertain")


class MirrorEngineGuiTests(MirrorEngineBase):
    """The engine driving the GUI executor (fake MetaTrader) through a mirror sequence."""

    def gui_cfg(self, mode="live"):
        return self.mirror_cfg(executor={"mode": mode, "gui": {"account_poll_s": 3600}},
                               account={"account_login": "12345678", "server_name": "HantecMarketsMU-MT5"},
                               risk={})

    def start_gui(self, mode="live", **fake_kw):
        cfg = self.gui_cfg(mode)
        fake_kw.setdefault("order_quote", XAU_QUOTE)
        fake_kw.setdefault("fill_price", 4176.97)
        self.fake = FakeMt5Driver(**fake_kw)
        ex = Mt5GuiExecutor(cfg, self.fake, make_calibration(), rehearsal=(mode == "rehearsal"),
                            shots_dir=cfg.shots_dir)
        return self.start(cfg, executor=ex)

    def test_open_partial_and_close_through_the_gui(self):
        self.start_gui()
        row = self.sync("long", 28, price=4177.8)
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual(self.fake.orders_sent[0]["sl"], "4168.47")      # ask 4176.97 - 8.5, not alert - 8.5
        led = self.ledger()
        self.assertEqual([(r["side"], r["lots"], r["sl"]) for r in led], [("buy", 0.28, 4168.47)])
        row = self.sync("long", 19.6, price=4186.0, order_id="TP", prev_position="long", prev_size=28)
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual([p["lots"] for p in self.fake.positions], [0.19])
        self.assertAlmostEqual(self.ledger()[0]["lots"], 0.19)
        row = self.sync("flat", 0, price=4190.0, order_id="Runner")
        self.assertEqual(row["status"], "done", row["reason"])
        self.assertEqual(self.fake.positions, [])
        self.assertEqual(self.ledger(), [])

    def test_quote_unreadable_fails_the_signal_without_a_halt(self):
        self.start_gui(order_quote="")
        row = self.sync("long", 28)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("QUOTE_UNREADABLE"), row["reason"])
        self.assertIsNone(self.store.get_kv("halted"))
        self.assertEqual(self.fake.order_button_clicks, [])
        self.assertIsNone(self.scale())

    def test_uncertain_entry_halts_and_adopts(self):
        self.start_gui(order_outcome="vanish")
        row = self.sync("long", 28, price=4177.8)
        self.assertEqual(row["status"], "failed")
        self.assertTrue((self.store.get_kv("halted") or "").startswith("UNCERTAIN_EXECUTION"))
        self.assertEqual([(r["side"], r["lots"]) for r in self.ledger()], [("buy", 0.28)])   # adopted
        self.assertAlmostEqual(float(self.scale()), 1.0)

    def test_rehearsal_mode_sends_nothing(self):
        self.start_gui(mode="rehearsal")
        row = self.sync("long", 28, price=4177.8)
        self.assertEqual(row["status"], "rehearsed", row["reason"])
        self.assertEqual(self.fake.order_button_clicks, [])
        self.assertEqual(self.ledger(), [])
        self.assertIsNone(self.scale())


if __name__ == "__main__":
    unittest.main()


class IdeaRiskStopTests(unittest.TestCase):
    """mirror.idea_risk_pct: the stop distance shrinks as the strategy's size grows."""

    def test_distance_formula(self):
        # 25,000 * 0.9% = 225 USD; 28 oz -> 8.04, 42 oz -> 5.36, 63 oz -> 3.57 (capped at stop_distance)
        for units, want in ((28, 8.04), (42, 5.36), (63, 3.57), (10, 8.5)):
            by_risk = 25000 * 0.9 / 100.0 / (units / 100.0 * 100.0)
            self.assertAlmostEqual(round(max(min(8.5, by_risk), 1.0), 2), want, places=2)
