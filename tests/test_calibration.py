import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tvbridge.config import config_from_dict
from tvbridge.gui.calibration import (
    POINT_NAMES, Calibration, CalibrationError, load_calibration, run_wizard, save_calibration,
)
from tvbridge.gui.driver import Driver, OcrItem, Window

SECRET = "s" * 24

MAIN = Window(wid=10, pid=500, owner="MetaTrader 5", title="12345678 - Demo-Server: Demo Account - Hedge",
              x=0.0, y=25.0, w=1400.0, h=900.0)
DIALOG = Window(wid=11, pid=500, owner="MetaTrader 5", title="Order", x=300.0, y=200.0, w=620.0, h=460.0)
OTHER_APP = Window(wid=99, pid=777, owner="Finder", title="Downloads", x=0.0, y=0.0, w=1600.0, h=1000.0)
HELPER = Window(wid=12, pid=500, owner="MetaTrader 5", title="", x=0.0, y=0.0, w=1.0, h=1.0)   # Wine helper

# window-relative points the "human" hovers over
DLG_POINTS = {"symbol": (200.0, 40.0), "volume": (150.0, 100.0), "sl": (150.0, 140.0), "tp": (400.0, 140.0),
              "sell": (150.0, 400.0), "buy": (450.0, 400.0)}
FOCUS = (600.0, 10.0)
TOOLBOX_TL = (10.0, 620.0)
TOOLBOX_BR = (1390.0, 880.0)
TRADE_TAB = (60.0, 890.0)


def glob(win, pt):
    return (win.x + pt[0], win.y + pt[1])


def centered(text, win, pt, w=80.0, h=14.0):
    gx, gy = glob(win, pt)
    return OcrItem(text=text, conf=0.9, x=gx - w / 2, y=gy - h / 2, w=w, h=h)


def dialog_items(swap=False, labels=True):
    sell_pt, buy_pt = (DLG_POINTS["buy"], DLG_POINTS["sell"]) if swap else (DLG_POINTS["sell"], DLG_POINTS["buy"])
    items = [
        centered("EURUSD.h, Euro vs US Dollar", DIALOG, (200, 40), w=160),
        centered("Market Execution", DIALOG, (200, 70), w=100),
        centered("Sell by Market", DIALOG, sell_pt),
        centered("Buy by Market", DIALOG, buy_pt),
    ]
    if labels:
        items += [
            centered("Volume:", DIALOG, (60, 100), w=50), centered("0.01", DIALOG, DLG_POINTS["volume"], w=30),
            centered("Stop Loss:", DIALOG, (60, 140), w=64), centered("0.00000", DIALOG, DLG_POINTS["sl"], w=50),
            centered("Take Profit:", DIALOG, (330, 140), w=70), centered("0.00000", DIALOG, DLG_POINTS["tp"], w=50),
        ]
    return items


def main_items(readable=True):
    items = [centered("Trade", MAIN, TRADE_TAB, w=30)]
    if readable:
        items.append(centered("EURUSD.h 52390671 2026.10.01 10:15:02 buy 0.50 1.08345 1.08100 1.08900 1.08311 -17.00",
                              MAIN, (500, 700), w=700))
        items.append(centered("Balance: 50 000.00 USD Equity: 49 812.35 Margin: 1 083.45 Free Margin: 48 728.90",
                              MAIN, (500, 760), w=700))
    return items


