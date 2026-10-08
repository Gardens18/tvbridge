import fcntl
import logging
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tvbridge import clock
from tvbridge.config import config_from_dict, deep_merge, default_home
from tvbridge.executors import ExecutorError, make_executor
from tvbridge.executors.mt5gui import (ACCOUNT_FAILED_LATEST_NAME, ACCOUNT_LATEST_NAME, Mt5GuiExecutor,
                                       login_in_title)
from tvbridge.gui.calibration import CalibrationError, save_calibration
from tvbridge.gui.driver import OcrItem, Window
from tvbridge.models import OrderRequest
from tvbridge.store import Store

from tests.fakes import (
    CLOSE_BUTTON_POINT, FOCUS_POINT, MAIN_RECT, MAIN_TITLE, MT5_PID, ORDER_DIALOG_ORIGIN, ORDER_POINTS, OWNER,
    POSITION_DIALOG_ORIGIN, ROW_STEP, ROW_TOP, TRADE_TAB_POINT, FakeMt5Driver, make_calibration,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)
SECRET = "s" * 24

_QUIET = logging.NullHandler()


def setUpModule():
    # Expected warnings/errors (rejections, uncertain results) would otherwise go to stderr.
    logging.getLogger("tvbridge.executors").addHandler(_QUIET)


def tearDownModule():
    logging.getLogger("tvbridge.executors").removeHandler(_QUIET)


def make_cfg(home, **sections):
    base = {"server": {"secret": SECRET},
            "account": {"account_login": "12345678", "server_name": "HantecMarketsMU-MT5"}}
    return config_from_dict(deep_merge(base, sections), home)


def buy_req(side="buy", lots=0.5, sl=1.08000, tp=1.09000, symbol="EURUSD.h", digits=5, price=1.08345):
    return OrderRequest(symbol=symbol, side=side, lots=lots, sl=sl, tp=tp, digits=digits, lot_decimals=2,
                        comment="tvb", price_hint=price)


def pos(ticket, symbol="EURUSD.h", side="buy", lots=0.5, price=1.08345, profit=0.0):
    return {"ticket": str(ticket), "symbol": symbol, "side": side, "lots": lots, "open_price": price,
            "sl": 0.0, "tp": 0.0, "price": price, "profit": profit}


def button_point(side):
    return ORDER_DIALOG_ORIGIN[0] + ORDER_POINTS[side][0], ORDER_DIALOG_ORIGIN[1] + ORDER_POINTS[side][1]


