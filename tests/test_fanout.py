"""Mirror fan-out: one TradingView sync alert mirrored onto several MT5 symbols (e.g. XAUUSD,
1 lot = 100 oz, and XAUUSDmicro, 1 lot = 10 oz), each as its own independent position.

Paper engine for the engine behaviour; the GUI part uses FakeMt5Driver only (never the real desktop).
"""

import dataclasses
import logging
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.fakes import FakeMt5Driver, make_calibration
from tests.test_engine import SECRET, T0
from tests.test_mirror import MirrorEngineBase
from tvbridge import cli, clock, risk, signals
from tvbridge.config import ConfigError, config_from_dict, deep_merge
from tvbridge.engine import FAN_OUT_PARENT, RESYNC_N, RESYNC_OF, RESYNC_ROOT, build_status
from tvbridge.executors.mt5gui import Mt5GuiExecutor
from tvbridge.executors.paper import PaperExecutor
from tvbridge.gui import parse
from tvbridge.models import OrderRequest
from tvbridge.store import Store

_QUIET = logging.NullHandler()

MICRO_SPEC = {"contract_size": 10, "quote": "USD", "digits": 2, "point": 0.01, "min_sl_points": 100,
              "commission_per_lot_usd": 0.5}
SYMBOLS = {"specs": {"XAUUSDMICRO": MICRO_SPEC}, "map": {"XAUUSDMICRO": "XAUUSDmicro"}}
FAN = {"fan_out": {"XAUUSD": ["XAUUSD", "XAUUSDMICRO"]}, "units_per_lot_by_symbol": {"XAUUSDMICRO": 10}}
MICRO = "XAUUSDmicro"
GOLD = "XAUUSD.h"
# worst-case loss per lot with the 8.5 stop: (distance x contract size + commission) x 1.15 slippage buffer
GOLD_PER_LOT = (8.5 * 100 + 5.0) * 1.15
MICRO_PER_LOT = (8.5 * 10 + 0.5) * 1.15


def setUpModule() -> None:
    logging.getLogger("tvbridge").addHandler(_QUIET)


def tearDownModule() -> None:
    logging.getLogger("tvbridge").removeHandler(_QUIET)


def cfg_with(mirror=None, symbols=None, **sections):
    base = {"server": {"secret": SECRET}, "symbols": SYMBOLS if symbols is None else symbols,
            "mirror": mirror if mirror is not None else FAN}
    return config_from_dict(deep_merge(base, sections), "/tmp/tvb-fanout-none")


class FanOutConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config_from_dict({"server": {"secret": SECRET}}, "/tmp/tvb-fanout-none")
        self.assertEqual((cfg.mirror.fan_out, cfg.mirror.units_per_lot_by_symbol), ({}, {}))
        self.assertIsNone(cfg.symbols.specs["XAUUSD"].commission_per_lot_usd)
        self.assertEqual(cfg.mirror_units_per_lot("XAUUSD.h"), 100.0)
        self.assertEqual(cfg.mirror_fan_out("XAUUSD"), [])
        self.assertEqual(cfg.commission_per_lot("XAUUSD.h"), 5.0)

    def test_normalized_and_resolved(self):
        cfg = cfg_with({"fan_out": {"oanda:xauusd": ["XAUUSD.h", "XAUUSDmicro"]},
                        "units_per_lot_by_symbol": {"XAUUSDmicro": 10}})
        self.assertEqual(cfg.mirror.fan_out, {"XAUUSD": ["XAUUSD", "XAUUSDMICRO"]})
        self.assertEqual(cfg.mirror.units_per_lot_by_symbol, {"XAUUSDMICRO": 10.0})
        self.assertEqual(cfg.mirror_fan_out("XAUUSD.h"), ["XAUUSD", "XAUUSDMICRO"])
        for name in ("XAUUSDMICRO", MICRO, "xauusdmicro"):
            self.assertEqual(cfg.mirror_units_per_lot(name), 10.0, name)
            self.assertEqual(cfg.commission_per_lot(name), 0.5, name)
        for name in ("XAUUSD", GOLD):
            self.assertEqual(cfg.mirror_units_per_lot(name), 100.0, name)
            self.assertEqual(cfg.commission_per_lot(name), 5.0, name)
        self.assertEqual(cfg.commission_per_lot(None), 5.0)
        self.assertEqual(cfg.commission_per_lot("DOGEUSD"), 5.0)
        self.assertEqual(cfg.mt5_symbol("XAUUSDMICRO"), MICRO)

    def test_validation_errors(self):
        cases = (
            ({"fan_out": {"XAUUSD": ["XAUUSD", "DOGEUSD"]}}, None, "mirror.fan_out.XAUUSD: target 'DOGEUSD' has no entry"),
            ({"fan_out": {"XAUUSD": []}}, None, "mirror.fan_out.XAUUSD must list at least one target"),
            ({"fan_out": {"XAUUSD": ["XAUUSDMICRO", "XAUUSDmicro"]}}, None, "listed more than once"),
            ({"fan_out": {"XAUUSD": ["XAUUSD"], "xauusd.h": ["XAUUSD"]}}, None, "duplicate symbol 'XAUUSD'"),
            ({"fan_out": {"XAUUSD": "XAUUSD"}}, None, "mirror.fan_out.XAUUSD: expected a list"),
            ({"fan_out": ["XAUUSD"]}, None, "mirror.fan_out: expected an object"),
            ({"units_per_lot_by_symbol": {"XAUUSDMICRO": 0}}, None,
             "mirror.units_per_lot_by_symbol.XAUUSDMICRO must be > 0"),
            ({"units_per_lot_by_symbol": {"DOGEUSD": 10}}, None,
             "mirror.units_per_lot_by_symbol: 'DOGEUSD' has no entry in symbols.specs"),
            ({"units_per_lot_by_symbol": {"XAUUSDMICRO": "ten"}}, None, "expected a number"),
            ({}, {"specs": {"XAUUSDMICRO": dict(MICRO_SPEC, commission_per_lot_usd=-1)}},
             "symbols.specs.XAUUSDMICRO.commission_per_lot_usd must be >= 0"),
            ({}, {"specs": {"XAUUSDMICRO": dict(MICRO_SPEC, commission_per_lot_usd="x")}}, "expected a number"),
        )
        for mirror, symbols, text in cases:
            with self.assertRaises(ConfigError) as cm:
                cfg_with(mirror, symbols)
            self.assertIn(text, str(cm.exception), mirror)

    def test_commission_override_in_risk_and_paper(self):
        cfg = cfg_with()
        spec = cfg.spec_for(MICRO)
        self.assertAlmostEqual(risk.per_lot_loss_usd(4176.5, 4168.0, spec, 1.0, cfg.commission_per_lot(MICRO)), 85.5)
        clock.set_clock(lambda: T0)
        self.addCleanup(clock.set_clock, None)
        store = Store(":memory:")
        self.addCleanup(store.close)
        ex = PaperExecutor(cfg, store)
        ex.open_market(OrderRequest(MICRO, "buy", 2.0, 4168.0, None, 2, price_hint=4176.0))
        ex.open_market(OrderRequest(GOLD, "buy", 0.2, 4168.0, None, 2, price_hint=4176.0))
        ex.set_price_hint(MICRO, 4186.0)
        ex.close_partial(MICRO, "buy", 1.0)
        self.assertAlmostEqual(ex.read_account().balance, 50000 + 10.0 * 10 * 1.0 - 0.5 * 1.0)
        ex.close_positions(MICRO)
        self.assertAlmostEqual(ex.read_account().balance, 50000 + 10.0 * 10 * 2.0 - 0.5 * 2.0)
        ex.close_positions(GOLD)                      # the other symbol still pays risk.commission_per_lot_usd
        self.assertAlmostEqual(ex.read_account().balance, 50000 + 199.0 - 5.0 * 0.2)


