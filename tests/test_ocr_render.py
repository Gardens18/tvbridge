"""End-to-end OCR tests on synthetic MT5-like images rendered offscreen with AppKit.

These run Apple Vision for real (no screen access, no permissions needed): render an image,
OCR it through ``MacDriver.ocr`` with a known window position and scale, and check both the
parsed values and that item coordinates land where the text was drawn (global points).
Skipped cleanly where pyobjc / Vision is unavailable.
"""

import os
import tempfile
import unittest

try:  # pyobjc is optional for the rest of the test suite
    import AppKit
    import Quartz  # noqa: F401
    import Vision  # noqa: F401
    HAVE_VISION = True
except Exception:  # pragma: no cover - depends on the host
    HAVE_VISION = False

from tvbridge.gui import parse
from tvbridge.gui.driver import MacDriver, Window

KNOWN = ["EURUSD.h", "GBPUSD.h", "AUDUSD.h", "NZDUSD.h", "USDJPY.h", "USDCAD.h", "USDCHF.h", "XAUUSD.h"]
TOL = 4.0        # points, for centres: Vision boxes are a little looser than the glyphs
EDGE_TOL = 8.0   # points, for left/right box edges: Vision pads long lines horizontally

TOOLBOX_W, TOOLBOX_H = 800.0, 120.0
COLS = [10, 90, 170, 300, 345, 400, 470, 540, 610, 690]
TOOLBOX_ROWS = [
    ["Symbol", "Ticket", "Time", "Type", "Volume", "Price", "S / L", "T / P", "Price", "Profit"],
    ["EURUSD.h", "52390671", "2026.10.01 10:15:02", "buy", "0.50", "1.08345", "1.08100", "1.08900", "1.08311", "-17.00"],
    ["XAUUSD.h", "52390690", "2026.10.01 11:02:40", "sell", "0.10", "2345.67", "2360.00", "2320.00", "2341.20", "44.70"],
    ["USDJPY.h", "52390702", "2026.10.01 11:30:00", "buy", "1.00", "149.123", "148.500", "0.000", "149.200", "51.60"],
]
ACCOUNT_TEXT = ("Balance: 50 000.00 USD   Equity: 49 812.35   Margin: 1 083.45   Free Margin: 48 728.90   "
                "Margin Level: 4 597.60 %")
ACCOUNT_Y = 90.0

DIALOG_W, DIALOG_H = 620.0, 460.0
SELL_BUTTON = (40.0, 380.0, 240.0, 40.0)   # x, y, w, h (window-relative, top-left origin)
BUY_BUTTON = (340.0, 380.0, 240.0, 40.0)


class Canvas(object):
    """Offscreen AppKit bitmap of ``w`` x ``h`` points at ``scale`` pixels per point.

    Coordinates passed in are top-left-origin points (like a window); every drawn text box is
    recorded in ``boxes[text] = (x, y, w, h)``.
    """

    def __init__(self, w, h, scale):
        self.w, self.h, self.scale = w, h, scale
        self.rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
            None, int(round(w * scale)), int(round(h * scale)), 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
        self.rep.setSize_((w, h))
        self.ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(self.rep)
        self.boxes = {}
        AppKit.NSGraphicsContext.saveGraphicsState()
        AppKit.NSGraphicsContext.setCurrentContext_(self.ctx)

    def fill(self, x, y, w, h, white=1.0):
        AppKit.NSColor.colorWithCalibratedWhite_alpha_(white, 1.0).set()
        AppKit.NSRectFill(((x, self.h - y - h), (w, h)))

    def text(self, s, x, y, size=11.0, color=None, center_in=None):
        font = AppKit.NSFont.fontWithName_size_("Tahoma", size) or AppKit.NSFont.systemFontOfSize_(size)
        attrs = {AppKit.NSFontAttributeName: font,
                 AppKit.NSForegroundColorAttributeName: color or AppKit.NSColor.blackColor()}
        ns = AppKit.NSString.stringWithString_(s)
        sz = ns.sizeWithAttributes_(attrs)
        if center_in is not None:
            bx, by, bw, bh = center_in
            x, y = bx + (bw - sz.width) / 2.0, by + (bh - sz.height) / 2.0
        ns.drawAtPoint_withAttributes_((x, self.h - y - sz.height), attrs)
        self.boxes[s] = (x, y, float(sz.width), float(sz.height))

    def button(self, rect, label, color):
        x, y, w, h = rect
        self.fill(x, y, w, h, white=0.85)
        AppKit.NSColor.grayColor().set()
        AppKit.NSFrameRect(((x, self.h - y - h), (w, h)))
        self.text(label, 0, 0, size=12.0, color=color, center_in=rect)

    def save(self, path):
        AppKit.NSGraphicsContext.restoreGraphicsState()
        data = self.rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        if not data.writeToFile_atomically_(path, True):
            raise RuntimeError("cannot write %s" % path)
        return self.boxes