class GuiTestCase(unittest.TestCase):
    rehearsal = False

    def setUp(self):
        clock.set_clock(lambda: T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = make_cfg(self.home)
        self.shots = self.home / "shots"

    def tearDown(self):
        self.tmp.cleanup()
        clock.set_clock(None)

    def make(self, rehearsal=None, cfg=None, calib=None, **fake_kw):
        self.fake = FakeMt5Driver(**fake_kw)
        ex = Mt5GuiExecutor(cfg or self.cfg, self.fake, calib or make_calibration(),
                            rehearsal=self.rehearsal if rehearsal is None else rehearsal,
                            shots_dir=self.shots)
        return ex

    # assertions ------------------------------------------------------------

    def assertNoOrderButtonClicked(self):
        self.assertEqual(self.fake.order_button_clicks, [])
        self.assertEqual(self.fake.clicks_on_order_buttons(), [])
        self.assertEqual(self.fake.orders_sent, [])

    def assertNoDialogs(self):
        self.assertEqual([d.title for d in self.fake.dialogs], [])

    def assertEvidence(self, res, n_min=1):
        self.assertGreaterEqual(len(res.evidence), n_min)
        for p in res.evidence:
            self.assertTrue(Path(p).exists(), p)
            self.assertEqual(Path(p).parent, self.shots / "20261001")
            self.assertRegex(Path(p).name, r"^095600_000_[A-Za-z0-9_-]+(-\d+)?\.png$")


class OpenMarketLiveTests(GuiTestCase):
    def test_live_fill_parses_ticket_and_price(self):
        ex = self.make()
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual(res.ticket, "52390671")
        self.assertAlmostEqual(res.fill_price, 1.08345)
        self.assertEqual(res.lots, 0.5)
        self.assertEvidence(res, 2)
        # exactly one click, on the buy button, with the verified field values
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertEqual(len(self.fake.clicks_on_order_buttons()), 1)
        x, y, n = self.fake.clicks_on_order_buttons()[0]
        self.assertEqual((x, y, n), button_point("buy") + (1,))
        self.assertEqual(self.fake.orders_sent, [
            {"side": "buy", "symbol": "EURUSD.h", "volume": "0.50", "sl": "1.08000", "tp": "1.09000"}])
        self.assertEqual([p["ticket"] for p in self.fake.positions], ["52390671"])
        self.assertNoDialogs()
        self.assertEqual(self.fake.dangerous_returns, 0)
        self.assertEqual(self.fake.lost_keys, [])
        # F9 opened the dialog; the fields were typed in order
        self.assertIn("f9", self.fake.keys())
        self.assertEqual(self.fake.typed(), ["EURUSD.h", "0.50", "1.08000", "1.09000"])

    def test_field_entry_sequence(self):
        ex = self.make()
        ex.open_market(buy_req())
        acts = [a for a in self.fake.actions if a[0] in ("click", "key", "type")]
        # find the volume field click and check the following key sequence
        vx = ORDER_DIALOG_ORIGIN[0] + ORDER_POINTS["volume"][0]
        vy = ORDER_DIALOG_ORIGIN[1] + ORDER_POINTS["volume"][1]
        i = acts.index(("click", vx, vy, 1))
        self.assertEqual(acts[i + 1:i + 5], [("key", "end", ()), ("key", "home", ("shift",)),
                                             ("type", "0.50"), ("key", "tab", ())])

    def test_sell_without_tp_types_zero(self):
        ex = self.make(fill_price=1.08340)
        res = ex.open_market(buy_req(side="sell", sl=1.09000, tp=None))
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual(self.fake.order_button_clicks, ["sell"])
        self.assertEqual(self.fake.orders_sent[0]["tp"], "0")
        self.assertEqual(self.fake.positions[0]["side"], "sell")

    def test_usdjpy_formats_with_symbol_digits(self):
        ex = self.make(fill_price=150.123)
        res = ex.open_market(buy_req(symbol="USDJPY.h", lots=1.2, sl=149.5, tp=151.25, digits=3, price=150.1))
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual(self.fake.orders_sent[0],
                         {"side": "buy", "symbol": "USDJPY.h", "volume": "1.20", "sl": "149.500", "tp": "151.250"})
        self.assertAlmostEqual(res.fill_price, 150.123)

    def test_rejection(self):
        ex = self.make(order_outcome="rejected", reject_text="Invalid stops")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "rejected")
        self.assertIn("Invalid stops", res.message)
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertEqual(self.fake.positions, [])
        self.assertNoDialogs()
        self.assertEvidence(res, 2)

    def test_dialog_vanishes_after_click_is_uncertain(self):
        ex = self.make(order_outcome="vanish")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertTrue(res.message.startswith("UNCERTAIN_EXECUTION"), res.message)
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertNoDialogs()

    def test_no_reaction_is_uncertain_and_never_presses_return(self):
        ex = self.make(order_outcome="nothing")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertIn("no confirmed result", res.message)
        # the market-execution warning ("... will be executed ...") must not count as a fill
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertEqual(self.fake.dangerous_returns, 0)
        self.assertNotIn("return", self.fake.keys())
        self.assertNoDialogs()  # closed with Escape

    def test_unknown_result_text_is_uncertain(self):
        ex = self.make(order_outcome="unknown")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertIn("waiting for the trade server", res.message)
        self.assertNoDialogs()

    def test_done_without_ticket_or_price_is_uncertain(self):
        ex = self.make(order_outcome="done_no_ticket")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertIn("no ticket or price", res.message)

    def test_result_for_other_side_is_uncertain(self):
        ex = self.make(order_outcome="wrong_side")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertIn("RESULT_MISMATCH", res.message)

    def test_exception_right_after_click_returns_uncertain(self):
        ex = self.make(raise_after_click="click")
        with self.assertLogs("tvbridge.executors.mt5gui", "ERROR"):
            res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertIn("RuntimeError", res.message)
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertEqual(self.fake.dangerous_returns, 0)

    def test_exception_while_reading_result_returns_uncertain(self):
        ex = self.make(raise_after_click="capture")
        with self.assertLogs("tvbridge.executors.mt5gui", "ERROR"):
            res = ex.open_market(buy_req())
        self.assertEqual(res.status, "uncertain")
        self.assertIn("screencapture failed", res.message)
        self.assertEqual(len(self.fake.order_button_clicks), 1)

    def test_bad_request_touches_nothing(self):
        ex = self.make()
        for req in (buy_req(sl=None), buy_req(sl=0.0), buy_req(lots=0), buy_req(side="close"),
                    buy_req(symbol="")):
            res = ex.open_market(req)
            self.assertEqual(res.status, "error")
            self.assertTrue(res.message.startswith("BAD_REQUEST"), res.message)
        self.assertEqual(self.fake.actions, [])