class FanOutEngineTests(MirrorEngineBase):
    #: 0.25 % of 50,000 = 125 USD per child: 0.12 lots XAUUSD, 1.27 lots micro (both below the strategy's size)
    RISK = {"risk_per_trade_pct": 0.25, "max_risk_per_trade_pct": 0.25}

    def fan_cfg(self, mirror=None, risk=None, **sections):
        m = dict(FAN)
        m.update(mirror or {})
        sections.setdefault("symbols", SYMBOLS)
        return self.mirror_cfg(mirror=m, risk=risk, **sections)

    def pos(self, symbol):
        return [(p.side, p.lots) for p in self.paper_positions() if p.symbol == symbol]

    def children(self, parent_id):
        return self.row("%s@%s" % (parent_id, GOLD)), self.row("%s@%s" % (parent_id, MICRO))

    def insert(self, position, size, seconds=0, status="queued", **kw):
        d = self.payload(position, size, **kw)
        d["time"] = clock.iso(self.now + timedelta(seconds=seconds))
        sig = signals.parse_payload(d, self.cfg, self.now + timedelta(seconds=seconds))
        self.assertTrue(self.store.insert_signal(sig, status=status))
        return sig

    def test_open_partial_flat(self):
        self.start(self.fan_cfg())
        parent = self.sync("long", 28)
        self.assertEqual((parent["status"], parent["reason"]), ("done", "FANNED_OUT: XAUUSD.h, XAUUSDmicro"))
        self.assertEqual(parent["result"]["fan_out"], [parent["id"] + "@" + GOLD, parent["id"] + "@" + MICRO])
        gold, micro = self.children(parent["id"])
        self.assertEqual([(r["status"], r["reason"], r["symbol"]) for r in (gold, micro)],
                         [("done", "", GOLD), ("done", "", MICRO)])
        self.assertEqual((micro["payload"]["tv_symbol"], micro["payload"]["raw"][FAN_OUT_PARENT]),
                         ("XAUUSDMICRO", parent["id"]))
        self.assertEqual((micro["payload"]["target_units"], micro["payload"]["price"]), (28.0, 4176.5))
        # both capped by max_risk_per_trade_pct; the micro sizing uses its own commission (0.5, not 5.0)
        self.assertEqual(self.pos(GOLD), [("buy", 0.12)])
        self.assertEqual(self.pos(MICRO), [("buy", 1.27)])
        self.assertEqual(int(125 / ((8.5 * 10 + 5.0) * 1.15) * 100) / 100.0, 1.20)   # what the global 5.0 would give
        led = {r["symbol"]: r for r in self.ledger()}
        self.assertEqual(sorted(led), [GOLD, MICRO])
        self.assertEqual((led[GOLD]["lots"], led[MICRO]["lots"]), (0.12, 1.27))
        self.assertEqual((led[GOLD]["signal_id"], led[MICRO]["signal_id"]), (gold["id"], micro["id"]))
        self.assertAlmostEqual(led[GOLD]["risk_usd"], 0.12 * GOLD_PER_LOT, places=2)
        self.assertAlmostEqual(led[MICRO]["risk_usd"], 1.27 * MICRO_PER_LOT, places=2)
        self.assertEqual(micro["result"]["plan"]["details"]["mirror"]["wanted_lots"], 2.8)
        self.assertAlmostEqual(float(self.store.get_kv("mirror_scale:XAUUSD")), 0.12 / 0.28)
        self.assertAlmostEqual(float(self.store.get_kv("mirror_scale:XAUUSDMICRO")), 1.27 / 2.8)
        self.assertEqual(len(self.events("sync_fanned_out")), 1)
        self.assertEqual(self.store.get_kv("halted") or "", "")

        # partial exit 28 -> 19.6 units: both reduced with their own scale
        parent = self.sync("long", 19.6, order_id="TP", prev_position="long", prev_size=28)
        self.assertTrue(parent["reason"].startswith("FANNED_OUT"))
        gold, micro = self.children(parent["id"])
        self.assertEqual((gold["status"], micro["status"]), ("done", "done"))
        self.assertEqual((gold["result"]["partial_lots"], micro["result"]["partial_lots"]), (0.04, 0.39))
        self.assertEqual(self.pos(GOLD), [("buy", 0.08)])
        self.assertEqual(self.pos(MICRO), [("buy", 0.88)])
        led = {r["symbol"]: r for r in self.ledger()}
        self.assertAlmostEqual(led[GOLD]["lots"], 0.08)
        self.assertAlmostEqual(led[MICRO]["lots"], 0.88)

        # flat: both closed
        parent = self.sync("flat", 0, order_id="Exit", prev_position="long", prev_size=19.6)
        gold, micro = self.children(parent["id"])
        self.assertEqual((gold["status"], micro["status"]), ("done", "done"))
        self.assertEqual(self.paper_positions(), [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.store.kv_with_prefix("mirror_scale:"), {})
        self.assertEqual(len(self.all_ledger()), 2)

    def test_reversal_on_both(self):
        self.start(self.fan_cfg())
        self.sync("long", 28)
        parent = self.sync("short", 28, order_id="Short", prev_position="long", prev_size=28)
        gold, micro = self.children(parent["id"])
        self.assertEqual((gold["status"], micro["status"]), ("done", "done"), (gold["reason"], micro["reason"]))
        self.assertEqual(self.pos(GOLD), [("sell", 0.12)])
        self.assertEqual(self.pos(MICRO), [("sell", 1.27)])
        self.assertEqual(sorted((r["symbol"], r["side"]) for r in self.ledger()), [(GOLD, "sell"), (MICRO, "sell")])
        self.assertEqual(len(self.all_ledger()), 4)

    def test_child_alert_and_other_symbols_do_not_fan_out(self):
        self.start(self.fan_cfg())
        # an alert for the micro symbol itself has no fan_out entry: a plain sync
        row = self.sync("long", 28, symbol="XAUUSDMICRO")
        self.assertEqual((row["status"], row["reason"]), ("done", ""))
        self.assertEqual(self.pos(MICRO), [("buy", 1.27)])
        self.assertEqual(self.pos(GOLD), [])
        # a child is never fanned out again, even though its tv_symbol has a fan_out entry
        sig = self.store.load_signal(self.sync("flat", 0, order_id="x")["id"] + "@" + GOLD)
        self.assertEqual(self.engine._fan_out_children(sig), [])

    def test_only_target_is_the_micro(self):
        self.start(self.fan_cfg(mirror={"fan_out": {"XAUUSD": ["XAUUSDmicro"]}}))
        parent = self.sync("long", 28)
        self.assertEqual(parent["reason"], "FANNED_OUT: XAUUSDmicro")
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([], [("buy", 1.27)]))

    def test_mirror_disabled_parent_has_no_children(self):
        self.start(self.fan_cfg(mirror={"enabled": False}))
        parent = self.sync("long", 28)
        self.assertEqual(parent["status"], "rejected")
        self.assertTrue(parent["reason"].startswith("MIRROR_DISABLED"))
        self.assertEqual(self.children(parent["id"]), (None, None))
        self.assertEqual(self.paper_positions(), [])

    def test_idea_risk_stop_uses_the_childs_contract_size(self):
        # 50,000 x 0.45 % = 225 USD over 28 oz -> 8.04 on both: 0.28 lots x 100 oz and 2.8 lots x 10 oz
        self.start(self.fan_cfg(mirror={"idea_risk_pct": 0.45}, risk={}))
        parent = self.sync("long", 28)
        gold, micro = self.children(parent["id"])
        self.assertEqual((gold["result"]["order"]["sl_distance"], micro["result"]["order"]["sl_distance"]),
                         (8.04, 8.04))
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([("buy", 0.28)], [("buy", 2.8)]))
        led = {r["symbol"]: r for r in self.ledger()}
        self.assertAlmostEqual(led[GOLD]["risk_usd"], 0.28 * (8.04 * 100 + 5.0) * 1.15, places=2)
        self.assertAlmostEqual(led[MICRO]["risk_usd"], 2.8 * (8.04 * 10 + 0.5) * 1.15, places=2)

    def test_total_open_risk_rejects_the_second_child_only(self):
        # cap 0.4 % = 200 USD: the first child books 117.99, the second (124.87) no longer fits
        self.start(self.fan_cfg(risk=dict(self.RISK, max_total_open_risk_pct=0.4)))
        parent = self.sync("long", 28)
        self.assertTrue(parent["reason"].startswith("FANNED_OUT"))
        gold, micro = self.children(parent["id"])
        self.assertEqual((gold["status"], gold["reason"]), ("done", ""))
        self.assertEqual(micro["status"], "rejected")
        self.assertTrue(micro["reason"].startswith("TOTAL_OPEN_RISK"), micro["reason"])
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([("buy", 0.12)], []))
        self.assertEqual([r["symbol"] for r in self.ledger()], [GOLD])
        self.assertIsNone(self.store.get_kv("mirror_scale:XAUUSDMICRO"))
        self.assertTrue(any(MICRO in n and "TOTAL_OPEN_RISK" in n for n in map(str, self.notes())), self.notes())
        # the exit still closes what is open
        self.sync("flat", 0, order_id="Exit")
        self.assertEqual(self.paper_positions(), [])

    def test_two_queued_parents_only_the_newest_acts(self):
        self.build(self.fan_cfg())
        old = self.insert("long", 28)
        new = self.insert("short", 14, seconds=1, order_id="Short")
        self.now = self.now + timedelta(seconds=2)
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        self.assertEqual(self.row(old.id)["status"], "expired")
        self.assertTrue(self.row(old.id)["reason"].startswith("SUPERSEDED_BY_SYNC"))
        self.assertEqual(self.children(old.id), (None, None))
        self.assertTrue(self.row(new.id)["reason"].startswith("FANNED_OUT"))
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([("sell", 0.12)], [("sell", 1.27)]))
        self.assertEqual(len(self.all_ledger()), 2)

    def test_children_of_an_older_parent_are_superseded_by_a_newer_parent(self):
        # the old parent already fanned out (children queued) when a newer alert was stored
        self.build(self.fan_cfg())
        old = self.insert("long", 28, status="done")
        for child in self.engine._fan_out_children(old):
            self.assertTrue(self.store.insert_signal(child))
        new = self.insert("long", 14, seconds=1, order_id="Long2")
        self.now = self.now + timedelta(seconds=2)
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        for row in self.children(old.id):
            self.assertEqual(row["status"], "expired", row)
            self.assertIn(new.id, row["reason"])
        self.assertEqual([r["status"] for r in self.children(new.id)], ["done", "done"])
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([("buy", 0.12)], [("buy", 1.27)]))
        self.assertEqual(len(self.all_ledger()), 2)       # the old children never traded
        self.assertAlmostEqual(float(self.store.get_kv("mirror_scale:XAUUSDMICRO")), 1.27 / 1.4)

    def test_same_instant_parents(self):
        self.build(self.fan_cfg())
        a = self.insert("long", 28, order_id="A")
        b = self.insert("long", 14, order_id="B")
        self.now = self.now + timedelta(seconds=2)
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        self.assertEqual(self.row(a.id)["status"], "expired")
        self.assertEqual([r["status"] for r in self.children(b.id)], ["done", "done"])
        self.assertEqual(len(self.all_ledger()), 2)

    def test_refan_out_after_a_crash_is_idempotent(self):
        self.build(self.fan_cfg())
        parent = self.insert("long", 28)                       # still queued: the crash came before "done"
        gold_child, micro_child = self.engine._fan_out_children(parent)
        self.store.insert_signal(gold_child)
        self.store.set_signal_status(gold_child.id, "done", "IN_SYNC: handled before the crash")
        self.now = self.now + timedelta(seconds=2)
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        self.assertEqual(self.row(parent.id)["reason"], "FANNED_OUT: XAUUSD.h, XAUUSDmicro")
        gold, micro = self.children(parent.id)
        self.assertEqual((gold["status"], gold["reason"]), ("done", "IN_SYNC: handled before the crash"))
        self.assertEqual((micro["status"], micro["reason"]), ("done", ""))
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([], [("buy", 1.27)]))   # the done child did not re-run
        # a duplicate submit of the finished parent changes nothing
        self.engine.submit_signal(parent)
        self.assertTrue(self.engine.wait_idle(15))
        self.assertEqual(len(self.all_ledger()), 1)

    def test_processing_parent_with_all_children_is_finished_without_a_halt(self):
        self.build(self.fan_cfg())
        parent = self.insert("long", 28, status="processing")
        for child in self.engine._fan_out_children(parent):
            self.store.insert_signal(child)
        self.now = self.now + timedelta(seconds=2)
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        row = self.row(parent.id)
        self.assertEqual((row["status"], row["reason"]), ("done", "FANNED_OUT: XAUUSD.h, XAUUSDmicro"))
        self.assertEqual(self.store.get_kv("halted") or "", "")
        self.assertEqual([r["status"] for r in self.children(parent.id)], ["done", "done"])
        self.assertEqual((self.pos(GOLD), self.pos(MICRO)), ([("buy", 0.12)], [("buy", 1.27)]))

    def test_processing_parent_with_a_missing_child_is_interrupted(self):
        self.build(self.fan_cfg())
        parent = self.insert("long", 28, status="processing")
        self.store.insert_signal(self.engine._fan_out_children(parent)[0])
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        row = self.row(parent.id)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["reason"].startswith("INTERRUPTED"))
        self.assertTrue((self.store.get_kv("halted") or "").startswith("INTERRUPTED"))
        # the stored child still runs, but entries are halted: nothing is opened
        gold = self.children(parent.id)[0]
        self.assertEqual(gold["status"], "rejected")
        self.assertTrue(gold["reason"].startswith("HALTED"), gold["reason"])
        self.assertEqual(self.paper_positions(), [])

    def test_processing_child_is_interrupted(self):
        self.build(self.fan_cfg())
        parent = self.insert("long", 28, status="done")
        child = self.engine._fan_out_children(parent)[1]
        self.store.insert_signal(child, status="processing")
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        self.assertTrue(self.row(child.id)["reason"].startswith("INTERRUPTED"))
        self.assertTrue((self.store.get_kv("halted") or "").startswith("INTERRUPTED"))

    def test_status_shows_the_targets(self):
        self.start(self.fan_cfg())
        st = build_status(self.cfg, self.store)
        self.assertEqual(st["mirror"]["fan_out"], {"XAUUSD": [{"symbol": GOLD, "units_per_lot": 100.0},
                                                              {"symbol": MICRO, "units_per_lot": 10.0}]})
        self.assertEqual(st["mirror"]["units_per_lot_by_symbol"], {"XAUUSDMICRO": 10.0})
        self.assertEqual(self.engine.status()["mirror"]["fan_out"], st["mirror"]["fan_out"])
        line = [ln for ln in cli._format_status(self.engine.status()).splitlines() if ln.startswith("mirror:")][0]
        self.assertIn("fan-out XAUUSD -> XAUUSD.h (100.0 units per lot), XAUUSDmicro (10.0 units per lot)", line)