def render_toolbox(path, scale):
    c = Canvas(TOOLBOX_W, TOOLBOX_H, scale)
    c.fill(0, 0, TOOLBOX_W, TOOLBOX_H)
    for r, values in enumerate(TOOLBOX_ROWS):
        for col, value in enumerate(values):
            c.text(value, COLS[col], 10 + 20 * r)
    c.text(ACCOUNT_TEXT, 10, ACCOUNT_Y)
    return c.save(path)


def render_dialog(path, scale):
    c = Canvas(DIALOG_W, DIALOG_H, scale)
    c.fill(0, 0, DIALOG_W, DIALOG_H, white=0.94)
    c.text("Symbol:", 20, 20)
    c.text("EURUSD.h, Euro vs US Dollar", 120, 20)
    c.text("Type:", 20, 60)
    c.text("Market Execution", 120, 60)
    c.text("Volume: 0.50", 20, 100)
    c.text("Stop Loss: 1.08100", 20, 140)
    c.text("Take Profit: 1.08900", 330, 140)
    c.text("Comment:", 20, 180)
    c.text("1.08311 / 1.08345", 240, 330)
    c.button(SELL_BUTTON, "Sell by Market", AppKit.NSColor.redColor())
    c.button(BUY_BUTTON, "Buy by Market", AppKit.NSColor.blueColor())
    return c.save(path)


def render_dialog_split(path, scale, volume="0.50", sl="1.08100", tp="1.08900", label_dy=0.0):
    """MT5-like order window where each label and its edit box are separate controls (and may
    sit a few points apart vertically)."""
    c = Canvas(DIALOG_W, DIALOG_H, scale)
    c.fill(0, 0, DIALOG_W, DIALOG_H, white=0.94)
    c.text("Symbol:", 20, 20)
    c.text("EURUSD.h, Euro vs US Dollar", 120, 20)
    c.text("Type:", 20, 60)
    c.text("Market Execution", 120, 60)
    for label, value, lx, vx, y in (("Volume:", volume, 20, 120, 100), ("Stop Loss:", sl, 20, 120, 140),
                                    ("Take Profit:", tp, 330, 420, 140)):
        c.text(label, lx, y + label_dy)
        c.fill(vx - 4, y - 3, 110, 20, white=1.0)
        c.text(value, vx, y)
    c.text("Comment:", 20, 180)
    c.text("1.08311 / 1.08345", 240, 330)
    c.button(SELL_BUTTON, "Sell by Market", AppKit.NSColor.redColor())
    c.button(BUY_BUTTON, "Buy by Market", AppKit.NSColor.blueColor())
    return c.save(path)


def solid_png(path, w, h, rgba):
    c = Canvas(w, h, 1.0)
    r, g, b, a = rgba
    AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).set()
    AppKit.NSRectFillUsingOperation(((0, 0), (w, h)), AppKit.NSCompositingOperationCopy)
    c.save(path)


def center(box):
    x, y, w, h = box
    return x + w / 2.0, y + h / 2.0


@unittest.skipUnless(HAVE_VISION, "pyobjc AppKit/Vision not available")
class OcrRenderTestBase(unittest.TestCase):
    # window placements: (scale, x, y) -- odd offsets so a missing offset cannot go unnoticed
    PLACEMENTS = ((1.0, 40.0, 600.0), (2.0, 1234.5, 77.0))

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = cls._tmp.name
        cls.driver = MacDriver()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def assertNear(self, got, want, msg=None, tol=TOL):
        self.assertLessEqual(abs(got - want), tol, "%s: got %.2f, want %.2f (+-%.1f)" % (msg, got, want, tol))

    def find(self, items, text):
        hits = [i for i in items if text in i.text]
        self.assertTrue(hits, "%r not found in OCR output %r" % (text, [i.text for i in items]))
        return min(hits, key=lambda i: len(i.text))