class OpenMarketFaultTests(GuiTestCase):
    def test_dialog_not_opened(self):
        ex = self.make(dialog_opens=False)
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "ORDER_DIALOG_NOT_OPENED")
        self.assertNoOrderButtonClicked()
        self.assertEqual(self.fake.typed(), [])
        # it waited (in fake time) for the dialog timeout
        self.assertGreaterEqual(self.fake.now, self.cfg.executor.gui.dialog_timeout_s - 0.11)

    def test_dialog_opens_after_delay(self):
        ex = self.make(dialog_delay_s=1.5)
        self.assertEqual(ex.open_market(buy_req()).status, "filled")

    def test_wrong_dialog_size(self):
        ex = self.make(dialog_size=(700.0, 470.0))
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "DIALOG_LAYOUT_CHANGED")
        self.assertIn("700x470", cm.exception.message)
        self.assertNoOrderButtonClicked()
        self.assertEqual(self.fake.typed(), [])
        self.assertIn("escape", self.fake.keys())
        self.assertNoDialogs()

    def test_size_within_tolerance_is_accepted(self):
        ex = self.make(dialog_size=(630.0, 462.0))
        self.assertEqual(ex.open_market(buy_req()).status, "filled")

    def test_volume_ignores_typing_fails_verification(self):
        ex = self.make(volume_ignores_typing=True)
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("VERIFY_FAILED"), res.message)
        self.assertIn("0.50", res.message)
        self.assertNoOrderButtonClicked()
        self.assertNoDialogs()
        self.assertEqual(self.fake.dangerous_returns, 0)
        self.assertEvidence(res, 1)

    def test_swapped_button_labels(self):
        ex = self.make(swap_buttons=True)
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("BUTTON_LABEL_MISMATCH"), res.message)
        self.assertNoOrderButtonClicked()
        self.assertNoDialogs()

    def test_wrong_account_title(self):
        ex = self.make(main_title="87654321 - HantecMarketsMU-MT5: Demo Account - Hedge")
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "WRONG_ACCOUNT")
        self.assertIn("account_login", cm.exception.message)
        self.assertEqual(self.fake.clicks(), [])
        self.assertEqual(self.fake.keys(), [])

    def test_wrong_server_title(self):
        ex = self.make(main_title="12345678 - OtherBroker-Server: Demo Account - Hedge")
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "WRONG_ACCOUNT")
        self.assertIn("server_name", cm.exception.message)

    def test_account_checks_disabled_when_not_configured(self):
        cfg = config_from_dict({"server": {"secret": SECRET}}, self.home)
        ex = self.make(cfg=cfg, main_title="99999999 - Some-Server")
        self.assertEqual(ex.read_account().balance, 50000.0)

    def test_main_title_contains(self):
        cfg = make_cfg(self.home, executor={"gui": {"main_title_contains": "NotThere"}})
        ex = self.make(cfg=cfg)
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "MT5_NOT_FOUND")

    def test_mt5_not_running(self):
        ex = self.make(mt5_running=False)
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "MT5_NOT_FOUND")
        self.assertEqual(self.fake.keys(), [])

    def test_closable_stray_dialog_is_dismissed(self):
        ex = self.make(stray_dialog="closable")
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual(self.fake.keys()[0], "escape")

    def test_stuck_stray_dialog(self):
        ex = self.make(stray_dialog="stuck")
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "STRAY_DIALOG")
        self.assertIn("Alert", cm.exception.message)
        self.assertEqual(self.fake.keys(), ["escape", "escape"])
        self.assertNotIn("f9", self.fake.keys())
        self.assertNoOrderButtonClicked()

    def test_gui_busy_when_another_process_holds_the_lock(self):
        ex = self.make()
        ex._gui_lock.timeout = 0.2
        with open(str(self.home / "gui.lock"), "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # a separate open file description conflicts, as another process would
            with self.assertRaises(ExecutorError) as cm:
                ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "GUI_BUSY")
        self.assertEqual(self.fake.actions, [])
        # released: works again
        self.assertEqual(ex.open_market(buy_req()).status, "filled")


class RehearsalTests(GuiTestCase):
    rehearsal = True

    def test_rehearsal_fills_verifies_escapes_and_never_clicks(self):
        ex = self.make()
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "rehearsed", res.message)
        self.assertIn("nothing was sent", res.message)
        self.assertEqual(self.fake.typed(), ["EURUSD.h", "0.50", "1.08000", "1.09000"])
        self.assertNoOrderButtonClicked()
        self.assertEqual(self.fake.keys()[-1], "escape")
        self.assertNoDialogs()
        self.assertEqual(self.fake.positions, [])
        self.assertEvidence(res, 1)

    def test_rehearsal_still_fails_verification(self):
        ex = self.make(volume_ignores_typing=True)
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("VERIFY_FAILED"))
        self.assertNoOrderButtonClicked()

    def test_rehearsal_checks_button_labels(self):
        ex = self.make(swap_buttons=True)
        res = ex.open_market(buy_req(side="sell", sl=1.09, tp=None))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("BUTTON_LABEL_MISMATCH"))
        self.assertNoOrderButtonClicked()

    def test_rehearsal_close_never_clicks_close(self):
        ex = self.make(positions=[pos(111111), pos(222222)])
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["rehearsed"])
        self.assertIn("Close #111111", results[0].message)
        self.assertEqual(self.fake.close_button_clicks, [])
        self.assertEqual(len(self.fake.positions), 2)
        self.assertNoDialogs()

    def test_rehearsal_reads_account(self):
        ex = self.make(positions=[pos(111111)])
        snap = ex.read_account()
        self.assertEqual(len(snap.positions), 1)


class ReadAccountTests(GuiTestCase):
    def test_parses_balance_equity_and_positions(self):
        ex = self.make(positions=[pos(111111, profit=-17.0), pos(222222, "GBPUSD.h", "sell", 0.3, 1.30123, 4.5)],
                       balance=50000.0)
        snap = ex.read_account()
        self.assertEqual(snap.source, "mt5gui")
        self.assertEqual(snap.ts, T0)
        self.assertAlmostEqual(snap.balance, 50000.0)
        self.assertAlmostEqual(snap.equity, 49987.5)
        self.assertAlmostEqual(snap.margin, 1085.0)
        self.assertAlmostEqual(snap.free_margin, 49987.5 - 1085.0)
        got = sorted((p.symbol, p.side, p.lots) for p in snap.positions)
        self.assertEqual(got, [("EURUSD.h", "buy", 0.5), ("GBPUSD.h", "sell", 0.3)])
        # read-only: no clicks, no keys
        self.assertEqual(self.fake.clicks(), [])
        self.assertEqual(self.fake.keys(), [])
        # routine polls keep only the latest screenshot
        day = self.shots / "20261001"
        self.assertEqual(sorted(p.name for p in day.iterdir()), [ACCOUNT_LATEST_NAME])

    def test_no_positions(self):
        ex = self.make()
        snap = ex.read_account()
        self.assertEqual(snap.positions, [])
        self.assertAlmostEqual(snap.equity, 50000.0)

    def test_unreadable_then_readable_after_trade_tab(self):
        ex = self.make(toolbox_tab="journal", positions=[pos(111111)])
        snap = ex.read_account()
        self.assertAlmostEqual(snap.balance, 50000.0)
        self.assertEqual(len(snap.positions), 1)
        tab = (MAIN_RECT[0] + TRADE_TAB_POINT[0], MAIN_RECT[1] + TRADE_TAB_POINT[1], 1)
        self.assertIn(tab, self.fake.clicks())
        self.assertEqual(self.fake.toolbox_tab, "trade")

    def test_unreadable(self):
        ex = self.make(toolbox_tab="journal", trade_tab_works=False)
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "ACCOUNT_UNREADABLE")
        self.assertIn(".png", cm.exception.message)

    def test_unreadable_without_trade_tab_point_does_not_click(self):
        ex = self.make(toolbox_tab="journal", calib=make_calibration(trade_tab_point=None))
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "ACCOUNT_UNREADABLE")
        self.assertEqual(self.fake.clicks(), [])

    def test_low_confidence_items_are_dropped(self):
        ex = self.make()
        items, _png = ex._ocr(self.fake.main_window(), None, "t")
        self.assertTrue(items)
        self.assertTrue(all(i.conf >= self.cfg.executor.gui.ocr_min_confidence for i in items))


