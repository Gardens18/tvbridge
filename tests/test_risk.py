"""Tests for tvbridge.risk (SPEC section 9).

All money figures use the real Hantec Endurance $50,000 account with the default config
(4% daily / 8% max loss, 1% internal buffers, 0.3% kill buffer, 0.5% risk per trade,
$5/lot commission, 15% slippage buffer). Times are UTC; the broker server is GMT+3.
"""

import math
import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tvbridge import risk
from tvbridge.config import SymbolSpec, config_from_dict
from tvbridge.models import AccountSnapshot, ObservedPosition, Signal, TradePlan
from tvbridge.risk import (
    Floors, RiskState, check_trading_window, compute_floors, estimate_reference, evaluate_kill, per_lot_loss_usd,
    plan_entry, quote_to_usd, round_lots_down,
)

UTC = timezone.utc
HOME = Path("/nonexistent/tvbridge-test-home")
SECRET = "s" * 32

# Wednesday 2026-09-30 07:00:00Z == 10:00 server time (GMT+3): inside the trading window.
NOW = datetime(2026, 9, 30, 7, 0, 0, tzinfo=UTC)

KNOWN_CODES = {
    "HALTED", "PAUSED", "BAD_ACTION", "NO_SPEC", "WINDOW", "STALE_SIGNAL", "NO_SNAPSHOT", "SNAPSHOT_STALE",
    "NO_DAY_REFERENCE", "UNTRACKED_POSITIONS", "NO_PRICE", "SL_MISSING", "SL_WRONG_SIDE", "TP_WRONG_SIDE",
    "SL_TOO_TIGHT", "OPPOSITE_OPEN", "PYRAMIDING", "MAX_POSITIONS", "MAX_TRADES_DAY", "NO_FX_RATE",
    "SIZE_TOO_SMALL", "TOTAL_OPEN_RISK", "BELOW_ENTRY_FLOOR", "WORST_CASE", "POSITIONS_UNCERTAIN",
    "SL_MISSING_ON_SERVER",
}


def make_cfg(risk_over=None, specs=None, **sections):
    d = {"server": {"secret": SECRET}}
    if risk_over:
        d["risk"] = dict(risk_over)
    sym_specs = {"EURGBP": {"quote": "GBP"}}
    if specs:
        sym_specs.update(specs)
    d["symbols"] = {"specs": sym_specs}
    d.update(sections)
    return config_from_dict(d, HOME)


def make_signal(**kw):
    base = dict(
        id="buy:t1", action="buy", tv_symbol="EURUSD", symbol="EURUSD.h", side=None,
        price=1.08345, sl=1.08100, tp=1.08900, risk_pct=None, quote_usd=None,
        fired_at=NOW - timedelta(seconds=5), received_at=NOW - timedelta(seconds=4),
        strategy="test", comment="tvb",
    )
    base.update(kw)
    return Signal(**base)


def make_snapshot(balance=50000.0, equity=50000.0, age_s=10.0, positions=None):
    return AccountSnapshot(ts=NOW - timedelta(seconds=age_s), balance=balance, equity=equity,
                           positions=positions, source="paper")


def ledger_row(symbol, side, lots=0.5, risk_usd=100.0, ticket=None, entry_price=None):
    return {"pid": 1, "signal_id": "x", "symbol": symbol, "side": side, "lots": lots, "entry_price": entry_price,
            "sl": None, "tp": None, "risk_usd": risk_usd, "opened_at": "2026-09-30T06:00:00Z", "status": "open",
            "ticket": ticket}


def make_state(cfg, **kw):
    base = dict(
        now=NOW,
        snapshot=make_snapshot(),
        floors=compute_floors(50000.0, cfg),
        open_positions=[],
        observed_positions=[],
        untracked_positions=[],
        trades_today=0,
        paused=False,
        halted="",
    )
    base.update(kw)
    return RiskState(**base)


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def plan(self, sig=None, cfg=None, **state_kw):
        cfg = cfg or self.cfg
        return plan_entry(sig or make_signal(), make_state(cfg, **state_kw), cfg)

    def assertCode(self, plan, code):
        self.assertIsInstance(plan, TradePlan)
        self.assertFalse(plan.approved, "expected %s, got approved plan %r" % (code, plan))
        self.assertEqual(plan.code, code, plan.reason)
        self.assertTrue(plan.reason.startswith(code + ": "), plan.reason)
        self.assertGreater(len(plan.reason), len(code) + 2)
        # A rejected plan never carries a size.
        self.assertEqual(plan.lots, 0.0)
        self.assertEqual(plan.risk_usd, 0.0)
        return plan

    def assertApproved(self, plan):
        self.assertTrue(plan.approved, plan.reason)
        self.assertEqual(plan.reason, "")
        self.assertGreater(plan.lots, 0)
        return plan


# --------------------------------------------------------------------------- floors


class FloorsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def assertFloors(self, f, **expected):
        for k, v in expected.items():
            self.assertAlmostEqual(getattr(f, k), v, places=6, msg=k)

    def test_50k_default_floors(self):
        f = compute_floors(50000, self.cfg)
        self.assertIsInstance(f, Floors)
        self.assertFloors(f, reference=50000, hard_daily_floor=48000, hard_max_floor=46000,
                          internal_daily_floor=48500, internal_max_floor=46500, entry_floor=48500,
                          kill_floor=48150)

    def test_reference_52000_daily_floor_above_max_floor(self):
        # 52000 * 0.96 = 49920 > static 46000, so the daily floors dominate.
        f = compute_floors(52000, self.cfg)
        self.assertFloors(f, reference=52000, hard_daily_floor=49920, hard_max_floor=46000,
                          internal_daily_floor=50440, internal_max_floor=46500, entry_floor=50440,
                          kill_floor=49920 + 150)
        self.assertGreater(f.hard_daily_floor, f.hard_max_floor)

    def test_readme_example_51000(self):
        f = compute_floors(51000, self.cfg)
        self.assertFloors(f, hard_daily_floor=48960, internal_daily_floor=49470, kill_floor=49110)

    def test_low_reference_max_floor_dominates(self):
        # After a drawdown the static floors are the binding ones.
        f = compute_floors(47000, self.cfg)
        self.assertFloors(f, hard_daily_floor=45120, hard_max_floor=46000, internal_daily_floor=45590,
                          internal_max_floor=46500, entry_floor=46500, kill_floor=46150)

    def test_kill_floor_below_entry_floor_by_default(self):
        f = compute_floors(50000, self.cfg)
        self.assertLess(f.kill_floor, f.entry_floor)

    def test_invalid_reference_raises(self):
        for bad in (0, -1, float("nan"), float("inf"), None):
            with self.assertRaises(ValueError):
                compute_floors(bad, self.cfg)

    def test_to_dict(self):
        d = compute_floors(50000, self.cfg).to_dict()
        self.assertEqual(set(d), {"reference", "hard_daily_floor", "hard_max_floor", "internal_daily_floor",
                                  "internal_max_floor", "entry_floor", "kill_floor"})


# --------------------------------------------------------------------------- primitives


class QuoteToUsdTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_usd_quote(self):
        self.assertEqual(quote_to_usd("EURUSD", self.cfg.spec_for("EURUSD"), 1.08, None), 1.0)
        self.assertEqual(quote_to_usd("XAUUSD", self.cfg.spec_for("XAUUSD"), 2650.0, 99.0), 1.0)

    def test_usd_base_uses_inverse_price(self):
        self.assertAlmostEqual(quote_to_usd("USDJPY", self.cfg.spec_for("USDJPY"), 150.0, None), 1 / 150.0)
        self.assertAlmostEqual(quote_to_usd("USDCAD", self.cfg.spec_for("USDCAD"), 1.36, None), 1 / 1.36)
        self.assertAlmostEqual(quote_to_usd("usdchf", self.cfg.spec_for("USDCHF"), 0.8, None), 1.25)

    def test_cross_needs_signal_rate(self):
        spec = self.cfg.spec_for("EURGBP")
        self.assertEqual(spec.quote, "GBP")
        self.assertIsNone(quote_to_usd("EURGBP", spec, 0.865, None))
        self.assertIsNone(quote_to_usd("EURGBP", spec, 0.865, 0.0))
        self.assertIsNone(quote_to_usd("EURGBP", spec, 0.865, -1.27))
        self.assertIsNone(quote_to_usd("EURGBP", spec, 0.865, float("nan")))
        self.assertEqual(quote_to_usd("EURGBP", spec, 0.865, 1.27), 1.27)

    def test_usd_base_with_bad_price_falls_back(self):
        spec = self.cfg.spec_for("USDJPY")
        self.assertIsNone(quote_to_usd("USDJPY", spec, 0.0, None))
        self.assertEqual(quote_to_usd("USDJPY", spec, 0.0, 0.0067), 0.0067)


class PerLotLossTests(unittest.TestCase):
    def test_values(self):
        cfg = make_cfg()
        self.assertAlmostEqual(per_lot_loss_usd(1.10000, 1.09800, cfg.spec_for("EURUSD"), 1.0, 5.0), 205.0)
        self.assertAlmostEqual(per_lot_loss_usd(2640.0, 2650.0, cfg.spec_for("XAUUSD"), 1.0, 5.0), 1005.0)
        self.assertAlmostEqual(per_lot_loss_usd(150.0, 149.5, cfg.spec_for("USDJPY"), 1 / 150.0, 0.0),
                               50000 / 150.0)


class RoundLotsDownTests(unittest.TestCase):
    def test_never_rounds_up(self):
        self.assertEqual(round_lots_down(0.29999999, 0.01, 0.01), 0.29)
        self.assertEqual(round_lots_down(0.869565, 0.01, 0.01), 0.86)
        self.assertEqual(round_lots_down(0.999999999, 0.01, 0.01), 0.99)

    def test_exact_multiples_unchanged(self):
        self.assertEqual(round_lots_down(0.3, 0.01, 0.01), 0.3)
        self.assertEqual(round_lots_down(1.0, 0.01, 0.01), 1.0)
        self.assertEqual(round_lots_down(5.0, 0.01, 0.01), 5.0)
        self.assertEqual(round_lots_down(0.01, 0.01, 0.01), 0.01)

    def test_other_steps(self):
        self.assertEqual(round_lots_down(1.25, 0.1, 0.1), 1.2)
        self.assertEqual(round_lots_down(2.9, 1, 1), 2.0)
        self.assertEqual(round_lots_down(0.155, 0.05, 0.05), 0.15)

    def test_below_min_lot(self):
        self.assertEqual(round_lots_down(0.0099, 0.01, 0.01), 0.0)
        self.assertEqual(round_lots_down(0.09, 0.01, 0.1), 0.0)
        self.assertEqual(round_lots_down(0.1, 0.01, 0.1), 0.1)

    def test_garbage_is_zero(self):
        for bad in (0.0, -0.5, float("nan"), float("inf"), None):
            self.assertEqual(round_lots_down(bad, 0.01, 0.01), 0.0)
        self.assertEqual(round_lots_down(1.0, 0.0, 0.01), 0.0)
        self.assertEqual(round_lots_down(1.0, -0.01, 0.01), 0.0)

    def test_random_never_exceeds_input_and_is_multiple(self):
        rng = random.Random(7)
        for _ in range(3000):
            step = rng.choice([0.01, 0.1, 1.0, 0.05])
            lots = rng.uniform(0, 10)
            out = round_lots_down(lots, step, step)
            self.assertLessEqual(out, lots)
            self.assertGreater(out + step, lots - 1e-9)  # floor, not something lower
            self.assertEqual(Decimal(repr(out)) % Decimal(repr(step)), 0)


# --------------------------------------------------------------------------- trading window


class TradingWindowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def ok(self, dt):
        self.assertIsNone(check_trading_window(dt, self.cfg), dt)

    def blocked(self, dt, needle):
        msg = check_trading_window(dt, self.cfg)
        self.assertIsNotNone(msg, dt)
        self.assertTrue(msg.startswith("WINDOW: "), msg)
        self.assertIn(needle, msg)
        return msg

    def test_midweek_ok(self):
        self.ok(NOW)

    def test_sunday_evening_utc_is_monday_server_before_start(self):
        # Sunday 21:02Z == Monday 00:02 server time: right day, before 00:05 start.
        msg = self.blocked(datetime(2026, 10, 4, 21, 2, tzinfo=UTC), "before trading start")
        self.assertIn("Mon 2026-10-05 00:02", msg)
        self.ok(datetime(2026, 10, 4, 21, 5, tzinfo=UTC))   # Monday 00:05 server: start is inclusive

    def test_sunday_server_is_weekend(self):
        # Sunday 20:59Z == Sunday 23:59 server time.
        self.blocked(datetime(2026, 10, 4, 20, 59, tzinfo=UTC), "not a trading day")

    def test_saturday(self):
        self.blocked(datetime(2026, 10, 3, 12, 0, tzinfo=UTC), "not a trading day")

    def test_friday_evening_utc_is_saturday_server(self):
        # Friday 21:00Z == Saturday 00:00 server.
        self.blocked(datetime(2026, 10, 2, 21, 0, tzinfo=UTC), "not a trading day")

    def test_after_end(self):
        # Thursday 20:50Z == 23:50 server: end is exclusive.
        self.blocked(datetime(2026, 10, 1, 20, 50, tzinfo=UTC), "after trading end")
        self.ok(datetime(2026, 10, 1, 20, 49, 59, tzinfo=UTC))

    def test_friday_cutoff_in_server_time(self):
        # Friday 19:00Z == 22:00 server == cutoff.
        self.blocked(datetime(2026, 10, 2, 19, 0, tzinfo=UTC), "Friday cutoff")
        self.ok(datetime(2026, 10, 2, 18, 59, 59, tzinfo=UTC))
        # The same UTC time on Thursday is fine (cutoff only on Fridays).
        self.ok(datetime(2026, 10, 1, 19, 30, tzinfo=UTC))

    def test_friday_cutoff_disabled(self):
        cfg = make_cfg({"friday_cutoff_server": None})
        self.assertIsNone(check_trading_window(datetime(2026, 10, 2, 19, 30, tzinfo=UTC), cfg))

    def test_custom_days_include_sunday(self):
        cfg = make_cfg({"trading_days_server": [6, 0, 1, 2, 3, 4]})
        self.assertIsNone(check_trading_window(datetime(2026, 10, 4, 12, 0, tzinfo=UTC), cfg))

    def test_overnight_window(self):
        cfg = make_cfg({"trading_start_server": "22:00", "trading_end_server": "06:00",
                        "friday_cutoff_server": None})
        # Wednesday 23:00 server (20:00Z) and Thursday 05:59 server (02:59Z) are inside.
        self.assertIsNone(check_trading_window(datetime(2026, 9, 30, 20, 0, tzinfo=UTC), cfg))
        self.assertIsNone(check_trading_window(datetime(2026, 10, 1, 2, 59, tzinfo=UTC), cfg))
        msg = check_trading_window(datetime(2026, 9, 30, 9, 0, tzinfo=UTC), cfg)  # 12:00 server
        self.assertTrue(msg.startswith("WINDOW: "), msg)

    def test_empty_window_blocks(self):
        cfg = make_cfg({"trading_start_server": "10:00", "trading_end_server": "10:00"})
        self.assertTrue(check_trading_window(NOW, cfg).startswith("WINDOW: "))

    def test_other_offset(self):
        cfg = make_cfg(account={"server_utc_offset_hours": 2.0})
        # Sunday 22:02Z == Monday 00:02 at GMT+2 -> before start; at GMT+3 it's 01:02 -> OK.
        self.assertIn("before trading start", check_trading_window(datetime(2026, 10, 4, 22, 2, tzinfo=UTC), cfg))
        self.assertIsNone(check_trading_window(datetime(2026, 10, 4, 22, 2, tzinfo=UTC), self.cfg))