class ToolboxOcrTests(OcrRenderTestBase):
    @classmethod
    def setUpClass(cls):
        super(ToolboxOcrTests, cls).setUpClass()
        cls.cases = []
        for scale, wx, wy in cls.PLACEMENTS:
            path = os.path.join(cls.dir, "toolbox_%gx.png" % scale)
            boxes = render_toolbox(path, scale)
            win = Window(wid=1, pid=1, owner="MetaTrader 5", title="test", x=wx, y=wy, w=TOOLBOX_W, h=TOOLBOX_H)
            items = cls.driver.ocr(path, win, scale)
            cls.cases.append((scale, path, win, boxes, items))

    def test_account_line(self):
        for scale, _path, _win, _boxes, items in self.cases:
            with self.subTest(scale=scale):
                acc = parse.parse_account_line(items)
                self.assertEqual(acc, {"balance": 50000.0, "equity": 49812.35, "margin": 1083.45,
                                       "free_margin": 48728.90})

    def test_positions(self):
        for scale, _path, win, boxes, items in self.cases:
            with self.subTest(scale=scale):
                res = parse.parse_position_rows(items, KNOWN)
                self.assertEqual([(p.symbol, p.side, p.lots) for p, _ in res],
                                 [("EURUSD.h", "buy", 0.5), ("XAUUSD.h", "sell", 0.1), ("USDJPY.h", "buy", 1.0)])
                eur, xau, jpy = [p for p, _ in res]
                self.assertEqual(eur.ticket, "52390671")
                self.assertAlmostEqual(eur.open_price, 1.08345)
                self.assertAlmostEqual(eur.sl, 1.081)
                self.assertAlmostEqual(eur.tp, 1.089)
                self.assertAlmostEqual(eur.profit, -17.0)
                self.assertAlmostEqual(xau.open_price, 2345.67)
                self.assertAlmostEqual(xau.sl, 2360.0)
                self.assertAlmostEqual(xau.tp, 2320.0)
                self.assertAlmostEqual(xau.profit, 44.7)
                self.assertAlmostEqual(jpy.sl, 148.5)
                self.assertIsNone(jpy.tp)           # shown as 0.000 = not set
                # anchors sit on their rows, at the symbol column
                for (pos, anchor), row in zip(res, (1, 2, 3)):
                    sx, sy, sw, sh = boxes[pos.symbol]
                    self.assertNear(anchor.cy, win.y + sy + sh / 2, "%s anchor cy" % pos.symbol)
                    self.assertNear(anchor.x, win.x + sx, "%s anchor x" % pos.symbol, tol=EDGE_TOL)

    def test_header_and_account_line_found(self):
        for scale, _path, _win, _boxes, items in self.cases:
            with self.subTest(scale=scale):
                hdr = parse.find_trade_header(items)
                self.assertIsNotNone(hdr, [i.text for i in items])
                self.assertFalse(hdr["swap"])
                ay = parse.account_line_y(items)
                rows = parse.parse_position_rows(items, KNOWN)
                self.assertLess(hdr["cy"], min(a.cy for _, a in rows))
                self.assertGreater(ay, max(a.cy for _, a in rows))

    def test_item_coordinates_are_global(self):
        for scale, _path, win, boxes, items in self.cases:
            with self.subTest(scale=scale):
                # first cell of a row: left edge and vertical centre (holds even if Vision merges the row)
                for text in ("EURUSD.h", "XAUUSD.h", "Symbol"):
                    bx, by, bw, bh = boxes[text]
                    it = self.find(items, text)
                    self.assertNear(it.x, win.x + bx, "%s x" % text, tol=EDGE_TOL)
                    self.assertNear(it.cy, win.y + by + bh / 2, "%s cy" % text)
                # last cell of a row: right edge
                bx, by, bw, bh = boxes["-17.00"]
                it = self.find(items, "-17.00")
                self.assertNear(it.x + it.w, win.x + bx + bw, "-17.00 right edge", tol=EDGE_TOL)
                self.assertNear(it.cy, win.y + by + bh / 2, "-17.00 cy")
                # account line
                bx, by, bw, bh = boxes[ACCOUNT_TEXT]
                it = self.find(items, "Balance")
                self.assertNear(it.x, win.x + bx, "Balance x", tol=EDGE_TOL)
                self.assertNear(it.cy, win.y + by + bh / 2, "Balance cy")

    def test_region_is_cropped_and_offset_back(self):
        for scale, path, win, boxes, _items in self.cases:
            with self.subTest(scale=scale):
                # only the account line
                items = self.driver.ocr(path, win, scale, region=(0.0, ACCOUNT_Y - 5, TOOLBOX_W, 25.0))
                self.assertTrue(items)
                self.assertTrue(all("Balance" in i.text or "Margin" in i.text or "%" in i.text for i in items),
                                [i.text for i in items])
                self.assertEqual(parse.parse_position_rows(items, KNOWN), [])
                self.assertEqual(parse.parse_account_line(items)["equity"], 49812.35)
                bx, by, bw, bh = boxes[ACCOUNT_TEXT]
                it = self.find(items, "Balance")
                self.assertNear(it.x, win.x + bx, "Balance x (region)", tol=EDGE_TOL)
                self.assertNear(it.cy, win.y + by + bh / 2, "Balance cy (region)")

                # right half of the EURUSD row: results must still be in global points
                items = self.driver.ocr(path, win, scale, region=(330.0, 25.0, 470.0, 20.0))
                texts = " ".join(i.text for i in items)
                self.assertIn("-17.00", texts)
                self.assertNotIn("EURUSD", texts)
                for text in ("0.50", "-17.00"):
                    bx, by, bw, bh = boxes[text]
                    it = self.find(items, text)
                    self.assertNear(it.x + it.w, win.x + bx + bw, "%s right edge (region)" % text, tol=EDGE_TOL)
                    self.assertNear(it.cy, win.y + by + bh / 2, "%s cy (region)" % text)

    def test_region_outside_image_is_empty(self):
        scale, path, win, _boxes, _items = self.cases[0]
        self.assertEqual(self.driver.ocr(path, win, scale, region=(TOOLBOX_W + 10, 0.0, 50.0, 50.0)), [])

    def test_recognize_returns_top_left_pixels(self):
        from tvbridge.gui import ocr

        scale, path, _win, boxes, _items = self.cases[1]   # 2x image
        results = ocr.recognize(path)
        bx, by, bw, bh = boxes["EURUSD.h"]
        text, conf, (x, y, w, h) = min((r for r in results if "EURUSD.h" in r[0]), key=lambda r: len(r[0]))
        self.assertGreater(conf, 0.0)
        self.assertNear(x, bx * scale, "pixel x", tol=EDGE_TOL * scale)
        self.assertNear(y + h / 2, (by + bh / 2) * scale, "pixel cy", tol=TOL * scale)
        # with a crop, coordinates are relative to the crop
        crop = (330 * scale, 25 * scale, 470 * scale, 20 * scale)
        results = ocr.recognize(path, crop)
        bx, by, bw, bh = boxes["-17.00"]
        text, conf, (x, y, w, h) = min((r for r in results if "-17.00" in r[0]), key=lambda r: len(r[0]))
        self.assertNear(x + w, (bx + bw - 330) * scale, "cropped pixel right edge", tol=EDGE_TOL * scale)
        self.assertNear(y + h / 2, (by + bh / 2 - 25) * scale, "cropped pixel cy", tol=TOL * scale)