class ClosePositionsTests(GuiTestCase):
    def test_closes_two_rows_of_same_symbol_one_by_one(self):
        ex = self.make(positions=[pos(111111), pos(333333, "GBPUSD.h"), pos(222222, lots=0.2)])
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["filled", "filled"], [r.message for r in results])
        self.assertEqual([r.ticket for r in results], ["111111", "222222"])
        self.assertEqual([r.fill_price for r in results], [1.084, 1.084])
        self.assertEqual(self.fake.close_button_clicks, ["111111", "222222"])
        self.assertEqual([p["ticket"] for p in self.fake.positions], ["333333"])
        double_clicks = [c for c in self.fake.clicks() if c[2] == 2]
        self.assertEqual(len(double_clicks), 2)
        # each close button click landed on the button text found by OCR
        bx = POSITION_DIALOG_ORIGIN[0] + CLOSE_BUTTON_POINT[0]
        by = POSITION_DIALOG_ORIGIN[1] + CLOSE_BUTTON_POINT[1]
        self.assertEqual(self.fake.clicks().count((bx, by, 1)), 2)
        self.assertNoDialogs()
        self.assertEqual(self.fake.dangerous_returns, 0)
        for r in results:
            self.assertEvidence(r, 2)

    def test_side_filter(self):
        ex = self.make(positions=[pos(111111, side="buy"), pos(222222, side="sell")])
        results = ex.close_positions("EURUSD.h", side="sell")
        self.assertEqual([r.ticket for r in results], ["222222"])
        self.assertEqual([p["ticket"] for p in self.fake.positions], ["111111"])

    def test_tv_symbol_accepted(self):
        ex = self.make(positions=[pos(111111)])
        self.assertEqual([r.status for r in ex.close_positions("EURUSD")], ["filled"])

    def test_returns_empty_when_nothing_matches(self):
        ex = self.make(positions=[pos(111111, side="buy")])
        self.assertEqual(ex.close_positions("GBPUSD.h"), [])
        self.assertEqual(ex.close_positions("EURUSD.h", side="sell"), [])
        self.assertEqual(self.fake.clicks(), [])
        self.assertEqual(self.fake.keys(), [])
        self.assertEqual(len(self.fake.positions), 1)

    def test_unreadable_toolbox_raises_instead_of_reporting_nothing(self):
        ex = self.make(positions=[pos(111111)], toolbox_tab="journal", trade_tab_works=False)
        with self.assertRaises(ExecutorError) as cm:
            ex.close_positions("EURUSD.h")
        self.assertEqual(cm.exception.code, "ACCOUNT_UNREADABLE")

    def test_rejected_close_stops(self):
        ex = self.make(positions=[pos(111111), pos(222222)], close_outcome="rejected")
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["rejected"])
        self.assertIn("Market is closed", results[0].message)
        self.assertEqual(self.fake.close_button_clicks, ["111111"])
        self.assertEqual(len(self.fake.positions), 2)
        self.assertNoDialogs()

    def test_dialog_vanishing_but_position_gone_counts_as_closed(self):
        ex = self.make(positions=[pos(111111)], close_outcome="vanish")
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["filled"], results[0].message)
        self.assertIn("no longer listed", results[0].message)
        self.assertEqual(results[0].ticket, "111111")

    def test_dialog_vanishing_with_position_still_listed_is_uncertain(self):
        ex = self.make(positions=[pos(111111), pos(222222)], close_outcome="vanish_keep")
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["uncertain"])
        self.assertEqual(len(self.fake.close_button_clicks), 1)

    def test_ticket_reported_closed_is_never_closed_twice(self):
        ex = self.make(positions=[pos(111111)], close_outcome="done_keep")
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["filled", "uncertain"])
        self.assertTrue(results[1].message.startswith("POSITION_STILL_LISTED"), results[1].message)
        self.assertEqual(self.fake.close_button_clicks, ["111111"])
        self.assertEqual(len([c for c in self.fake.clicks() if c[2] == 2]), 1)

    def test_close_button_not_found(self):
        ex = self.make(positions=[pos(111111)], close_button_text="C1ose #111111 buy 0.50 EURUSD.h by Market")
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["error"])
        self.assertTrue(results[0].message.startswith("CLOSE_BUTTON_NOT_FOUND"))
        self.assertEqual(self.fake.close_button_clicks, [])
        self.assertNoDialogs()

    def test_close_button_for_other_ticket_is_refused(self):
        ex = self.make(positions=[pos(111111)], close_button_text="Close #999999 buy 0.50 EURUSD.h by Market")
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["error"])
        self.assertIn("#999999", results[0].message)
        self.assertEqual(self.fake.close_button_clicks, [])

    def test_exception_after_close_click_is_uncertain(self):
        ex = self.make(positions=[pos(111111)])
        original = self.fake.click

        def click(x, y, count=1):
            original(x, y, count)
            if self.fake.close_button_clicks:
                raise RuntimeError("boom")

        self.fake.click = click
        with self.assertLogs("tvbridge.executors.mt5gui", "ERROR"):
            results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["uncertain"])

    def test_failure_after_a_successful_close_keeps_earlier_results(self):
        ex = self.make(positions=[pos(111111), pos(222222)])
        real_main = ex._main_window
        calls = {"n": 0}

        def flaky_main():
            calls["n"] += 1
            if calls["n"] > 1 and self.fake.close_button_clicks:
                raise ExecutorError("MT5_NOT_FOUND", "gone")
            return real_main()

        ex._main_window = flaky_main
        with self.assertLogs("tvbridge.executors.mt5gui", "ERROR"):
            results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["filled", "error"])
        self.assertTrue(results[1].message.startswith("MT5_NOT_FOUND"))