# --------------------------------------------------------------------------- sizing


class SizingTests(Base):
    def test_eurusd_hand_computed(self):
        # EURUSD buy 1.08345, SL 1.08100 -> 0.00245 = 24.5 pips.
        # loss/lot = 0.00245 * 100,000 * 1.0 + $5 commission = $245 + $5 = $250.00
        # with 15% slippage buffer: $250 * 1.15 = $287.50 per lot
        # target = 0.5% of min(50,000, 50,000) = $250.00
        # lots = 250 / 287.50 = 0.869565... -> floor to 0.01 -> 0.86
        # booked risk = 0.86 * 287.50 = $247.25
        p = self.assertApproved(self.plan())
        self.assertEqual(p.lots, 0.86)
        self.assertAlmostEqual(p.per_lot_loss_usd, 250.0, places=6)
        self.assertAlmostEqual(p.risk_usd, 247.25, places=6)
        self.assertEqual(p.close_first, [])
        d = p.details
        self.assertAlmostEqual(d["base"], 50000.0)
        self.assertAlmostEqual(d["target"], 250.0)
        self.assertAlmostEqual(d["raw_lots"], 250 / 287.5, places=9)
        self.assertAlmostEqual(d["worst_case_balance"], 50000 - 247.25, places=6)
        self.assertAlmostEqual(d["worst_case_equity"], 50000 - 247.25, places=6)
        self.assertAlmostEqual(d["floors"]["entry_floor"], 48500.0)

    def test_sell_side_same_size(self):
        sig = make_signal(action="sell", price=1.08100, sl=1.08345, tp=1.07500)
        p = self.assertApproved(self.plan(sig))
        self.assertEqual(p.lots, 0.86)

    def test_base_is_min_of_balance_and_equity(self):
        # equity 49,000 -> target $245 -> 245 / 287.5 = 0.852 -> 0.85 lots
        p = self.assertApproved(self.plan(snapshot=make_snapshot(balance=50000, equity=49000)))
        self.assertEqual(p.lots, 0.85)
        self.assertAlmostEqual(p.risk_usd, 0.85 * 287.5, places=6)

    def test_usdjpy_inverse_price(self):
        # USDJPY buy 150.000, SL 149.500 -> 0.5 JPY * 100,000 = 50,000 JPY per lot
        # JPY -> USD at 1/150: 333.333...; + $5 = 338.333...; * 1.15 = 389.0833...
        # 250 / 389.0833 = 0.6425 -> 0.64 lots; risk = 0.64 * 389.0833 = $249.0133
        sig = make_signal(tv_symbol="USDJPY", symbol="USDJPY.h", price=150.000, sl=149.500, tp=151.0)
        p = self.assertApproved(self.plan(sig))
        self.assertEqual(p.lots, 0.64)
        self.assertAlmostEqual(p.per_lot_loss_usd, 50000 / 150 + 5, places=6)
        self.assertAlmostEqual(p.risk_usd, 0.64 * (50000 / 150 + 5) * 1.15, places=6)
        self.assertAlmostEqual(p.details["q2usd"], 1 / 150.0)

    def test_xauusd_contract_100(self):
        # XAUUSD buy 2650.00, SL 2640.00 -> $10 * 100 oz = $1,000 + $5 = $1,005; * 1.15 = $1,155.75
        # 250 / 1155.75 = 0.2163 -> 0.21 lots; risk = 0.21 * 1155.75 = $242.7075
        sig = make_signal(tv_symbol="XAUUSD", symbol="XAUUSD.h", price=2650.00, sl=2640.00, tp=2680.0)
        p = self.assertApproved(self.plan(sig))
        self.assertEqual(p.lots, 0.21)
        self.assertAlmostEqual(p.risk_usd, 242.7075, places=6)

    def test_cross_without_quote_usd_is_no_fx_rate(self):
        sig = make_signal(tv_symbol="EURGBP", symbol="EURGBP.h", price=0.86500, sl=0.86300, tp=0.87000)
        self.assertCode(self.plan(sig), "NO_FX_RATE")

    def test_cross_with_quote_usd(self):
        # EURGBP buy 0.86500, SL 0.86300 -> 0.002 GBP * 100,000 = 200 GBP * 1.27 = $254 + $5 = $259
        # * 1.15 = $297.85; 250 / 297.85 = 0.8393 -> 0.83 lots; risk = 0.83 * 297.85 = $247.2155
        sig = make_signal(tv_symbol="EURGBP", symbol="EURGBP.h", price=0.86500, sl=0.86300, tp=0.87000,
                          quote_usd=1.27)
        p = self.assertApproved(self.plan(sig))
        self.assertEqual(p.lots, 0.83)
        self.assertAlmostEqual(p.risk_usd, 247.2155, places=6)

    def test_risk_pct_override_capped(self):
        # 2% requested, capped at max_risk_per_trade_pct 1% -> target $500
        # 500 / 287.5 = 1.739 -> 1.73 lots; risk = 1.73 * 287.5 = $497.375
        p = self.assertApproved(self.plan(make_signal(risk_pct=2.0)))
        self.assertEqual(p.lots, 1.73)
        self.assertAlmostEqual(p.risk_usd, 497.375, places=6)
        self.assertAlmostEqual(p.details["risk_pct"], 1.0)
        self.assertLessEqual(p.risk_usd, 50000 * 1.0 / 100)

    def test_risk_pct_override_lower(self):
        # 0.25% -> $125 / 287.5 = 0.4347 -> 0.43 lots
        p = self.assertApproved(self.plan(make_signal(risk_pct=0.25)))
        self.assertEqual(p.lots, 0.43)

    def test_risk_pct_zero_means_default(self):
        p = self.assertApproved(self.plan(make_signal(risk_pct=0.0)))
        self.assertEqual(p.lots, 0.86)

    def test_negative_risk_pct_refused(self):
        self.assertCode(self.plan(make_signal(risk_pct=-1.0)), "SIZE_TOO_SMALL")

    def test_max_lots_clamp(self):
        # 5-pip stop (exactly min_sl_points=50) with 1% risk:
        # $50 + $5 = $55 * 1.15 = $63.25/lot; 500 / 63.25 = 7.9 lots -> clamped to max_lots 5.0
        # risk = 5.0 * 63.25 = $316.25
        sig = make_signal(price=1.08345, sl=1.08295, risk_pct=1.0)
        p = self.assertApproved(self.plan(sig))
        self.assertEqual(p.lots, 5.0)
        self.assertTrue(p.details["capped_by_max_lots"])
        self.assertAlmostEqual(p.risk_usd, 316.25, places=6)

    def test_max_lots_clamp_floored_to_step(self):
        cfg = make_cfg({"max_lots": 0.505})
        p = self.assertApproved(self.plan(cfg=cfg))
        self.assertEqual(p.lots, 0.5)
        self.assertAlmostEqual(p.risk_usd, 0.5 * 287.5, places=6)

    def test_size_too_small_for_very_wide_stop(self):
        # XAUUSD SL $300 away: $30,000 + $5 = $30,005 * 1.15 = $34,505.75/lot
        # 250 / 34505.75 = 0.0072 lots < min_lot 0.01
        sig = make_signal(tv_symbol="XAUUSD", symbol="XAUUSD.h", price=2650.0, sl=2350.0, tp=None)
        p = self.assertCode(self.plan(sig), "SIZE_TOO_SMALL")
        self.assertAlmostEqual(p.details["raw_lots"], 250 / 34505.75, places=9)

    def test_lot_step_from_spec(self):
        cfg = make_cfg(specs={"XAUUSD": {"lot_step": 0.1, "min_lot": 0.1}})
        sig = make_signal(tv_symbol="XAUUSD", symbol="XAUUSD.h", price=2650.0, sl=2649.0, tp=None, risk_pct=1.0)
        # $1 * 100 + $5 = $105 * 1.15 = $120.75; 500 / 120.75 = 4.14 -> 4.1 lots
        p = self.assertApproved(self.plan(sig, cfg=cfg))
        self.assertEqual(p.lots, 4.1)
        # The $10-stop example (0.2163 lots) floors to the 0.1 step: 0.2.
        sig2 = make_signal(tv_symbol="XAUUSD", symbol="XAUUSD.h", price=2650.0, sl=2640.0, tp=None)
        self.assertEqual(self.assertApproved(self.plan(sig2, cfg=cfg)).lots, 0.2)

    def test_no_slippage_no_commission(self):
        cfg = make_cfg({"commission_per_lot_usd": 0.0, "slippage_buffer_pct": 0.0})
        # $245/lot -> 250/245 = 1.0204 -> 1.02 lots; risk = 1.02 * 245 = $249.90
        p = self.assertApproved(self.plan(cfg=cfg))
        self.assertEqual(p.lots, 1.02)
        self.assertAlmostEqual(p.risk_usd, 249.9, places=6)

    def test_readme_example(self):
        # README: EURUSD 1.10000 / 1.09800 -> $205/lot * 1.15 = $235.75; 250 / 235.75 = 1.0604 -> 1.06 lots;
        # booked 1.06 * 235.75 = $249.895 (README rounds to $249.90)
        p = self.assertApproved(self.plan(make_signal(price=1.10000, sl=1.09800, tp=1.105)))
        self.assertEqual(p.lots, 1.06)
        self.assertAlmostEqual(p.risk_usd, 249.895, places=6)