class FakeDriver(Driver):
    """Scripted driver: windows and mouse positions come from the test; any input event fails."""

    def __init__(self, windows, mouse, perms=None, ocr=None):
        self.windows = list(windows)
        self.mouse = list(mouse)
        self.perms = perms if perms is not None else {"accessibility": True, "screen_recording": True}
        self.ocr_by_wid = ocr or {}
        self.captures = []
        self.sleeps = []
        self.owner_queries = []
        self.on_sleep = None

    def list_windows(self, owner_names):
        self.owner_queries.append(list(owner_names))
        needles = [n.lower() for n in owner_names]
        return [w for w in self.windows if any(n in w.owner.lower() for n in needles)]

    def mouse_location(self):
        if not self.mouse:
            raise AssertionError("wizard asked for more mouse positions than scripted")
        return self.mouse.pop(0)

    def capture(self, win, out_path):
        self.captures.append((win.wid, out_path))
        return 2.0

    def ocr(self, png_path, win, scale, region=None):
        items = list(self.ocr_by_wid.get(win.wid, []))
        if region is not None:
            dx, dy, w, h = region
            x0, y0 = win.x + dx, win.y + dy
            items = [i for i in items if x0 <= i.cx <= x0 + w and y0 <= i.cy <= y0 + h]
        return items

    def sleep(self, s):
        self.sleeps.append(s)
        if self.on_sleep is not None:
            self.on_sleep()

    def permissions(self):
        return dict(self.perms)

    # the wizard must never send input to MT5
    def click(self, x, y, count=1):
        raise AssertionError("calibration wizard clicked")

    def key(self, name, mods=()):
        raise AssertionError("calibration wizard pressed a key")

    def type_text(self, text):
        raise AssertionError("calibration wizard typed")

    def activate(self, pid):
        raise AssertionError("calibration wizard activated an app")


def happy_mouse():
    seq = [glob(DIALOG, DLG_POINTS[n]) for n in POINT_NAMES]
    seq += [glob(MAIN, FOCUS), glob(MAIN, TOOLBOX_TL), glob(MAIN, TOOLBOX_BR), glob(MAIN, TRADE_TAB)]
    return seq


def valid_calibration_dict():
    return {
        "version": 1,
        "created_at": "2026-10-01T10:00:00Z",
        "main_window": {"title": "MT5", "w": 1400, "h": 900},
        "order_dialog": {"title": "Order", "w": 620, "h": 460,
                         "points": {k: list(v) for k, v in DLG_POINTS.items()}},
        "toolbox_region": [10, 620, 1380, 260],
        "focus_point": [600, 10],
        "trade_tab_point": [60, 890],
    }


class LoadSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "calibration.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")

    def test_round_trip_and_mode_600(self):
        calib = Calibration.from_dict(valid_calibration_dict())
        save_calibration(self.path, calib)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        loaded = load_calibration(self.path)
        self.assertEqual(loaded, calib)
        self.assertEqual(loaded.order_dialog["w"], 620.0)
        self.assertEqual(loaded.dialog_point("buy"), (450.0, 400.0))
        self.assertEqual(loaded.toolbox_region, [10.0, 620.0, 1380.0, 260.0])
        self.assertEqual(loaded.trade_tab_point, [60.0, 890.0])
        # no temp files left behind
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["calibration.json"])

    def test_overwrite_tightens_permissions(self):
        self.write("{}")
        os.chmod(self.path, 0o644)
        save_calibration(self.path, Calibration.from_dict(valid_calibration_dict()))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_save_creates_parent_dirs(self):
        p = self.dir / "a" / "b" / "calibration.json"
        save_calibration(str(p), Calibration.from_dict(valid_calibration_dict()))
        self.assertTrue(p.exists())

    def test_trade_tab_optional(self):
        d = valid_calibration_dict()
        d["trade_tab_point"] = None
        save_calibration(self.path, Calibration.from_dict(d))
        self.assertIsNone(load_calibration(self.path).trade_tab_point)
        del d["trade_tab_point"]
        self.assertIsNone(Calibration.from_dict(d).trade_tab_point)

    def test_missing_file(self):
        with self.assertRaises(CalibrationError) as cm:
            load_calibration(self.dir / "nope.json")
        msg = str(cm.exception)
        self.assertIn("missing", msg)
        self.assertIn("tvbridge calibrate", msg)

    def test_invalid_json(self):
        self.write("{not json")
        with self.assertRaises(CalibrationError) as cm:
            load_calibration(self.path)
        self.assertIn("invalid", str(cm.exception))

    def assert_invalid(self, mutate, needle):
        d = valid_calibration_dict()
        mutate(d)
        self.write(d)
        with self.assertRaises(CalibrationError) as cm:
            load_calibration(self.path)
        self.assertIn(needle, str(cm.exception))

    def test_invalid_structures(self):
        self.assert_invalid(lambda d: d.pop("toolbox_region"), "toolbox_region")
        self.assert_invalid(lambda d: d["order_dialog"]["points"].pop("buy"), "points.buy")
        self.assert_invalid(lambda d: d["order_dialog"].pop("points"), "points")
        self.assert_invalid(lambda d: d["order_dialog"]["points"].update(sell=[1, "x"]), "points.sell")
        self.assert_invalid(lambda d: d["order_dialog"]["points"].update(sell=[1]), "points.sell")
        self.assert_invalid(lambda d: d["order_dialog"].update(w=0), "order_dialog.w")
        self.assert_invalid(lambda d: d["main_window"].update(h=True), "main_window.h")
        self.assert_invalid(lambda d: d.update(version=2), "version")
        self.assert_invalid(lambda d: d.update(focus_point=[1, 2, 3]), "focus_point")
        self.assert_invalid(lambda d: d.update(trade_tab_point="here"), "trade_tab_point")

    def test_geometry_checks(self):
        self.assert_invalid(lambda d: d["order_dialog"]["points"].update(buy=[700, 400]), "outside")
        self.assert_invalid(lambda d: d["order_dialog"]["points"].update(buy=[150, 401]), "same spot")
        self.assert_invalid(lambda d: d.update(toolbox_region=[10, 620, 1500, 260]), "outside")
        self.assert_invalid(lambda d: d.update(toolbox_region=[10, 620, 0, 260]), "toolbox_region")
        self.assert_invalid(lambda d: d.update(focus_point=[-50, 10]), "focus_point")

    def test_top_level_not_object(self):
        self.write("[1, 2]")
        with self.assertRaises(CalibrationError):
            load_calibration(self.path)

    def test_save_refuses_invalid(self):
        with self.assertRaises(CalibrationError):
            save_calibration(self.path, Calibration())
        self.assertFalse(self.path.exists())


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = config_from_dict({"server": {"secret": SECRET}}, self.home)
        self.path = self.home / "calibration.json"
        self.prompts = []
        self.output = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_driver(self, mouse=None, **kw):
        ocr = kw.pop("ocr", {MAIN.wid: main_items(), DIALOG.wid: dialog_items()})
        return FakeDriver([OTHER_APP, MAIN], happy_mouse() if mouse is None else mouse, ocr=ocr, **kw)

    def input_for(self, driver, trade_tab="y", dialog_opens=True, dialog_closes=True, answers=None):
        answers = list(answers or [])

        def input_fn(prompt):
            self.prompts.append(prompt)
            if answers:
                return answers.pop(0)
            if "F9" in prompt and dialog_opens and DIALOG not in driver.windows:
                driver.windows.append(DIALOG)
            if "close the New Order window" in prompt and dialog_closes and DIALOG in driver.windows:
                driver.windows.remove(DIALOG)
            if prompt.startswith("Optional"):
                return trade_tab
            return ""
        return input_fn

    def run_wizard(self, driver, **kw):
        return run_wizard(driver, self.cfg, self.path, input_fn=self.input_for(driver, **kw),
                          print_fn=lambda *a: self.output.append(" ".join(str(x) for x in a)))

    def test_happy_path(self):
        driver = self.make_driver()
        calib = self.run_wizard(driver)

        self.assertEqual(calib.main_window, {"title": MAIN.title, "w": MAIN.w, "h": MAIN.h})
        self.assertEqual(calib.order_dialog["title"], "Order")
        self.assertEqual((calib.order_dialog["w"], calib.order_dialog["h"]), (620.0, 460.0))
        for name in POINT_NAMES:
            self.assertEqual(calib.dialog_point(name), DLG_POINTS[name], name)
        self.assertEqual(calib.focus_point, list(FOCUS))
        self.assertEqual(calib.toolbox_region, [10.0, 620.0, 1380.0, 260.0])
        self.assertEqual(calib.trade_tab_point, list(TRADE_TAB))
        self.assertTrue(calib.created_at.endswith("Z"))

        # saved, mode 600, identical on reload
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(load_calibration(self.path), calib)

        out = "\n".join(self.output)
        self.assertIn("DEMO", out)
        self.assertIn("do NOT click", " ".join(self.prompts) + out)
        self.assertIn("Balance 50000.00, Equity 49812.35", out)
        self.assertIn("Open positions seen: 1", out)
        self.assertIn("Trade tab label was found", out)
        self.assertEqual(driver.mouse, [])                      # every scripted hover consumed
        self.assertTrue(all(q == self.cfg.executor.gui.owner_names for q in driver.owner_queries))
        self.assertTrue(all(s in (1.0, 0.25) for s in driver.sleeps))
        # screenshots go under shots/calibration
        self.assertTrue(all(str(self.cfg.shots_dir / "calibration") in p for _, p in driver.captures))
        self.assertIn(DIALOG.wid, [wid for wid, _ in driver.captures])

    def test_swapped_field_points_fail_label_check(self):
        mouse = happy_mouse()
        names = list(POINT_NAMES)
        iv, isl = names.index("volume"), names.index("sl")
        mouse[iv], mouse[isl] = mouse[isl], mouse[iv]
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(self.make_driver(mouse=mouse))
        self.assertIn("swapped", str(cm.exception))
        self.assertFalse(self.path.exists())

    def test_field_labels_must_be_readable(self):
        driver = self.make_driver(ocr={MAIN.wid: main_items(), DIALOG.wid: dialog_items(labels=False)})
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver)
        self.assertIn("label", str(cm.exception))

    def test_label_and_value_in_one_observation(self):
        items = [i for i in dialog_items() if i.text not in ("Stop Loss:", "Take Profit:", "0.00000")]
        x0, y0 = glob(DIALOG, (20, 140))
        items.append(OcrItem(text="Stop Loss: 0.00000 Take Profit: 0.00000", conf=0.9, x=x0, y=y0 - 7,
                             w=460.0, h=14.0))
        driver = self.make_driver(ocr={MAIN.wid: main_items(), DIALOG.wid: items})
        self.run_wizard(driver)
        self.assertTrue(self.path.exists())

    def test_two_candidate_main_windows_are_refused(self):
        twin = Window(wid=20, pid=900, owner="wine64-preloader", title="MetaEditor", x=0.0, y=25.0,
                      w=1500.0, h=950.0)
        driver = FakeDriver([OTHER_APP, MAIN, twin], happy_mouse(),
                            ocr={MAIN.wid: main_items(), DIALOG.wid: dialog_items()})
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver)
        self.assertIn("several windows", str(cm.exception))
        # the configured login picks the right one
        self.cfg = config_from_dict({"server": {"secret": SECRET}, "account": {"account_login": "12345678"}},
                                    self.home)
        driver = FakeDriver([OTHER_APP, MAIN, twin], happy_mouse(),
                            ocr={MAIN.wid: main_items(), DIALOG.wid: dialog_items()})
        calib = self.run_wizard(driver)
        self.assertEqual(calib.main_window["title"], MAIN.title)

    def test_trade_tab_skipped(self):
        mouse = happy_mouse()[:-1]
        calib = self.run_wizard(self.make_driver(mouse=mouse), trade_tab="")
        self.assertIsNone(calib.trade_tab_point)

    def test_point_outside_dialog_is_asked_again(self):
        mouse = happy_mouse()
        mouse.insert(0, (5.0, 5.0))           # first "symbol" hover is outside the order window
        calib = self.run_wizard(self.make_driver(mouse=mouse))
        self.assertEqual(calib.dialog_point("symbol"), DLG_POINTS["symbol"])
        self.assertTrue(any("outside the New Order window" in o for o in self.output))

    def test_focus_point_must_be_in_title_bar(self):
        mouse = happy_mouse()
        idx = len(POINT_NAMES)
        mouse.insert(idx, glob(MAIN, (600.0, 300.0)))   # over a chart, not the title bar
        calib = self.run_wizard(self.make_driver(mouse=mouse))
        self.assertEqual(calib.focus_point, list(FOCUS))
        self.assertTrue(any("title bar" in o for o in self.output))

    def test_swapped_buttons_fail_ocr_check(self):
        driver = self.make_driver(ocr={MAIN.wid: main_items(), DIALOG.wid: dialog_items(swap=True)})
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver)
        self.assertIn("button", str(cm.exception))
        self.assertFalse(self.path.exists())

    def test_unreadable_toolbox_still_saves_with_clear_failure(self):
        driver = self.make_driver(ocr={MAIN.wid: main_items(readable=False), DIALOG.wid: dialog_items()})
        self.run_wizard(driver)
        self.assertTrue(self.path.exists())
        self.assertTrue(any(o.startswith("FAILED: could not read 'Balance:'") for o in self.output))

    def test_requires_screen_recording(self):
        driver = self.make_driver(perms={"accessibility": True, "screen_recording": False})
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver)
        self.assertIn("Screen Recording", str(cm.exception))
        self.assertFalse(self.path.exists())

    def test_missing_accessibility_only_warns(self):
        driver = self.make_driver(perms={"accessibility": False, "screen_recording": True})
        self.run_wizard(driver)
        self.assertTrue(any("Accessibility permission is missing" in o for o in self.output))

    def test_no_mt5_window(self):
        driver = FakeDriver([OTHER_APP], [])
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver)
        self.assertIn("no MetaTrader 5 window", str(cm.exception))

    def test_main_window_too_small(self):
        small = Window(wid=10, pid=500, owner="MetaTrader 5", title="MT5", x=0, y=0, w=500, h=300)
        with self.assertRaises(CalibrationError):
            self.run_wizard(FakeDriver([small], []))

    def test_tiny_helper_window_is_not_the_dialog(self):
        driver = self.make_driver()

        def dialog_appears_late():
            if DIALOG not in driver.windows:
                driver.windows.append(DIALOG)
            driver.on_sleep = None

        def input_fn(prompt):
            self.prompts.append(prompt)
            if "F9" in prompt:
                driver.windows.append(HELPER)          # appears first ...
                driver.on_sleep = dialog_appears_late   # ... the real dialog on the next poll
            if "close the New Order window" in prompt:
                driver.windows.remove(DIALOG)
            return "y" if prompt.startswith("Optional") else ""

        calib = run_wizard(driver, self.cfg, self.path, input_fn=input_fn, print_fn=self.output.append)
        self.assertEqual((calib.order_dialog["w"], calib.order_dialog["h"]), (DIALOG.w, DIALOG.h))
        self.assertFalse(any("other windows open" in o for o in self.output))

    def test_dialog_never_opens(self):
        driver = self.make_driver()
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver, dialog_opens=False)
        self.assertIn("New Order window was not detected", str(cm.exception))
        self.assertEqual(sum("F9" in p for p in self.prompts), 3)

    def test_dialog_never_closed(self):
        driver = self.make_driver()
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver, dialog_closes=False)
        self.assertIn("still open", str(cm.exception))
        self.assertFalse(self.path.exists())

    def test_abort_with_q(self):
        driver = self.make_driver()
        with self.assertRaises(CalibrationError) as cm:
            self.run_wizard(driver, answers=["q"])
        self.assertIn("aborted", str(cm.exception))

    def test_eof_aborts(self):
        driver = self.make_driver()

        def eof(_prompt):
            raise EOFError

        with self.assertRaises(CalibrationError):
            run_wizard(driver, self.cfg, self.path, input_fn=eof, print_fn=lambda *a: None)


if __name__ == "__main__":
    unittest.main()