class CloseAllTests(GuiTestCase):
    def test_close_all_over_several_symbols(self):
        ex = self.make(positions=[pos(111111), pos(222222, "GBPUSD.h", "sell", 0.3),
                                  pos(333333, "USDJPY.h", "buy", 1.0, 150.123), pos(444444, lots=0.1)])
        results = ex.close_all()
        self.assertEqual([r.status for r in results], ["filled"] * 4, [r.message for r in results])
        self.assertEqual(sorted(r.ticket for r in results), ["111111", "222222", "333333", "444444"])
        self.assertEqual(self.fake.positions, [])
        self.assertNoDialogs()

    def test_close_all_continues_after_a_failure(self):
        ex = self.make(positions=[pos(111111), pos(222222, "GBPUSD.h", "sell", 0.3)], close_outcome="rejected")
        results = ex.close_all()
        self.assertEqual([r.status for r in results], ["rejected", "rejected"])
        self.assertEqual(self.fake.close_button_clicks, ["111111", "222222"])

    def test_close_all_with_nothing_open(self):
        ex = self.make()
        self.assertEqual(ex.close_all(), [])
        self.assertEqual(self.fake.clicks(), [])


class HealthTests(GuiTestCase):
    def test_ok(self):
        ex = self.make()
        h = ex.health()
        self.assertTrue(h["ok"], h)
        self.assertIn(MAIN_TITLE, h["detail"])
        self.assertEqual(h["mode"], "live")

    def test_missing_permission(self):
        ex = self.make(permissions={"accessibility": True, "screen_recording": False})
        h = ex.health()
        self.assertFalse(h["ok"])
        self.assertIn("screen recording", h["detail"])

    def test_mt5_missing(self):
        ex = self.make(mt5_running=False)
        h = ex.health()
        self.assertFalse(h["ok"])
        self.assertIn("MT5_NOT_FOUND", h["detail"])


class MakeExecutorGuiTests(unittest.TestCase):
    def setUp(self):
        clock.set_clock(lambda: T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TVBRIDGE_HOME": self.tmp.name})
        self.env.start()
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()
        clock.set_clock(None)

    def cfg(self, mode, login=""):
        cfg = config_from_dict({"server": {"secret": SECRET}, "executor": {"mode": mode},
                                "account": {"account_login": login}}, default_home())
        self.assertEqual(cfg.home, Path(self.tmp.name))
        return cfg

    def test_rehearsal_and_live_with_injected_driver(self):
        for mode, rehearsal in (("rehearsal", True), ("live", False)):
            cfg = self.cfg(mode, login="12345678")
            save_calibration(cfg.calibration_path, make_calibration())
            fake = FakeMt5Driver()
            ex = make_executor(cfg, self.store, driver=fake)
            self.assertIsInstance(ex, Mt5GuiExecutor)
            self.assertIs(ex.driver, fake)
            self.assertEqual(ex.rehearsal, rehearsal)
            self.assertEqual(ex.shots_dir, cfg.shots_dir)
            self.assertEqual(ex.calib.order_dialog["w"], 620.0)

    def test_rehearsal_end_to_end(self):
        cfg = self.cfg("rehearsal")
        save_calibration(cfg.calibration_path, make_calibration())
        fake = FakeMt5Driver()
        ex = make_executor(cfg, self.store, driver=fake)
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "rehearsed", res.message)
        self.assertEqual(fake.order_button_clicks, [])
        self.assertTrue(res.evidence)
        self.assertTrue(all(Path(p).is_relative_to(cfg.shots_dir) for p in res.evidence))

    def test_missing_calibration(self):
        cfg = self.cfg("live", login="12345678")
        with self.assertRaises(CalibrationError):
            make_executor(cfg, self.store, driver=FakeMt5Driver())


# ---------------------------------------------------------------------------------- review fixes