# --------------------------------------------------------------------------- one test per reason code


class ReasonCodeTests(Base):
    def test_approved_baseline(self):
        self.assertApproved(self.plan())

    def test_halted(self):
        p = self.assertCode(self.plan(halted="UNCERTAIN_EXECUTION buy EURUSD.h", paused=True), "HALTED")
        self.assertIn("UNCERTAIN_EXECUTION", p.reason)

    def test_paused(self):
        self.assertCode(self.plan(paused=True), "PAUSED")

    def test_bad_action(self):
        for action in ("close", "close_all", "", "long"):
            self.assertCode(self.plan(make_signal(action=action)), "BAD_ACTION")

    def test_no_spec(self):
        self.assertCode(self.plan(make_signal(tv_symbol="BTCUSD", symbol="BTCUSD.h")), "NO_SPEC")
        self.assertCode(self.plan(make_signal(tv_symbol="", symbol="")), "NO_SPEC")

    def test_window_weekend(self):
        sat = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
        sig = make_signal(fired_at=sat - timedelta(seconds=5))
        p = self.assertCode(self.plan(sig, now=sat, snapshot=AccountSnapshot(sat, 50000, 50000, positions=[])),
                            "WINDOW")
        self.assertIn("not a trading day", p.reason)

    def test_window_before_start(self):
        t = datetime(2026, 10, 4, 21, 2, tzinfo=UTC)   # Monday 00:02 server
        sig = make_signal(fired_at=t - timedelta(seconds=5))
        p = self.assertCode(self.plan(sig, now=t, snapshot=AccountSnapshot(t, 50000, 50000)), "WINDOW")
        self.assertIn("before trading start", p.reason)

    def test_window_after_end(self):
        t = datetime(2026, 9, 30, 20, 55, tzinfo=UTC)  # Wednesday 23:55 server
        sig = make_signal(fired_at=t - timedelta(seconds=5))
        p = self.assertCode(self.plan(sig, now=t, snapshot=AccountSnapshot(t, 50000, 50000)), "WINDOW")
        self.assertIn("after trading end", p.reason)

    def test_window_friday_cutoff(self):
        t = datetime(2026, 10, 2, 19, 30, tzinfo=UTC)  # Friday 22:30 server
        sig = make_signal(fired_at=t - timedelta(seconds=5))
        p = self.assertCode(self.plan(sig, now=t, snapshot=AccountSnapshot(t, 50000, 50000)), "WINDOW")
        self.assertIn("Friday cutoff", p.reason)

    def test_stale_signal(self):
        self.assertCode(self.plan(make_signal(fired_at=NOW - timedelta(seconds=46))), "STALE_SIGNAL")
        self.assertApproved(self.plan(make_signal(fired_at=NOW - timedelta(seconds=45))))

    def test_no_snapshot(self):
        self.assertCode(self.plan(snapshot=None), "NO_SNAPSHOT")

    def test_invalid_snapshot_values(self):
        self.assertCode(self.plan(snapshot=make_snapshot(equity=float("nan"))), "NO_SNAPSHOT")
        self.assertCode(self.plan(snapshot=make_snapshot(balance=0.0)), "NO_SNAPSHOT")

    def test_snapshot_stale(self):
        self.assertCode(self.plan(snapshot=make_snapshot(age_s=91)), "SNAPSHOT_STALE")
        self.assertApproved(self.plan(snapshot=make_snapshot(age_s=90)))

    def test_no_day_reference(self):
        self.assertCode(self.plan(floors=None), "NO_DAY_REFERENCE")
        bad = replace(compute_floors(50000, self.cfg), entry_floor=float("nan"))
        self.assertCode(self.plan(floors=bad), "NO_DAY_REFERENCE")

    def test_untracked_positions(self):
        stray = ObservedPosition("GBPUSD.h", "buy", 0.1)
        self.assertCode(self.plan(untracked_positions=[stray], observed_positions=[stray]),
                        "UNTRACKED_POSITIONS")

    def test_untracked_allowed_when_not_blocking_and_its_risk_is_known(self):
        cfg = make_cfg({"block_untracked_positions": False})
        stray = ObservedPosition("GBPUSD.h", "buy", 0.1, open_price=1.30000, sl=1.29700)
        p = self.assertApproved(self.plan(cfg=cfg, untracked_positions=[stray], observed_positions=[stray]))
        # 0.1 x (0.003 x 100,000 + 5) = 30.50 counted as open risk
        self.assertAlmostEqual(p.details["untracked_risk_usd"], 30.5, places=6)
        self.assertAlmostEqual(p.details["open_risk_usd"], 30.5, places=6)

    def test_untracked_without_known_sl_is_unknown_risk(self):
        cfg = make_cfg({"block_untracked_positions": False})
        for stray in (ObservedPosition("GBPUSD.h", "buy", 0.1),
                      ObservedPosition("XAUUSD.h", "buy", 2.0, open_price=2650.0, sl=None, sl_missing=True),
                      ObservedPosition("BTCUSD.h", "buy", 0.1, open_price=60000.0, sl=59000.0)):   # no spec
            p = self.assertCode(self.plan(cfg=cfg, untracked_positions=[stray], observed_positions=[stray]),
                                "TOTAL_OPEN_RISK")
            self.assertIn("unknown SL/risk", p.reason)

    def test_positions_uncertain(self):
        p = self.assertCode(self.plan(positions_uncertain="EURUSD.h buy 1.06 (#1) not visible in MT5"),
                            "POSITIONS_UNCERTAIN")
        self.assertIn("not visible", p.reason)

    def test_sl_missing_on_server(self):
        p = self.assertCode(self.plan(sl_issues=["EURUSD.h buy 1.06 #123 shows no stop-loss"]),
                            "SL_MISSING_ON_SERVER")
        self.assertIn("#123", p.reason)

    def test_stale_reference_estimate_refuses_entries(self):
        p = self.assertCode(self.plan(reference_note="daily reference is a stale estimate"), "NO_DAY_REFERENCE")
        self.assertIn("set-reference", p.reason)

    def test_confirmed_balance_beats_a_single_high_misread(self):
        # true balance 49,000 (misread once as 59,000), equity 48,850, 400 USD booked open risk
        rows = [ledger_row("GBPUSD.h", "buy", risk_usd=400)]
        obs = [ObservedPosition("GBPUSD.h", "buy", 0.3)]
        snap = make_snapshot(balance=59000, equity=48850)
        self.assertApproved(self.plan(snapshot=snap, open_positions=rows, observed_positions=obs))
        p = self.assertCode(self.plan(snapshot=snap, open_positions=rows, observed_positions=obs,
                                      confirmed_balance=49000.0, confirmed_equity=48850.0), "WORST_CASE")
        self.assertAlmostEqual(p.details["confirmed_balance"], 49000.0)

    def test_confirmed_equity_for_entry_floor(self):
        self.assertCode(self.plan(confirmed_equity=48400.0), "BELOW_ENTRY_FLOOR")

    def test_no_price(self):
        for price in (None, 0.0, -1.0, float("nan")):
            self.assertCode(self.plan(make_signal(price=price)), "NO_PRICE")

    def test_sl_missing(self):
        self.assertCode(self.plan(make_signal(sl=None)), "SL_MISSING")
        self.assertCode(self.plan(make_signal(sl=float("nan"))), "SL_MISSING")

    def test_sl_wrong_side_buy(self):
        self.assertCode(self.plan(make_signal(action="buy", sl=1.08400)), "SL_WRONG_SIDE")
        self.assertCode(self.plan(make_signal(action="buy", sl=1.08345)), "SL_WRONG_SIDE")  # equal

    def test_sl_wrong_side_sell(self):
        sig = make_signal(action="sell", price=1.08345, sl=1.08100, tp=1.07000)
        self.assertCode(self.plan(sig), "SL_WRONG_SIDE")
        self.assertCode(self.plan(make_signal(action="sell", sl=1.08345, tp=1.07)), "SL_WRONG_SIDE")

    def test_tp_wrong_side(self):
        self.assertCode(self.plan(make_signal(action="buy", tp=1.08000)), "TP_WRONG_SIDE")
        self.assertCode(self.plan(make_signal(action="buy", tp=1.08345)), "TP_WRONG_SIDE")
        sell = make_signal(action="sell", price=1.08345, sl=1.08600, tp=1.09000)
        self.assertCode(self.plan(sell), "TP_WRONG_SIDE")

    def test_no_tp_is_fine(self):
        self.assertApproved(self.plan(make_signal(tp=None)))

    def test_sl_too_tight(self):
        # min_sl_points 50 * point 0.00001 = 0.00050; 49 points is too tight, exactly 50 is OK
        # (1.08345 - 1.08295 is 0.000499999... in float; the check must not trip on that).
        self.assertCode(self.plan(make_signal(sl=1.08296)), "SL_TOO_TIGHT")
        self.assertApproved(self.plan(make_signal(sl=1.08295, tp=None)))
        gold = make_signal(tv_symbol="XAUUSD", symbol="XAUUSD.h", price=2650.00, sl=2649.01, tp=None)
        self.assertCode(self.plan(gold), "SL_TOO_TIGHT")  # needs 100 points = $1.00

    def test_opposite_open(self):
        cfg = make_cfg({"reverse_on_opposite": False})
        opp = ObservedPosition("EURUSD.h", "sell", 0.5, ticket="123")
        self.assertCode(self.plan(cfg=cfg, observed_positions=[opp],
                                  open_positions=[ledger_row("EURUSD.h", "sell", risk_usd=200)]), "OPPOSITE_OPEN")

    def test_pyramiding(self):
        same = ObservedPosition("EURUSD.h", "buy", 0.5)
        self.assertCode(self.plan(observed_positions=[same],
                                  open_positions=[ledger_row("EURUSD.h", "buy")]), "PYRAMIDING")

    def test_pyramiding_allowed(self):
        cfg = make_cfg({"allow_pyramiding": True})
        same = ObservedPosition("EURUSD.h", "buy", 0.5)
        self.assertApproved(self.plan(cfg=cfg, observed_positions=[same],
                                      open_positions=[ledger_row("EURUSD.h", "buy")]))

    def test_max_positions(self):
        obs = [ObservedPosition(s, "buy", 0.1) for s in ("GBPUSD.h", "AUDUSD.h", "NZDUSD.h")]
        rows = [ledger_row(p.symbol, "buy", risk_usd=50) for p in obs]
        self.assertCode(self.plan(observed_positions=obs, open_positions=rows), "MAX_POSITIONS")
        self.assertApproved(self.plan(observed_positions=obs[:2], open_positions=rows[:2]))

    def test_max_trades_day(self):
        self.assertCode(self.plan(trades_today=8), "MAX_TRADES_DAY")
        self.assertApproved(self.plan(trades_today=7))

    def test_no_fx_rate(self):
        sig = make_signal(tv_symbol="EURGBP", symbol="EURGBP.h", price=0.86500, sl=0.86300, tp=None)
        self.assertCode(self.plan(sig), "NO_FX_RATE")

    def test_size_too_small(self):
        sig = make_signal(price=1.08345, sl=0.58345, tp=None)  # 5,000 pips
        self.assertCode(self.plan(sig), "SIZE_TOO_SMALL")

    def test_total_open_risk(self):
        # open $800 + this $247.25 = $1,047.25 > 2% of 50,000 = $1,000
        rows = [ledger_row("GBPUSD.h", "buy", risk_usd=400), ledger_row("AUDUSD.h", "sell", risk_usd=400)]
        p = self.assertCode(self.plan(open_positions=rows, observed_positions=None), "TOTAL_OPEN_RISK")
        self.assertAlmostEqual(p.details["open_risk_usd"], 800.0)
        rows[1]["risk_usd"] = 350  # 750 + 247.25 = 997.25 -> OK
        self.assertApproved(self.plan(open_positions=rows, observed_positions=None))

    def test_total_open_risk_unknown_ledger_risk(self):
        rows = [ledger_row("GBPUSD.h", "buy", risk_usd=None)]
        self.assertCode(self.plan(open_positions=rows, observed_positions=None), "TOTAL_OPEN_RISK")

    def test_below_entry_floor(self):
        # equity exactly at the entry floor (48,500) is refused
        self.assertCode(self.plan(snapshot=make_snapshot(balance=50000, equity=48500)), "BELOW_ENTRY_FLOOR")
        self.assertCode(self.plan(snapshot=make_snapshot(balance=50000, equity=48000)), "BELOW_ENTRY_FLOOR")

    def test_worst_case_equity(self):
        # README: equity 48,700 -> target 243.50 -> 0.84 lots -> $241.50; 48,700 - 241.50 = 48,458.50 < 48,500
        p = self.assertCode(self.plan(snapshot=make_snapshot(balance=50000, equity=48700)), "WORST_CASE")
        self.assertAlmostEqual(p.details["worst_case_equity"], 48458.5, places=6)

    def test_worst_case_balance(self):
        # balance 49,000, open risk $300 -> 49,000 - 300 - 244.375 = 48,455.625 < 48,500
        rows = [ledger_row("GBPUSD.h", "buy", risk_usd=300)]
        obs = [ObservedPosition("GBPUSD.h", "buy", 0.3)]
        p = self.assertCode(self.plan(snapshot=make_snapshot(balance=49000, equity=49000),
                                      open_positions=rows, observed_positions=obs), "WORST_CASE")
        self.assertAlmostEqual(p.details["worst_case_balance"], 49000 - 300 - 0.85 * 287.5, places=6)
        # Without the open trade it fits: 49,000 - 244.375 >= 48,500
        self.assertApproved(self.plan(snapshot=make_snapshot(balance=49000, equity=49000)))

    def test_floors_from_higher_reference(self):
        # reference 52,000 -> entry floor 50,440: a 50,600 account can risk only ~160 USD.
        cfg = self.cfg
        floors = compute_floors(52000, cfg)
        self.assertCode(self.plan(floors=floors, snapshot=make_snapshot(balance=50600, equity=50600)),
                        "WORST_CASE")
        self.assertCode(self.plan(floors=floors, snapshot=make_snapshot(balance=50440, equity=50440)),
                        "BELOW_ENTRY_FLOOR")

    def test_internal_error_fails_closed(self):
        state = make_state(self.cfg)
        state.now = "not a datetime"  # type: ignore[assignment]
        p = plan_entry(make_signal(), state, self.cfg)
        self.assertFalse(p.approved)
        self.assertEqual(p.code, "RISK_ERROR")