class DialogOcrTests(OcrRenderTestBase):
    @classmethod
    def setUpClass(cls):
        super(DialogOcrTests, cls).setUpClass()
        cls.cases = []
        for scale, wx, wy in cls.PLACEMENTS:
            path = os.path.join(cls.dir, "dialog_%gx.png" % scale)
            boxes = render_dialog(path, scale)
            win = Window(wid=2, pid=1, owner="MetaTrader 5", title="Order", x=wx + 300, y=wy - 50,
                         w=DIALOG_W, h=DIALOG_H)
            items = cls.driver.ocr(path, win, scale)
            cls.cases.append((scale, path, win, boxes, items))

    def test_verify_dialog_fields(self):
        for scale, _path, _win, _boxes, items in self.cases:
            with self.subTest(scale=scale):
                ok, problems = parse.verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", ["Market"])
                self.assertTrue(ok, problems)
                ok, problems = parse.verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08200", "1.08900", ["Market"])
                self.assertFalse(ok)
                ok, problems = parse.verify_dialog_fields(items, "GBPUSD.h", "0.50", "1.08100", "1.08900", ["Market"])
                self.assertFalse(ok)

    def test_button_guard(self):
        for scale, _path, win, _boxes, items in self.cases:
            with self.subTest(scale=scale):
                sx, sy = center(SELL_BUTTON)
                bx, by = center(BUY_BUTTON)
                self.assertEqual(parse.nearest_label(items, (win.x + sx, win.y + sy), ["buy", "sell"], 80), "sell")
                self.assertEqual(parse.nearest_label(items, (win.x + bx, win.y + by), ["buy", "sell"], 80), "buy")
                self.assertIsNone(parse.nearest_label(items, (win.x + 310, win.y + 60), ["buy", "sell"], 80))

    def test_label_coordinates_are_global(self):
        for scale, _path, win, boxes, items in self.cases:
            with self.subTest(scale=scale):
                for text in ("Buy by Market", "Sell by Market", "Market Execution"):
                    cx, cy = center(boxes[text])
                    it = self.find(items, text)
                    self.assertNear(it.cx, win.x + cx, "%s cx" % text)
                    self.assertNear(it.cy, win.y + cy, "%s cy" % text)

    def split_items(self, scale, **kw):
        path = os.path.join(self.dir, "split_%gx_%s.png" % (scale, abs(hash(tuple(sorted(kw.items()))))))
        render_dialog_split(path, scale, **kw)
        win = Window(wid=3, pid=1, owner="MetaTrader 5", title="Order", x=500.0, y=120.0, w=DIALOG_W, h=DIALOG_H)
        return self.driver.ocr(path, win, scale)

    def test_split_labels_pair_with_their_own_values(self):
        for scale in (1.0, 2.0):
            for dy in (0.0, -6.0, 6.0):
                with self.subTest(scale=scale, label_dy=dy):
                    items = self.split_items(scale, label_dy=dy)
                    ok, problems = parse.verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900",
                                                              ["Market"])
                    self.assertTrue(ok, (problems, [(i.text, round(i.cy, 1)) for i in items]))

    def test_split_labels_catch_swapped_fields(self):
        for scale in (1.0, 2.0):
            with self.subTest(scale=scale):
                items = self.split_items(scale, volume="1.08100", sl="0.50", label_dy=-6.0)
                ok, problems = parse.verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900",
                                                          ["Market"])
                self.assertFalse(ok)
                self.assertTrue(any(p.startswith("Volume shows") for p in problems), problems)
                items = self.split_items(scale, sl="1.08900", tp="1.08100")
                ok, problems = parse.verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900",
                                                          ["Market"])
                self.assertFalse(ok)
                self.assertTrue(any(p.startswith("Stop Loss shows") for p in problems), problems)

    def test_unclicked_dialog_is_not_a_result(self):
        for scale, _path, _win, _boxes, items in self.cases:
            with self.subTest(scale=scale):
                status, _msg, _ticket, _price = parse.parse_order_result(" ".join(i.text for i in items))
                self.assertEqual(status, "unknown")


class BlankCaptureTests(OcrRenderTestBase):
    """The pixel check ``MacDriver.capture`` uses to detect a missing Screen Recording grant."""

    def test_blank_images_detected(self):
        for name, rgba in (("black", (0, 0, 0, 1)), ("transparent", (0, 0, 0, 0)), ("white", (1, 1, 1, 1))):
            path = os.path.join(self.dir, "%s.png" % name)
            solid_png(path, 300, 200, rgba)
            width, blank = MacDriver._inspect_png(path)
            self.assertEqual(width, 300, name)
            self.assertTrue(blank, name)

    def test_real_content_not_blank(self):
        path = os.path.join(self.dir, "content.png")
        render_dialog(path, 2.0)
        width, blank = MacDriver._inspect_png(path)
        self.assertEqual(width, int(DIALOG_W * 2))
        self.assertFalse(blank)


if __name__ == "__main__":
    unittest.main()