class ToolboxCompletenessTests(GuiTestCase):
    """A partly visible Trade list is reported as unknown, never as complete."""

    def three(self):
        return [pos(52390601, profit=-900.0), pos(52390602, "GBPUSD.h", "sell", 0.3, 1.30123, -300.0),
                pos(52390603, "USDJPY.h", "buy", 0.2, 150.123, -150.0)]

    def test_complete_list_is_trusted(self):
        ex = self.make(positions=self.three())
        snap = ex.read_account()
        self.assertEqual(len(snap.positions), 3)
        self.assertEqual(snap.positions_note, "")

    def test_shorter_main_window_is_incomplete_but_equity_is_read(self):
        ex = self.make(positions=self.three(), main_size=(1440.0, 835.0))
        snap = ex.read_account()
        self.assertAlmostEqual(snap.equity, 50000.0 - 1350.0)
        self.assertIsNone(snap.positions)
        self.assertTrue(snap.positions_note.startswith("TOOLBOX_INCOMPLETE"), snap.positions_note)
        self.assertIn("835", snap.positions_note)

    def test_taller_toolbox_same_window_is_incomplete(self):
        ex = self.make(positions=self.three(), toolbox_shift=40.0)
        snap = ex.read_account()
        self.assertIsNone(snap.positions)
        self.assertIn("header", snap.positions_note)

    def test_unreadable_row_leaves_a_gap(self):
        ex = self.make(positions=self.three(), hidden_tickets=["52390602"])
        snap = ex.read_account()
        self.assertIsNone(snap.positions)
        self.assertIn("gap", snap.positions_note)

    def test_hidden_profit_is_noticed(self):
        # equity - balance shows 500 more loss than the readable rows: a row is missing
        ex = self.make(positions=self.three(), equity=50000.0 - 1350.0 - 500.0)
        snap = ex.read_account()
        self.assertIsNone(snap.positions)
        self.assertIn("row is missing", snap.positions_note)
        # rounding, commission and a little swap are tolerated
        ex = self.make(positions=self.three(), equity=50000.0 - 1350.0 - 4.0)
        self.assertEqual(len(ex.read_account().positions), 3)

    def test_no_rows_but_margin_is_incomplete(self):
        ex = self.make()
        main = self.fake.main_window()
        items, _png, acct = ex._scan_toolbox(main, "t")
        acct = dict(acct, margin=1085.0)
        self.assertIn("margin", ex._toolbox_issue(main, items, acct, []))

    def test_close_all_closes_visible_rows_and_reports_the_rest(self):
        ex = self.make(positions=self.three(), main_size=(1440.0, 835.0))
        results = ex.close_all()
        self.assertEqual(results[-1].status, "uncertain")
        self.assertTrue(results[-1].message.startswith("TOOLBOX_INCOMPLETE"), results[-1].message)
        self.assertEqual(sorted(r.ticket for r in results[:-1] if r.status == "filled"), ["52390602", "52390603"])
        self.assertEqual([p["ticket"] for p in self.fake.positions], ["52390601"])   # hidden: still open

    def test_close_all_with_nothing_visible_is_not_empty(self):
        ex = self.make(positions=[pos(52390601, profit=-900.0)], hidden_tickets=["52390601"])
        results = ex.close_all()
        self.assertEqual([r.status for r in results], ["uncertain"])
        self.assertIn("TOOLBOX_INCOMPLETE", results[0].message)

    def test_close_positions_reports_incomplete_list(self):
        ex = self.make(positions=self.three(), toolbox_shift=40.0)
        results = ex.close_positions("GBPUSD.h")
        self.assertEqual([r.status for r in results], ["filled", "uncertain"])
        self.assertIn("TOOLBOX_INCOMPLETE", results[-1].message)

    def test_ticket_listed_is_unknown_when_incomplete(self):
        ex = self.make(positions=self.three(), toolbox_shift=40.0)
        self.assertIsNone(ex._ticket_listed("52390601"))
        ex2 = self.make(positions=self.three())
        self.assertTrue(ex2._ticket_listed("52390601"))
        self.assertFalse(ex2._ticket_listed("99999999"))

    def test_entry_refused_when_main_window_size_changed(self):
        ex = self.make(main_size=(1440.0, 835.0))
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "MAIN_LAYOUT_CHANGED")
        self.assertEqual(self.fake.keys(), [])
        self.assertEqual(self.fake.clicks(), [])
        self.assertFalse(ex.health()["ok"])

    def test_trade_tab_not_clicked_when_window_size_changed(self):
        ex = self.make(toolbox_tab="journal", main_size=(1440.0, 835.0))
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "ACCOUNT_UNREADABLE")
        self.assertEqual(self.fake.clicks(), [])


class MainWindowSelectionTests(GuiTestCase):
    def other(self, title, w=1600.0, h=950.0, pid=7777, wid=900):
        return Window(wid=wid, pid=pid, owner=OWNER, title=title, x=0.0, y=25.0, w=w, h=h)

    def test_bigger_other_wine_window_is_ignored_when_login_is_set(self):
        ex = self.make(extra_windows=[self.other("MetaEditor - [tvb_indicator.mq5]")], positions=[pos(111111)])
        snap = ex.read_account()
        self.assertEqual(len(snap.positions), 1)
        self.assertEqual(ex.close_all()[0].status, "filled")
        self.assertTrue(ex.health()["ok"], ex.health())

    def test_other_terminal_without_login_uses_calibrated_size(self):
        cfg = config_from_dict({"server": {"secret": SECRET}}, self.home)
        ex = self.make(cfg=cfg, extra_windows=[self.other("87654321: HantecMarkets-Demo - Hedge")])
        self.assertEqual(ex._main_window().wid, self.fake.main_window().wid)

    def test_two_equally_good_windows_are_ambiguous(self):
        cfg = config_from_dict({"server": {"secret": SECRET}}, self.home)
        twin = self.other("87654321: HantecMarkets-Demo - Hedge", w=MAIN_RECT[2], h=MAIN_RECT[3])
        ex = self.make(cfg=cfg, extra_windows=[twin])
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "AMBIGUOUS_MAIN_WINDOW")

    def test_login_must_match_as_a_whole_number(self):
        self.assertTrue(login_in_title("12345678", "12345678 - HantecMarketsMU-MT5"))
        self.assertTrue(login_in_title("12345678", "Account 12345678: Demo"))
        self.assertFalse(login_in_title("12345678", "112345678 - HantecMarketsMU-MT5"))
        self.assertFalse(login_in_title("12345678", "123456789 - HantecMarketsMU-MT5"))
        ex = self.make(main_title="112345678 - HantecMarketsMU-MT5: Demo Account - Hedge")
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertEqual(cm.exception.code, "WRONG_ACCOUNT")