class CheckOrderTests(Base):
    """Start with a state where every check fails and fix one thing at a time: the codes must
    appear exactly in the SPEC order."""

    def test_cascade(self):
        cfg_holder = {"cfg": make_cfg({"reverse_on_opposite": False, "allow_pyramiding": False,
                                        "max_open_positions": 2})}
        sat = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
        sig = make_signal(action="close", tv_symbol="FOOBAR", symbol="FOOBAR.h", price=None, sl=None,
                          tp=None, risk_pct=0.0001, fired_at=NOW - timedelta(seconds=600))
        obs = [ObservedPosition("EURGBP.h", "sell", 0.2), ObservedPosition("EURGBP.h", "buy", 0.2),
               ObservedPosition("GBPUSD.h", "buy", 0.1)]
        rows = [ledger_row("EURGBP.h", "sell", risk_usd=400), ledger_row("EURGBP.h", "buy", risk_usd=400),
                ledger_row("GBPUSD.h", "buy", risk_usd=100)]
        st = make_state(cfg_holder["cfg"], now=sat, snapshot=None, floors=None, open_positions=rows,
                        observed_positions=obs, untracked_positions=[ObservedPosition("USDCHF.h", "buy", 1)],
                        trades_today=8, paused=True, halted="KILL", positions_uncertain="row missing",
                        sl_issues=["no SL"])
        st.now = sat

        def set_cfg(**risk_over):
            base = {"reverse_on_opposite": False, "allow_pyramiding": False, "max_open_positions": 2}
            cur = cfg_holder.get("risk_over", base)
            cur = dict(cur, **risk_over)
            cfg_holder["risk_over"] = cur
            cfg_holder["cfg"] = make_cfg(cur)

        steps = [
            ("HALTED", lambda: setattr(st, "halted", "")),
            ("PAUSED", lambda: setattr(st, "paused", False)),
            ("BAD_ACTION", lambda: setattr(sig, "action", "buy")),
            ("NO_SPEC", lambda: (setattr(sig, "symbol", "EURGBP.h"), setattr(sig, "tv_symbol", "EURGBP"))),
            ("WINDOW", lambda: setattr(st, "now", NOW)),
            ("STALE_SIGNAL", lambda: setattr(sig, "fired_at", NOW - timedelta(seconds=3))),
            ("NO_SNAPSHOT", lambda: setattr(st, "snapshot", make_snapshot(age_s=300))),
            ("SNAPSHOT_STALE", lambda: setattr(st, "snapshot", make_snapshot(balance=48800, equity=48400))),
            ("NO_DAY_REFERENCE", lambda: setattr(st, "floors", compute_floors(50000, cfg_holder["cfg"]))),
            ("UNTRACKED_POSITIONS", lambda: setattr(st, "untracked_positions", [])),
            ("POSITIONS_UNCERTAIN", lambda: setattr(st, "positions_uncertain", "")),
            ("SL_MISSING_ON_SERVER", lambda: setattr(st, "sl_issues", [])),
            ("NO_PRICE", lambda: setattr(sig, "price", 0.86500)),
            ("SL_MISSING", lambda: setattr(sig, "sl", 0.86600)),
            ("SL_WRONG_SIDE", lambda: (setattr(sig, "sl", 0.86490), setattr(sig, "tp", 0.86000))),
            ("TP_WRONG_SIDE", lambda: setattr(sig, "tp", 0.87000)),
            ("SL_TOO_TIGHT", lambda: setattr(sig, "sl", 0.86300)),
            ("OPPOSITE_OPEN", lambda: set_cfg(reverse_on_opposite=True)),
            ("PYRAMIDING", lambda: set_cfg(allow_pyramiding=True)),
            ("MAX_POSITIONS", lambda: set_cfg(max_open_positions=3)),
            ("MAX_TRADES_DAY", lambda: setattr(st, "trades_today", 0)),
            ("NO_FX_RATE", lambda: setattr(sig, "quote_usd", 1.27)),
            ("SIZE_TOO_SMALL", lambda: setattr(sig, "risk_pct", None)),
            ("TOTAL_OPEN_RISK", lambda: [r.__setitem__("risk_usd", v) for r, v in zip(rows, (30, 30, 40))]),
            ("BELOW_ENTRY_FLOOR", lambda: setattr(st, "snapshot", make_snapshot(balance=48800, equity=48800))),
            ("WORST_CASE", lambda: setattr(st, "snapshot", make_snapshot(balance=50000, equity=50000))),
        ]
        seen = []
        for code, fix in steps:
            p = plan_entry(sig, st, cfg_holder["cfg"])
            self.assertFalse(p.approved, "expected %s, got approved" % code)
            self.assertEqual(p.code, code, "after %s: %s" % (seen, p.reason))
            seen.append(code)
            fix()
        p = plan_entry(sig, st, cfg_holder["cfg"])
        self.assertApproved(p)
        self.assertEqual([(c.symbol, c.side) for c in p.close_first], [("EURGBP.h", "sell")])
        self.assertEqual(set(seen), KNOWN_CODES)