class FanOutStoreTests(unittest.TestCase):
    def test_newer_sync_signal_exclude_prefix(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        cfg = cfg_with(dict(FAN, enabled=True))

        def add(seconds, order_id):
            d = {"time": clock.iso(T0 + timedelta(seconds=seconds)), "symbol": "XAUUSD", "action": "sync",
                 "position": "long", "size": 28, "price": 1.1, "order_id": order_id}
            sig = signals.parse_payload(d, cfg, T0 + timedelta(seconds=30))
            store.insert_signal(sig)
            return sig

        a = add(0, "a")
        child = dataclasses.replace(a, id=a.id + "@" + GOLD)
        store.insert_signal(child)
        self.assertEqual(store.newer_sync_signal(a.id)["id"], child.id)
        self.assertIsNone(store.newer_sync_signal(a.id, a.id + "@"))
        b = add(0, "b")                                   # same instant, stored later
        self.assertEqual(store.newer_sync_signal(a.id, a.id + "@")["id"], b.id)
        self.assertEqual(store.newer_sync_signal(child.id)["id"], b.id)


# ---------------------------------------------------------------------------------- GUI (fake driver)


def gui_pos(ticket, symbol, lots, side="buy"):
    return {"ticket": str(ticket), "symbol": symbol, "side": side, "lots": lots, "open_price": 4176.97,
            "sl": 4168.47, "tp": 0.0, "price": 4176.97, "profit": 0.0}


class FanOutGuiTests(unittest.TestCase):
    """The Toolbox rows of "XAUUSD" and "XAUUSDmicro" (one name is a prefix of the other) are told apart."""

    def setUp(self):
        clock.set_clock(lambda: datetime(2026, 10, 1, 9, 56, tzinfo=timezone.utc))
        self.addCleanup(clock.set_clock, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = config_from_dict({
            "server": {"secret": SECRET},
            "account": {"account_login": "12345678", "server_name": "HantecMarketsMU-MT5"},
            "symbols": {"suffix": "", "specs": {"XAUUSDMICRO": MICRO_SPEC}, "map": {"XAUUSDMICRO": MICRO}},
            "mirror": dict(FAN, enabled=True)}, self.home)

    def make(self, **fake_kw):
        fake_kw.setdefault("close_price", 4180.10)
        self.fake = FakeMt5Driver(positions=[gui_pos(1001, "XAUUSD", 0.28), gui_pos(1002, MICRO, 2.8)], **fake_kw)
        return Mt5GuiExecutor(self.cfg, self.fake, make_calibration(), rehearsal=False,
                              shots_dir=self.home / "shots")

    def left(self):
        return [(p["ticket"], p["symbol"], p["lots"]) for p in self.fake.positions]

    def test_known_symbols(self):
        self.assertEqual((self.cfg.mt5_symbol("XAUUSD"), self.cfg.mt5_symbol("XAUUSDMICRO")), ("XAUUSD", MICRO))
        self.assertIn("XAUUSD", self.cfg.known_mt5_symbols())
        self.assertIn(MICRO, self.cfg.known_mt5_symbols())

    def test_parse_position_rows_tells_them_apart(self):
        self.make()
        for known in (["XAUUSD", MICRO], [MICRO, "XAUUSD"]):
            rows = parse.parse_position_rows(self.fake._main_items(), known)
            self.assertEqual(sorted((p.ticket, p.symbol, p.lots) for p, _ in rows),
                             [("1001", "XAUUSD", 0.28), ("1002", MICRO, 2.8)])
        self.assertIsNone(parse.find_symbol("XAUUSDmicro 1002 buy 2.80", ["XAUUSD"]))
        self.assertEqual(parse.find_symbol("XAUUSDmicro 1002 buy 2.80", ["XAUUSD", MICRO])[0], MICRO)
        self.assertEqual(parse.find_symbol("XAUUSD 1001 buy 0.28", ["XAUUSD", MICRO])[0], "XAUUSD")

    def test_read_account(self):
        ex = self.make()
        self.assertEqual(sorted((p.symbol, p.lots) for p in ex.read_account().positions),
                         [("XAUUSD", 0.28), (MICRO, 2.8)])

    def test_closing_xauusd_leaves_the_micro_row(self):
        ex = self.make()
        res = ex.close_positions("XAUUSD")
        self.assertEqual([(r.status, r.ticket) for r in res], [("filled", "1001")], [r.message for r in res])
        self.assertEqual(self.fake.closed_tickets, ["1001"])
        self.assertEqual(self.left(), [("1002", MICRO, 2.8)])

    def test_closing_the_micro_leaves_the_xauusd_row(self):
        ex = self.make()
        res = ex.close_positions(MICRO)
        self.assertEqual([(r.status, r.ticket) for r in res], [("filled", "1002")], [r.message for r in res])
        self.assertEqual(self.fake.closed_tickets, ["1002"])
        self.assertEqual(self.left(), [("1001", "XAUUSD", 0.28)])

    def test_partial_closes_hit_the_right_row(self):
        ex = self.make()
        res = ex.close_partial(MICRO, "buy", 0.9)
        self.assertEqual([(r.status, r.ticket, r.lots) for r in res], [("filled", "1002", 0.9)],
                         [r.message for r in res])
        res = ex.close_partial("XAUUSD", "buy", 0.09)
        self.assertEqual([(r.status, r.ticket, r.lots) for r in res], [("filled", "1001", 0.09)],
                         [r.message for r in res])
        self.assertEqual(self.fake.partial_closes, [("1002", 0.9), ("1001", 0.09)])
        self.assertEqual([(t, s, round(l, 2)) for t, s, l in self.left()],
                         [("1001", "XAUUSD", 0.19), ("1002", MICRO, 1.9)])


if __name__ == "__main__":
    unittest.main()


class ResyncTests(MirrorEngineBase):
    """mirror.resync_lockout_min: a fan-out leg MT5 stopped out while the strategy still holds the
    trade is opened again after the lockout (paper engine; the micro leg is the one that stops)."""

    RISK = FanOutEngineTests.RISK
    RESYNC = {"resync_lockout_min": 10, "resync_max_per_trade": 1}
    fan_cfg = FanOutEngineTests.fan_cfg
    pos = FanOutEngineTests.pos
    children = FanOutEngineTests.children

    def resync_cfg(self, mirror=None, **sections):
        m = dict(self.RESYNC)
        m.update(mirror or {})
        return self.fan_cfg(mirror=m, **sections)

    def poll(self, n=2):
        """``n`` account reads (RECONCILE_MISSES reads prove a server-side close)."""
        for _ in range(n):
            self.assertTrue(self.engine.request_account_poll())
            self.assertTrue(self.engine.wait_idle(10))

    def stop_out_micro(self):
        """Move the paper micro market through the long's stop: MT5 closes it, gold stays open."""
        self.executor.set_price_hint(MICRO, 4160.0)
        self.poll()
        self.assertEqual(self.pos(MICRO), [])
        self.assertEqual(self.pos(GOLD), [("buy", 0.12)])
        closed = [r for r in self.all_ledger() if r["symbol"] == MICRO and r["status"] == "closed"]
        self.assertEqual([r["close_reason"] for r in closed], ["closed_on_server"])
        return closed[0]

    def pending(self):
        return build_status(self.cfg, self.store)["resync_pending"]

    def wait_resync_done(self, sig_id):
        self.wait_for(lambda: (self.row(sig_id) or {}).get("status") not in (None, "queued", "processing"),
                      msg="re-sync %s did not run" % sig_id)
        return self.row(sig_id)

    def test_stopped_out_leg_is_reopened_after_the_lockout(self):
        self.start(self.resync_cfg())
        parent = self.sync("long", 28)
        row = self.stop_out_micro()
        # scheduled: stored, visible in the status, nothing opened yet
        self.assertEqual(len(self.events("resync_scheduled")), 1)
        pend = self.pending()
        self.assertEqual([(p["symbol"], p["side"], p["n"], p["price"]) for p in pend], [(MICRO, "buy", 1, row["sl"])])
        self.assertEqual(pend[0]["not_before"], clock.iso(self.now + timedelta(minutes=10)))
        self.assertEqual(pend[0]["root"], parent["id"])
        self.assertEqual(self.pos(MICRO), [])
        self.tick(5 * 60)
        time.sleep(0.2)
        self.assertEqual(self.pos(MICRO), [])             # the lockout is not over
        # the lockout ends: a new sync entry built from the newest alert opens the leg again
        self.tick(5 * 60 + 1)
        rid = "%s@%s#r1" % (parent["id"], MICRO)
        res = self.wait_resync_done(rid)
        self.assertEqual((res["status"], res["reason"]), ("done", ""))
        self.assertEqual(res["payload"]["raw"][FAN_OUT_PARENT], parent["id"])
        self.assertEqual((res["payload"]["raw"][RESYNC_ROOT], res["payload"]["raw"][RESYNC_N]), (parent["id"], 1))
        self.assertEqual(res["payload"]["raw"][RESYNC_OF], row["signal_id"])
        self.assertEqual((res["payload"]["price"], res["payload"]["fired_at"]), (row["sl"], clock.iso(self.now)))
        self.assertEqual((res["payload"]["target_side"], res["payload"]["target_units"]), ("buy", 28.0))
        self.assertEqual(self.pos(MICRO), [("buy", 1.26)])      # sized on the balance after the loss
        self.assertEqual(self.pos(GOLD), [("buy", 0.12)])
        led = {r["symbol"]: r for r in self.ledger()}
        self.assertEqual(led[MICRO]["signal_id"], rid)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.events("resync")), 1)
        self.assertEqual(self.store.kv_with_prefix("resync:"), {})
        # the strategy exits: both legs close, the re-synced one too
        flat = self.sync("flat", 0, order_id="Exit", prev_position="long", prev_size=28)
        gold, micro = self.children(flat["id"])
        self.assertEqual((gold["status"], micro["status"]), ("done", "done"))
        self.assertEqual(self.paper_positions(), [])
        self.assertEqual(self.ledger(), [])

    def test_second_stop_out_is_not_resynced(self):
        self.start(self.resync_cfg())
        parent = self.sync("long", 28)
        self.stop_out_micro()
        self.tick(10 * 60 + 1)
        rid = "%s@%s#r1" % (parent["id"], MICRO)
        self.assertEqual(self.wait_resync_done(rid)["status"], "done")
        self.assertEqual(self.pos(MICRO), [("buy", 1.26)])
        # stopped out again: the limit (1 per trade) is reached
        self.executor.set_price_hint(MICRO, 4150.0)
        self.poll()
        self.assertEqual(self.pos(MICRO), [])
        self.assertEqual(len(self.events("resync_limit")), 1)
        self.assertEqual(len(self.events("resync_scheduled")), 1)
        self.assertEqual(self.pending(), [])
        self.assertTrue(any("not re-synced" in m for m in self.notes("warn")))

    def test_cancelled_when_the_strategy_left_the_trade(self):
        self.start(self.resync_cfg())
        parent = self.sync("long", 28)
        self.stop_out_micro()
        self.assertEqual(len(self.pending()), 1)
        # the strategy exits during the lockout: gold closes, the micro is already flat
        self.tick(60)
        flat = self.sync("flat", 0, order_id="Exit", prev_position="long", prev_size=28)
        gold, micro = self.children(flat["id"])
        self.assertEqual((gold["status"], micro["status"]), ("done", "done"))
        self.assertTrue(micro["reason"].startswith("IN_SYNC"), micro["reason"])
        self.tick(10 * 60)
        self.wait_for(lambda: self.events("resync_cancelled"), msg="re-sync not cancelled")
        self.assertIn("says flat, not long", self.events("resync_cancelled")[0]["message"])
        self.assertEqual(self.paper_positions(), [])
        self.assertIsNone(self.row("%s@%s#r1" % (parent["id"], MICRO)))
        self.assertEqual(self.pending(), [])

    def test_reversal_during_the_lockout_cancels_too(self):
        self.start(self.resync_cfg())
        self.sync("long", 28)
        self.stop_out_micro()
        self.tick(60)
        short = self.sync("short", 28, price=4160.0, order_id="Short", prev_position="long", prev_size=28)
        gold, micro = self.children(short["id"])
        self.assertEqual((gold["status"], micro["status"]), ("done", "done"))
        self.assertEqual(self.pos(MICRO), [("sell", 1.26)])
        self.tick(10 * 60)
        self.wait_for(lambda: self.events("resync_cancelled"), msg="re-sync not cancelled")
        self.assertIn("says short, not long", self.events("resync_cancelled")[0]["message"])
        self.assertEqual(self.pos(MICRO), [("sell", 1.26)])    # untouched
        self.assertEqual(len(self.ledger()), 2)

    def test_partial_exit_during_the_lockout_resyncs_the_smaller_size(self):
        self.start(self.resync_cfg())
        parent = self.sync("long", 28)
        self.stop_out_micro()
        self.tick(60)
        part = self.sync("long", 19.6, order_id="TP", prev_position="long", prev_size=28)
        gold, micro = self.children(part["id"])
        self.assertEqual(gold["status"], "done")
        self.assertEqual(micro["status"], "rejected")          # MIRROR_NOT_AN_ENTRY: a partial exit on a flat leg
        self.tick(10 * 60)
        rid = "%s@%s#r1" % (part["id"], MICRO)                 # built from the NEWEST alert, not the first
        res = self.wait_resync_done(rid)
        self.assertEqual((res["status"], res["reason"]), ("done", ""))
        self.assertEqual(res["payload"]["target_units"], 19.6)
        self.assertEqual(res["result"]["plan"]["details"]["mirror"]["wanted_lots"], 1.96)
        self.assertEqual(self.pos(MICRO), [("buy", 1.26)])      # 1.96 wanted, capped by the risk guard
        self.assertIsNone(self.row("%s@%s#r1" % (parent["id"], MICRO)))

    def test_close_at_a_profit_is_not_a_stop_out(self):
        self.start(self.resync_cfg(mirror={"tp_distance": 12.0}))
        self.sync("long", 28)
        self.executor.set_price_hint(MICRO, 4190.0)           # take-profit hit
        self.poll()
        self.assertEqual(self.pos(MICRO), [])
        self.assertEqual(len(self.events("resync_skipped")), 1)
        self.assertIn("not a stop-out", self.events("resync_skipped")[0]["message"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.events("resync_scheduled"), [])

    def test_off_by_default(self):
        self.start(self.fan_cfg())
        self.assertEqual(self.cfg.mirror.resync_lockout_min, 0.0)
        self.sync("long", 28)
        self.stop_out_micro()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.events("resync_scheduled"), [])
        self.tick(11 * 60)
        time.sleep(0.2)
        self.assertEqual(self.pos(MICRO), [])

    def test_pending_resync_survives_a_restart(self):
        self.start(self.resync_cfg())
        parent = self.sync("long", 28)
        self.stop_out_micro()
        self.assertEqual(len(self.pending()), 1)
        self.engine.stop(timeout=5)
        self.tick(3 * 60)
        self.build(self.cfg)
        self.engine.start()
        self.assertTrue(self.engine.wait_idle(15))
        self.assertEqual(len(self.pending()), 1)              # still pending, not run early
        self.assertEqual(self.pos(MICRO), [])
        self.tick(7 * 60 + 1)
        rid = "%s@%s#r1" % (parent["id"], MICRO)
        self.assertEqual(self.wait_resync_done(rid)["status"], "done")
        self.assertEqual(self.pos(MICRO), [("buy", 1.26)])
        self.assertEqual(self.pending(), [])

    def test_resync_goes_through_the_risk_guard(self):
        self.start(self.resync_cfg())
        parent = self.sync("long", 28)
        self.stop_out_micro()
        self.store.set_kv("paused", "1")
        self.store.set_kv("pause_reason", "test")
        self.tick(10 * 60 + 1)
        rid = "%s@%s#r1" % (parent["id"], MICRO)
        res = self.wait_resync_done(rid)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("PAUSED", res["reason"])
        self.assertEqual(self.pos(MICRO), [])
        self.assertEqual(self.pending(), [])                   # not retried

    def test_status_lines(self):
        self.start(self.resync_cfg())
        self.sync("long", 28)
        self.stop_out_micro()
        text = cli._format_status(self.engine.status())
        self.assertIn("re-sync a stopped-out leg after 10 min (max 1 per trade)", text)
        self.assertIn("re-sync:     buy %s pending" % MICRO, text)

    def test_config_validation(self):
        with self.assertRaises(ConfigError):
            self.fan_cfg(mirror={"resync_lockout_min": -1})
        with self.assertRaises(ConfigError):
            self.fan_cfg(mirror={"resync_max_per_trade": 0})
        with self.assertRaises(ConfigError):
            self.fan_cfg(mirror={"resync_min_loss_frac": 0})
        cfg = self.fan_cfg(mirror={"resync_lockout_min": 2.5, "resync_min_loss_frac": 1})
        self.assertEqual((cfg.mirror.resync_lockout_min, cfg.mirror.resync_max_per_trade), (2.5, 1))