class ResultClassificationTests(GuiTestCase):
    def test_timeout_after_click_is_uncertain_not_rejected(self):
        for text in ("Request canceled by timeout", "No connection with the trade server", "Trade timeout"):
            ex = self.make(order_outcome="custom", custom_result_text=text)
            res = ex.open_market(buy_req())
            self.assertEqual(res.status, "uncertain", (text, res.message))
            self.assertTrue(res.message.startswith("UNCERTAIN_EXECUTION"), res.message)

    def test_definitive_rejection_stays_rejected(self):
        ex = self.make(order_outcome="custom", custom_result_text="Invalid stops")
        self.assertEqual(ex.open_market(buy_req()).status, "rejected")

    def test_volume_symbol_and_price_of_a_fill_are_checked(self):
        cases = [
            ("Done: buy 1.00 EURUSD.h at 1.08345 #52390671", "volume"),
            ("Done: buy 0.50 GBPUSD.h at 1.08345 #52390671", "GBPUSD.h"),
            ("Done: buy 0.50 at 150.123 #52390671", "far from the alert price"),
        ]
        for text, needle in cases:
            ex = self.make(order_outcome="custom_fill", custom_result_text=text)
            res = ex.open_market(buy_req())
            self.assertEqual(res.status, "uncertain", (text, res.message))
            self.assertTrue(res.message.startswith("RESULT_MISMATCH"), res.message)
            self.assertIn(needle, res.message)


class PreClickTests(GuiTestCase):
    def test_unexpected_error_before_the_click_is_an_executor_error(self):
        ex = self.make(capture_fails=True)
        with self.assertLogs("tvbridge.executors.mt5gui", "ERROR"):
            with self.assertRaises(ExecutorError) as cm:
                ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "GUI_ERROR")
        self.assertIn("nothing was sent", cm.exception.message)
        self.assertNoOrderButtonClicked()
        self.assertNoDialogs()
        # the blank capture was not left on disk
        day = self.shots / "20261001"
        self.assertEqual([p.name for p in day.iterdir()] if day.exists() else [], [])

    def test_abort_check_stops_before_the_click(self):
        ex = self.make()
        ex.abort_check = lambda: "entries paused"
        res = ex.open_market(buy_req())
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("ABORTED: entries paused"), res.message)
        self.assertNoOrderButtonClicked()
        self.assertNoDialogs()
        ex.abort_check = lambda: None
        self.assertEqual(ex.open_market(buy_req()).status, "filled")

    def test_failing_abort_check_fails_closed(self):
        ex = self.make()

        def boom():
            raise RuntimeError("db locked")

        ex.abort_check = boom
        res = ex.open_market(buy_req())
        self.assertTrue(res.message.startswith("ABORTED"), res.message)
        self.assertNoOrderButtonClicked()


class ExitStrayWindowTests(GuiTestCase):
    def test_non_modal_stuck_window_does_not_block_closes(self):
        ex = self.make(positions=[pos(111111)], stray_dialog="stuck", stray_modal=False)
        results = ex.close_all()
        self.assertEqual([r.status for r in results], ["filled"], [r.message for r in results])
        self.assertEqual(self.fake.positions, [])
        # entries still refuse to work around it
        with self.assertRaises(ExecutorError) as cm:
            ex.open_market(buy_req())
        self.assertEqual(cm.exception.code, "STRAY_DIALOG")

    def test_stray_over_the_row_is_never_clicked(self):
        row_y = MAIN_RECT[1] + ROW_TOP
        ex = self.make(positions=[pos(111111)], stray_dialog="stuck", stray_modal=False,
                       stray_rect=(0.0, row_y - 20, 200.0, 60.0))
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["error"])
        self.assertTrue(results[0].message.startswith("STRAY_DIALOG"), results[0].message)
        self.assertEqual([c for c in self.fake.clicks() if c[2] == 2], [])
        self.assertEqual(len(self.fake.positions), 1)

    def test_stray_over_the_focus_point_gets_no_focus_click(self):
        fx, fy = MAIN_RECT[0] + FOCUS_POINT[0], MAIN_RECT[1] + FOCUS_POINT[1]
        ex = self.make(positions=[pos(111111)], stray_dialog="stuck", stray_modal=False,
                       stray_rect=(fx - 50, fy - 10, 200.0, 100.0))
        results = ex.close_positions("EURUSD.h")
        self.assertEqual([r.status for r in results], ["filled"], [r.message for r in results])
        self.assertNotIn((fx, fy, 1), self.fake.clicks())