# --------------------------------------------------------------------------- reversal


class ReversalTests(Base):
    def test_reversal_closes_opposite_first(self):
        opp = ObservedPosition("EURUSD.h", "sell", 0.5, ticket="555")
        rows = [ledger_row("EURUSD.h", "sell", lots=0.5, risk_usd=200, ticket="555")]
        p = self.assertApproved(self.plan(observed_positions=[opp], open_positions=rows))
        self.assertEqual(p.close_first, [opp])
        self.assertEqual(p.lots, 0.86)
        # Total open risk still counts the position being closed: 200 + 247.25
        self.assertAlmostEqual(p.details["open_risk_usd"], 200.0)

    def test_reversal_excluded_from_max_positions(self):
        cfg = make_cfg({"max_open_positions": 1})
        opp = ObservedPosition("EURUSD.h", "sell", 0.5)
        rows = [ledger_row("EURUSD.h", "sell", risk_usd=200)]
        p = self.assertApproved(self.plan(cfg=cfg, observed_positions=[opp], open_positions=rows))
        self.assertEqual(len(p.close_first), 1)
        # But another open position still counts.
        other = ObservedPosition("GBPUSD.h", "buy", 0.1)
        rows2 = rows + [ledger_row("GBPUSD.h", "buy", risk_usd=50)]
        self.assertCode(self.plan(cfg=cfg, observed_positions=[opp, other], open_positions=rows2),
                        "MAX_POSITIONS")

    def test_reversal_of_sell_signal(self):
        opp = ObservedPosition("EURUSD.h", "buy", 0.5)
        sig = make_signal(action="sell", price=1.08100, sl=1.08345, tp=1.07500)
        p = self.assertApproved(self.plan(sig, observed_positions=[opp],
                                          open_positions=[ledger_row("EURUSD.h", "buy", risk_usd=100)]))
        self.assertEqual(p.close_first, [opp])

    def test_reversal_from_ledger_when_observed_unknown(self):
        rows = [ledger_row("EURUSD.h", "sell", lots=0.4, risk_usd=150, ticket="77", entry_price=1.0850)]
        p = self.assertApproved(self.plan(observed_positions=None, open_positions=rows))
        self.assertEqual(len(p.close_first), 1)
        c = p.close_first[0]
        self.assertIsInstance(c, ObservedPosition)
        self.assertEqual((c.symbol, c.side, c.lots, c.ticket, c.open_price), ("EURUSD.h", "sell", 0.4, "77", 1.085))

    def test_reversal_from_ledger_when_snapshot_predates_fill(self):
        # Observed list is known but older than a just-filled ledger position: still reverse.
        rows = [ledger_row("EURUSD.h", "sell", risk_usd=150)]
        p = self.assertApproved(self.plan(observed_positions=[], open_positions=rows))
        self.assertEqual([(c.symbol, c.side) for c in p.close_first], [("EURUSD.h", "sell")])

    def test_pyramiding_detected_from_ledger_when_snapshot_predates_fill(self):
        rows = [ledger_row("EURUSD.h", "buy", risk_usd=150)]
        self.assertCode(self.plan(observed_positions=[], open_positions=rows), "PYRAMIDING")

    def test_symbol_forms_match(self):
        # Ledger stores the TV form, MT5 shows a different case: still the same symbol.
        rows = [ledger_row("EURUSD", "buy", risk_usd=150)]
        self.assertCode(self.plan(observed_positions=[ObservedPosition("eurusd.H", "buy", 0.1)],
                                  open_positions=rows), "PYRAMIDING")

    def test_other_symbol_not_reversed(self):
        obs = [ObservedPosition("GBPUSD.h", "sell", 0.5)]
        p = self.assertApproved(self.plan(observed_positions=obs,
                                          open_positions=[ledger_row("GBPUSD.h", "sell", risk_usd=100)]))
        self.assertEqual(p.close_first, [])


# --------------------------------------------------------------------------- kill & reference


class KillTests(unittest.TestCase):
    def setUp(self):
        self.floors = compute_floors(50000, make_cfg())   # kill floor 48,150

    def snap(self, equity):
        return AccountSnapshot(NOW, 50000.0, equity)

    def test_boundary_equal_kills(self):
        msg = evaluate_kill(self.snap(self.floors.kill_floor), self.floors)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.startswith("KILL: "), msg)
        self.assertIn("48150.00", msg)

    def test_below_kills(self):
        self.assertTrue(evaluate_kill(self.snap(47000.0), self.floors).startswith("KILL: "))

    def test_above_ok(self):
        self.assertIsNone(evaluate_kill(self.snap(48150.01), self.floors))
        self.assertIsNone(evaluate_kill(self.snap(50000.0), self.floors))

    def test_uses_equity_not_balance(self):
        self.assertIsNone(evaluate_kill(AccountSnapshot(NOW, 40000.0, 49000.0), self.floors))


class EstimateReferenceTests(unittest.TestCase):
    def s(self, balance, equity, minutes=0):
        return AccountSnapshot(NOW + timedelta(minutes=minutes), balance, equity)

    def test_no_history_uses_initial_balance(self):
        self.assertEqual(estimate_reference(None, [], 50000.0, False), 50000.0)

    def test_history_but_no_data_is_none(self):
        self.assertIsNone(estimate_reference(None, [], 50000.0, True))

    def test_readme_example(self):
        # previous day ended with balance 50,600 and equity 51,000 -> reference 51,000
        self.assertEqual(estimate_reference(self.s(50600, 51000), [], 50000.0, True), 51000.0)

    def test_today_snapshots_can_raise_reference(self):
        today = [self.s(50600, 50900, 1), self.s(50600, 51200, 2), self.s(50600, 50100, 3)]
        self.assertEqual(estimate_reference(self.s(50600, 51000), today, 50000.0, True), 51200.0)

    def test_only_today_snapshots(self):
        self.assertEqual(estimate_reference(None, [self.s(49800, 49700)], 50000.0, True), 49800.0)

    def test_initial_balance_only_without_history(self):
        today = [self.s(49800, 49700)]
        self.assertEqual(estimate_reference(None, today, 50000.0, False), 50000.0)
        self.assertEqual(estimate_reference(None, today, 50000.0, True), 49800.0)

    def test_non_finite_ignored(self):
        self.assertEqual(estimate_reference(self.s(float("nan"), 50100), [], 50000.0, True), 50100.0)