class UnknownSymbolCloseTests(GuiTestCase):
    def test_position_without_spec_is_read_and_closed_by_close_all(self):
        ex = self.make(positions=[pos(111111), pos(777777, "BTCUSD.h", "buy", 0.1, 60000.0, -25.0)])
        snap = ex.read_account()
        self.assertEqual(sorted(p.symbol for p in snap.positions), ["BTCUSD.h", "EURUSD.h"])
        results = ex.close_all()
        self.assertEqual(sorted(r.ticket for r in results if r.status == "filled"), ["111111", "777777"])
        self.assertEqual(self.fake.positions, [])


class FailedReadScreenshotTests(GuiTestCase):
    def test_failure_streak_keeps_first_and_latest_only(self):
        ex = self.make(toolbox_tab="journal", trade_tab_works=False)
        for _ in range(20):
            with self.assertRaises(ExecutorError):
                ex.read_account()
        day = self.shots / "20261001"
        names = sorted(p.name for p in day.iterdir())
        self.assertIn(ACCOUNT_FAILED_LATEST_NAME, names)
        self.assertLessEqual(len(names), 2, names)
        # a success resets the streak; the next failure keeps its own first screenshot
        self.fake.trade_tab_works = True
        ex.read_account()
        self.fake.toolbox_tab = "journal"
        self.fake.trade_tab_works = False
        with self.assertRaises(ExecutorError) as cm:
            ex.read_account()
        self.assertIn(ACCOUNT_FAILED_LATEST_NAME, cm.exception.message)
        names = sorted(p.name for p in day.iterdir())
        self.assertLessEqual(len(names), 4, names)


class MakeExecutorLoginTests(MakeExecutorGuiTests):
    def test_live_needs_account_login(self):
        cfg = self.cfg("live")
        save_calibration(cfg.calibration_path, make_calibration())
        with self.assertRaises(ExecutorError) as cm:
            make_executor(cfg, self.store, driver=FakeMt5Driver())
        self.assertEqual(cm.exception.code, "ACCOUNT_LOGIN_REQUIRED")
        cfg = config_from_dict({"server": {"secret": SECRET}, "executor": {"mode": "live"},
                                "account": {"account_login": "12345678"}}, default_home())
        self.assertIsInstance(make_executor(cfg, self.store, driver=FakeMt5Driver()), Mt5GuiExecutor)


if __name__ == "__main__":
    unittest.main()


class SymbolSelectionTests(GuiTestCase):
    """MT5 keeps the previous symbol when the typed one is not in its Market Watch."""

    def test_normal_entry_sees_the_symbol_in_the_title(self):
        ex = self.make(market_watch=["EURUSD.h", "XAGUSD.h"], fill_price=60.01)
        res = ex.open_market(buy_req(symbol="XAGUSD.h", sl=59.0, tp=61.0, digits=3, price=60.0))
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual(self.fake.orders_sent[0]["symbol"], "XAGUSD.h")
        self.assertEqual(self.fake.shown_symbols, [])

    def test_refused_symbol_is_never_sent(self):
        ex = self.make(market_watch=["EURUSD.h"])
        ex.can_add_symbols = False
        res = ex.open_market(buy_req(symbol="XAGUSD.h", sl=59.0, tp=61.0, digits=3, price=60.0))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("SYMBOL_NOT_SELECTED"), res.message)
        self.assertIn("Market Watch", res.message)
        self.assertNoOrderButtonClicked()
        self.assertNoDialogs()
        self.assertEqual(self.fake.typed(), ["XAGUSD.h"])
        self.assertEqual(self.fake.shown_symbols, [])

    def test_missing_symbol_is_added_through_the_symbols_window_then_sent(self):
        ex = self.make(market_watch=["EURUSD.h"], fill_price=60.01)
        ex.can_add_symbols = True
        res = ex.open_market(buy_req(symbol="XAGUSD.h", sl=59.0, tp=61.0, digits=3, price=60.0))
        self.assertEqual(res.status, "filled", res.message)
        self.assertEqual(self.fake.shown_symbols, ["XAGUSD.h"])
        self.assertEqual(self.fake.market_watch, ["EURUSD.h", "XAGUSD.h"])
        self.assertEqual(self.fake.orders_sent, [
            {"side": "buy", "symbol": "XAGUSD.h", "volume": "0.50", "sl": "59.000", "tp": "61.000"}])
        self.assertEqual(self.fake.order_button_clicks, ["buy"])
        self.assertNoDialogs()
        # typed: order window (refused), Symbols search, order window again, then the fields
        self.assertEqual(self.fake.typed(), ["XAGUSD.h", "XAGUSD.h", "XAGUSD.h", "0.50", "59.000", "61.000"])
        self.assertIn(("key", "u", ("ctrl",)), self.fake.actions)

    def test_symbol_unknown_to_the_broker_is_refused_after_one_attempt(self):
        ex = self.make(market_watch=["EURUSD.h"])
        ex.can_add_symbols = True
        res = ex.open_market(buy_req(symbol="XPTUSD.h", sl=900.0, tp=1000.0, digits=2, price=950.0))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("SYMBOL_NOT_SELECTED"), res.message)
        self.assertNoOrderButtonClicked()
        self.assertNoDialogs()          # the Symbols window was closed again
        self.assertEqual(self.fake.shown_symbols, [])

    def test_rehearsal_also_refuses_a_missing_symbol(self):
        ex = self.make(rehearsal=True, market_watch=["EURUSD.h"])
        ex.can_add_symbols = False
        res = ex.open_market(buy_req(symbol="XAGUSD.h", sl=59.0, tp=61.0, digits=3, price=60.0))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.message.startswith("SYMBOL_NOT_SELECTED"), res.message)
        self.assertNoDialogs()