# --------------------------------------------------------------------------- property test


class PropertyTests(unittest.TestCase):
    """2,000 deterministic pseudo-random approved plans must respect every invariant."""

    SYMBOLS = {
        # tv: (mt5, price range, quote_usd range or None)
        "EURUSD": ((1.00, 1.25), None),
        "GBPUSD": ((1.20, 1.40), None),
        "USDJPY": ((130.0, 160.0), None),
        "USDCAD": ((1.30, 1.40), None),
        "XAUUSD": ((1800.0, 2800.0), None),
        "EURGBP": ((0.80, 0.90), (1.20, 1.40)),
    }

    def configs(self):
        return [
            make_cfg(),
            make_cfg({"max_lots": 1.0, "commission_per_lot_usd": 7.0, "slippage_buffer_pct": 25.0,
                      "max_open_positions": 5, "risk_per_trade_pct": 1.0, "max_risk_per_trade_pct": 2.0},
                     specs={"XAUUSD": {"lot_step": 0.1, "min_lot": 0.1}}),
            make_cfg({"commission_per_lot_usd": 0.0, "slippage_buffer_pct": 0.0, "max_total_open_risk_pct": 3.0,
                      "max_open_positions": 4, "daily_buffer_pct": 0.5},
                     specs={"EURUSD": {"lot_step": 0.05, "min_lot": 0.05}}),
        ]

    def test_invariants(self):
        rng = random.Random(20261001)
        cfgs = self.configs()
        approved = 0
        rejected_codes = set()
        iterations = 0
        while approved < 2000:
            iterations += 1
            self.assertLess(iterations, 200000, "too few approvals generated")
            cfg = rng.choice(cfgs)
            r = cfg.risk
            tv = rng.choice(sorted(self.SYMBOLS))
            (lo, hi), q_range = self.SYMBOLS[tv]
            spec = cfg.spec_for(tv)
            side = rng.choice(("buy", "sell"))
            price = round(rng.uniform(lo, hi), spec.digits)
            pts = rng.randint(int(spec.min_sl_points), int(spec.min_sl_points) * 60)
            dist = pts * spec.point
            sl = round(price - dist if side == "buy" else price + dist, spec.digits)
            tp = round(price + dist * 2 if side == "buy" else price - dist * 2, spec.digits) if rng.random() < 0.7 \
                else None
            quote_usd = round(rng.uniform(*q_range), 5) if q_range else None
            risk_pct = None if rng.random() < 0.5 else round(rng.uniform(0.05, 3.0), 3)
            balance = round(rng.uniform(47500, 56000), 2)
            equity = round(balance + rng.uniform(-1500, 1500), 2)
            reference = round(rng.uniform(48000, 56000), 2)
            others = [s for s in self.SYMBOLS if s != tv]
            ledger = []
            for _ in range(rng.randint(0, 2)):
                other = others.pop(rng.randrange(len(others)))
                ledger.append(ledger_row(cfg.mt5_symbol(other), rng.choice(("buy", "sell")),
                                         risk_usd=round(rng.uniform(0, 400), 2)))
            observed = [ObservedPosition(row["symbol"], row["side"], 0.1) for row in ledger]
            sig = make_signal(tv_symbol=tv, symbol=cfg.mt5_symbol(tv), action=side, price=price, sl=sl, tp=tp,
                              quote_usd=quote_usd, risk_pct=risk_pct)
            floors = compute_floors(reference, cfg)
            snap = make_snapshot(balance=balance, equity=equity)
            state = make_state(cfg, snapshot=snap, floors=floors, open_positions=ledger,
                               observed_positions=observed if rng.random() < 0.8 else None)
            plan = plan_entry(sig, state, cfg)
            if not plan.approved:
                self.assertIn(plan.code, KNOWN_CODES, plan.reason)
                self.assertEqual((plan.lots, plan.risk_usd), (0.0, 0.0))
                rejected_codes.add(plan.code)
                continue
            approved += 1
            open_risk = sum(row["risk_usd"] for row in ledger)
            base = min(balance, equity)
            # The spec's core invariants.
            self.assertGreaterEqual(balance - open_risk - plan.risk_usd, floors.entry_floor)
            self.assertLessEqual(plan.risk_usd, base * r.max_risk_per_trade_pct / 100 + 1e-6)
            self.assertEqual(Decimal(repr(plan.lots)) % Decimal(repr(spec.lot_step)), 0, plan.lots)
            # Further invariants.
            self.assertGreaterEqual(equity - plan.risk_usd, floors.entry_floor)
            self.assertGreater(equity, floors.entry_floor)
            self.assertLessEqual(open_risk + plan.risk_usd, base * r.max_total_open_risk_pct / 100 + 1e-6)
            self.assertGreaterEqual(plan.lots, spec.min_lot)
            self.assertLessEqual(plan.lots, r.max_lots)
            eff_pct = min(risk_pct or r.risk_per_trade_pct, r.max_risk_per_trade_pct)
            target = base * eff_pct / 100
            self.assertLessEqual(plan.risk_usd, target + 1e-6)
            # Booked risk covers the true stop-out loss (commission included, before the buffer).
            q = quote_usd if tv == "EURGBP" else (1 / price if tv.startswith("USD") else 1.0)
            true_loss = plan.lots * (abs(price - sl) * spec.contract_size * q + r.commission_per_lot_usd)
            self.assertLessEqual(true_loss, plan.risk_usd + 1e-6)
            # Not undersized: one more step would exceed the target or max_lots.
            buffered = plan.per_lot_loss_usd * (1 + r.slippage_buffer_pct / 100)
            self.assertTrue((plan.lots + spec.lot_step) * buffered > target - 1e-6
                            or plan.lots + spec.lot_step > r.max_lots + 1e-9)
            self.assertAlmostEqual(plan.risk_usd, plan.lots * buffered, places=6)
        self.assertEqual(approved, 2000)
        # The generator also exercised the money-related rejections.
        for code in ("WORST_CASE", "BELOW_ENTRY_FLOOR", "TOTAL_OPEN_RISK"):
            self.assertIn(code, rejected_codes)


if __name__ == "__main__":
    unittest.main()
