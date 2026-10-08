"""Test doubles for the GUI layer.

:class:`FakeMt5Driver` implements :class:`tvbridge.gui.driver.Driver` and simulates the parts
of MetaTrader 5 (Wine build) that the GUI executor touches:

* a main window titled like ``"12345678 - HantecMarketsMU-MT5: Demo Account - Hedge"`` with a
  Toolbox (Trade tab: position rows + the "Balance: ... Equity: ..." line, or the Journal tab);
* F9 opens an "Order" dialog after a short delay; clicking a field focuses it, End then
  Shift+Home selects its text, typing replaces the selection, Tab commits;
* OCR of the dialog returns the field values at their calibrated positions, "Market
  Execution", the market-execution warning text, "Sell by Market" / "Buy by Market" at the
  sell / buy points, and after a Buy/Sell click a result page (configurable outcome);
* double-clicking a position row opens a "Position #ticket" dialog whose button reads
  "Close #ticket buy 0.50 EURUSD.h by Market"; clicking it closes the position; the dialog
  has a volume field right of its "Volume:" label: typing a smaller volume there changes the
  button text and makes the click a partial close (the position keeps the rest);
* the order dialog shows a bid / ask quote line (``order_quote``);
* Escape closes dialogs (unless a dialog is configured to be stuck).

Every action is recorded in :attr:`FakeMt5Driver.actions`; Buy/Sell and Close button hits are
also recorded in :attr:`order_button_clicks` / :attr:`close_button_clicks` so tests can assert
that a button was never pressed. :meth:`sleep` advances a fake clock and never really sleeps.

Faults are constructor options (see ``__init__``).
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from tvbridge.gui.calibration import Calibration
from tvbridge.gui.driver import Driver, OcrItem, Window

MT5_PID = 4242
OWNER = "wine64-preloader"
MAIN_WID = 100
MAIN_TITLE = "12345678 - HantecMarketsMU-MT5: Demo Account - Hedge"
MAIN_RECT = (0.0, 25.0, 1440.0, 875.0)                 # x, y, w, h (screen points)

ORDER_DIALOG_ORIGIN = (410.0, 180.0)
ORDER_DIALOG_SIZE = (620.0, 470.0)
ORDER_POINTS = {                                        # relative to the order dialog origin
    "symbol": [310.0, 60.0],
    "volume": [150.0, 130.0],
    "sl": [150.0, 170.0],
    "tp": [460.0, 170.0],
    "sell": [170.0, 370.0],
    "buy": [450.0, 370.0],
}
POSITION_DIALOG_ORIGIN = (420.0, 190.0)
POSITION_DIALOG_SIZE = (620.0, 470.0)
CLOSE_BUTTON_POINT = (310.0, 380.0)                     # relative to the position dialog origin
POSITION_VOLUME_POINT = (150.0, 130.0)                  # volume field of the position dialog
DEFAULT_ORDER_QUOTE = "1.08340 / 1.08345"
SYMBOLS_WINDOW_RECT = (178.0, 186.0, 705.0, 476.0)      # View > Symbols window (x, y, w, h)
SYMBOLS_SEARCH_POINT = (350.0, 78.0)                     # relative: search field (as the executor clicks it)
SYMBOLS_ROW_POINT = (430.0, 313.0)                       # relative: the single matching row
SYMBOLS_SHOW_POINT = (81.0, 456.0)                       # relative: "Show Symbol" button

TOOLBOX_REGION = [0.0, 600.0, 1440.0, 250.0]            # relative to the main window
FOCUS_POINT = [720.0, 12.0]
TRADE_TAB_POINT = [40.0, 862.0]
ROW_TOP = 635.0                                         # first position row (relative to main)
ROW_STEP = 18.0

FIELD_HALF = (70.0, 11.0)        # half width/height of an input field's clickable area
BUTTON_HALF = (110.0, 20.0)      # half width/height of the Buy/Sell buttons
CLOSE_HALF = (220.0, 20.0)

DESCRIPTIONS = {
    "EURUSD.h": "Euro vs US Dollar",
    "GBPUSD.h": "Great Britain Pound vs US Dollar",
    "USDJPY.h": "US Dollar vs Japanese Yen",
    "XAGUSD.h": "Silver vs US Dollar",
}

WARNING_TEXT = ("Attention! The trade will be executed at market conditions, "
                "difference with requested price may be significant!")


def make_calibration(**overrides: Any) -> Calibration:
    """A Calibration matching the fake's geometry."""
    kw = dict(
        version=1,
        created_at="2026-10-01T09:00:00Z",
        main_window={"title": MAIN_TITLE, "w": MAIN_RECT[2], "h": MAIN_RECT[3]},
        order_dialog={"title": "Order", "w": ORDER_DIALOG_SIZE[0], "h": ORDER_DIALOG_SIZE[1],
                      "points": {k: list(v) for k, v in ORDER_POINTS.items()}},
        toolbox_region=list(TOOLBOX_REGION),
        focus_point=list(FOCUS_POINT),
        trade_tab_point=list(TRADE_TAB_POINT),
    )
    kw.update(overrides)
    return Calibration(**kw)


def fmt_money(v: float) -> str:
    """MT5 style: space thousands separator, two decimals ("50 000.00", "-17.00")."""
    s = "{:,.2f}".format(abs(v)).replace(",", " ")
    return ("-" if v < 0 else "") + s


class FakeDialog(object):
    """State of one simulated MT5 dialog window."""

    def __init__(self, wid: int, kind: str, title: str, x: float, y: float, w: float, h: float):
        self.wid = wid
        self.kind = kind            # "order" | "position" | "stray"
        self.title = title
        self.x, self.y, self.w, self.h = x, y, w, h
        self.page = "form"          # "form" | "result"
        self.result_text = ""
        self.fields = {}            # type: Dict[str, str]
        self.focused = None         # type: Optional[str]
        self.selected = False
        self.stuck = False
        self.ticket = None          # type: Optional[str]  (position dialog)
        self.pending = False        # a Buy/Sell/Close click is being processed

    def window(self) -> Window:
        return Window(wid=self.wid, pid=MT5_PID, owner=OWNER, title=self.title,
                      x=self.x, y=self.y, w=self.w, h=self.h)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


def _hit(px: float, py: float, cx: float, cy: float, half: Tuple[float, float]) -> bool:
    return abs(px - cx) <= half[0] and abs(py - cy) <= half[1]


class FakeMt5Driver(Driver):
    """Simulated MT5 + macOS for executor tests. See the module docstring."""

    def __init__(
        self,
        main_title: str = MAIN_TITLE,
        positions: Optional[List[Dict[str, Any]]] = None,
        balance: float = 50000.0,
        equity: Optional[float] = None,
        mt5_running: bool = True,
        # order dialog
        dialog_opens: bool = True,
        dialog_size: Tuple[float, float] = ORDER_DIALOG_SIZE,
        dialog_delay_s: float = 0.2,
        initial_symbol: str = "EURUSD.h",
        volume_ignores_typing: bool = False,
        swap_buttons: bool = False,
        # what happens after Buy/Sell: "filled" | "rejected" | "unknown" | "nothing" | "vanish"
        #                              | "done_no_ticket" | "wrong_side"
        order_outcome: str = "filled",
        result_delay_s: float = 1.0,
        fill_price: float = 1.08345,
        first_ticket: int = 52390671,
        reject_text: str = "Invalid stops",
        unknown_text: str = "Request sent, waiting for the trade server",
        # "click": click() raises right after the click took effect; "capture": later captures raise
        raise_after_click: Optional[str] = None,
        # stray dialog already open: None | "closable" | "stuck"
        stray_dialog: Optional[str] = None,
        # a stuck stray that does not block clicks on the main window (Navigator, Alert list ...)
        stray_modal: bool = True,
        stray_rect: Optional[Tuple[float, float, float, float]] = None,
        # Toolbox: "trade" (positions + account line) or "journal" (no account line)
        toolbox_tab: str = "trade",
        trade_tab_works: bool = True,
        # position dialog / close: "filled" | "rejected" | "vanish" | "vanish_keep" | "done_keep"
        close_outcome: str = "filled",
        close_price: float = 1.08400,
        close_reject_text: str = "Market is closed",
        close_button_text: Optional[str] = None,   # override, e.g. unreadable text
        permissions: Optional[Dict[str, bool]] = None,
        # main window size (the Toolbox list moves up when the window is shorter)
        main_size: Optional[Tuple[float, float]] = None,
        # extra pt the Trade list content sits higher (taller Toolbox: splitter dragged up)
        toolbox_shift: float = 0.0,
        # tickets whose Toolbox row OCR cannot read (the row is there, its text is lost)
        hidden_tickets: Optional[List[str]] = None,
        # other windows on screen (MetaEditor, MT4, a second terminal ...)
        extra_windows: Optional[List[Window]] = None,
        # text shown after Buy/Sell for order_outcome "custom"
        custom_result_text: str = "",
        # every capture fails (like a refused screen recording) and leaves a file behind
        capture_fails: bool = False,
        # the order dialog's bid / ask line; "" = not shown (unreadable quote)
        order_quote: str = DEFAULT_ORDER_QUOTE,
        # the position dialog's volume field ignores typing (the button keeps the full volume)
        position_volume_ignores_typing: bool = False,
        # symbols the order dialog accepts (MT5's Market Watch); None = every symbol. Others are
        # silently refused: the dialog keeps its previous symbol, like real MT5
        market_watch: Optional[List[str]] = None,
        # symbols the (fake) broker offers in the Symbols window (Ctrl+U)
        server_symbols: Optional[List[str]] = None,
    ):
        self.main_title = main_title
        self.positions = [dict(p) for p in (positions or [])]
        self.balance = balance
        self.equity_override = equity
        self.mt5_running = mt5_running
        self.dialog_opens = dialog_opens
        self.dialog_size = dialog_size
        self.dialog_delay_s = dialog_delay_s
        self.current_symbol = initial_symbol
        self.volume_ignores_typing = volume_ignores_typing
        self.swap_buttons = swap_buttons
        self.order_outcome = order_outcome
        self.result_delay_s = result_delay_s
        self.fill_price = fill_price
        self.next_ticket = first_ticket
        self.reject_text = reject_text
        self.unknown_text = unknown_text
        self.raise_after_click = raise_after_click
        self.toolbox_tab = toolbox_tab
        self.trade_tab_works = trade_tab_works
        self.close_outcome = close_outcome
        self.close_price = close_price
        self.close_reject_text = close_reject_text
        self.close_button_text = close_button_text
        self.perms = dict(permissions if permissions is not None
                          else {"accessibility": True, "screen_recording": True})
        self.main_size = tuple(main_size) if main_size is not None else (MAIN_RECT[2], MAIN_RECT[3])
        self.toolbox_shift = float(toolbox_shift)
        self.hidden_tickets = set(str(t) for t in (hidden_tickets or []))
        self.extra_windows = list(extra_windows or [])
        self.custom_result_text = custom_result_text
        self.capture_fails = capture_fails
        self.stray_modal = stray_modal
        self.order_quote = order_quote
        self.position_volume_ignores_typing = position_volume_ignores_typing
        self.market_watch = list(market_watch) if market_watch is not None else None  # type: Optional[List[str]]
        self.server_symbols = list(server_symbols) if server_symbols is not None else list(DESCRIPTIONS)
        self.shown_symbols = []     # type: List[str]   symbols added via the Symbols window

        self.now = 0.0
        self._seq = 0
        self._scheduled = []        # type: List[Tuple[float, int, Callable[[], None]]]
        self._next_wid = 200
        self.dialogs = []           # type: List[FakeDialog]   (last = topmost)
        self.active_pid = None      # type: Optional[int]
        self._captures = {}         # type: Dict[str, List[OcrItem]]
        self._capture_broken = False
        self._mouse = (0.0, 0.0)

        # recordings
        self.actions = []           # type: List[Tuple[Any, ...]]
        self.order_button_clicks = []   # type: List[str]   actual side of each Buy/Sell button hit
        self.close_button_clicks = []   # type: List[str]   ticket of each Close button hit
        self.orders_sent = []       # type: List[Dict[str, Any]]
        self.closed_tickets = []    # type: List[str]
        self.partial_closes = []    # type: List[Tuple[str, float]]   (ticket, lots closed)
        self.lost_keys = []         # type: List[str]   keys sent while MT5 was not active
        self.dangerous_returns = 0  # Return pressed on an order/position form page

        if stray_dialog:
            sx, sy, sw, sh = stray_rect if stray_rect is not None else (500.0, 300.0, 320.0, 150.0)
            d = self._new_dialog("stray", "Alert", sx, sy, sw, sh)
            d.stuck = stray_dialog == "stuck"
            self.dialogs.append(d)

    # ------------------------------------------------------------------ fake time

    def _schedule(self, delay: float, fn: Callable[[], None]) -> None:
        self._seq += 1
        self._scheduled.append((self.now + delay, self._seq, fn))
        self._scheduled.sort(key=lambda t: (t[0], t[1]))
        self._tick()

    def _tick(self) -> None:
        while self._scheduled and self._scheduled[0][0] <= self.now + 1e-9:
            _, _, fn = self._scheduled.pop(0)
            fn()

    # ------------------------------------------------------------------ windows

    def _new_dialog(self, kind: str, title: str, x: float, y: float, w: float, h: float) -> FakeDialog:
        self._next_wid += 1
        return FakeDialog(self._next_wid, kind, title, x, y, w, h)

    def main_window(self) -> Window:
        x, y, _w, _h = MAIN_RECT
        w, h = self.main_size
        return Window(wid=MAIN_WID, pid=MT5_PID, owner=OWNER, title=self.main_title, x=x, y=y, w=w, h=h)

    def list_dy(self) -> float:
        """How far the Trade list content sits above its calibrated place."""
        return (MAIN_RECT[3] - self.main_size[1]) + self.toolbox_shift

    def tab_dy(self) -> float:
        return MAIN_RECT[3] - self.main_size[1]

    def row_y(self, i: int) -> float:
        """Global centre y of position row ``i``."""
        return MAIN_RECT[1] + ROW_TOP + ROW_STEP * i - self.list_dy()

    def _all_windows(self) -> List[Window]:
        wins = [
            # unrelated app, never an MT5 candidate
            Window(wid=1, pid=111, owner="Finder", title="Order history", x=0, y=0, w=1600, h=1000),
            # a second, small wine process window (e.g. a launcher) -> too small to be the main window
            Window(wid=2, pid=5151, owner="wine-preloader", title="MetaTrader 5", x=50, y=50, w=400, h=300),
        ]
        if self.mt5_running:
            wins.append(self.main_window())
            # invisible helper window of the MT5 process (Wine creates such windows)
            wins.append(Window(wid=101, pid=MT5_PID, owner=OWNER, title="", x=0, y=0, w=1, h=1))
            wins.extend(d.window() for d in self.dialogs)
        wins.extend(self.extra_windows)
        return wins

    def dialog_by_kind(self, kind: str) -> Optional[FakeDialog]:
        for d in self.dialogs:
            if d.kind == kind:
                return d
        return None

    def _top_dialog(self) -> Optional[FakeDialog]:
        return self.dialogs[-1] if self.dialogs else None

    def _remove_dialog(self, d: FakeDialog) -> None:
        if d in self.dialogs:
            self.dialogs.remove(d)

    # ------------------------------------------------------------------ Driver API

    def list_windows(self, owner_names: List[str]) -> List[Window]:
        self._tick()
        needles = [n.lower() for n in owner_names]
        return [w for w in self._all_windows() if any(n in w.owner.lower() for n in needles)]

    def activate(self, pid: int) -> None:
        self.actions.append(("activate", pid))
        self.active_pid = pid

    def capture(self, win: Window, out_path: str) -> float:
        self.actions.append(("capture", win.wid, out_path))
        self._tick()
        if self.capture_fails:
            with open(out_path, "wb") as fh:
                fh.write(b"\x89PNG blank\n")
            raise RuntimeError("simulated: screen capture is blank")
        if self._capture_broken:
            raise RuntimeError("simulated: screencapture failed")
        if win.wid == MAIN_WID and self.mt5_running:
            items = self._main_items()
        else:
            d = next((d for d in self.dialogs if d.wid == win.wid), None)
            if d is None:
                raise RuntimeError("simulated: could not create image from window %s" % win.wid)
            items = self._dialog_items(d)
        self._captures[out_path] = items
        with open(out_path, "wb") as fh:
            fh.write(b"\x89PNG fake capture\n")
        return 2.0

    def ocr(self, png_path: str, win: Window, scale: float,
            region: Optional[Tuple[float, float, float, float]] = None) -> List[OcrItem]:
        self.actions.append(("ocr", png_path, region))
        items = list(self._captures[png_path])
        if region is not None:
            dx, dy, w, h = region
            x0, y0 = win.x + dx, win.y + dy
            items = [i for i in items if x0 <= i.cx <= x0 + w and y0 <= i.cy <= y0 + h]
        return items

    def click(self, x: float, y: float, count: int = 1) -> None:
        self.actions.append(("click", x, y, count))
        self._mouse = (x, y)
        self._tick()
        if not self.mt5_running:
            return
        top = None
        for d in reversed(self.dialogs):
            if d.contains(x, y):
                top = d
                break
        if top is not None:
            self.active_pid = MT5_PID
            self._click_dialog(top, x, y, count)
            return
        mx, my = MAIN_RECT[0], MAIN_RECT[1]
        mw, mh = self.main_size
        if mx <= x <= mx + mw and my <= y <= my + mh:
            self.active_pid = MT5_PID
            if any(d.kind != "stray" or self.stray_modal for d in self.dialogs):
                self.actions.append(("blocked_by_modal", x, y))
                return
            self._click_main(x, y, count)

    def key(self, name: str, mods: Tuple[str, ...] = ()) -> None:
        self.actions.append(("key", name, tuple(mods)))
        self._tick()
        if self.active_pid != MT5_PID or not self.mt5_running:
            self.lost_keys.append(name)
            return
        top = self._top_dialog()
        if name == "f9":
            if top is None and self.dialog_opens:
                self._schedule(self.dialog_delay_s, self._open_order_dialog)
            return
        if name == "u" and "ctrl" in mods:
            if top is None:
                self._open_symbols_window()
            return
        if top is None:
            return
        if name == "escape":
            if not top.stuck:
                self._remove_dialog(top)
            return
        if top.kind == "stray":
            return
        if name == "return":
            if top.page == "result":
                self._remove_dialog(top)
            elif top.kind in ("order", "position"):
                self.dangerous_returns += 1
            return
        if top.kind not in ("order", "position", "symbols") or top.page != "form" or top.focused is None:
            return
        if name == "end":
            top.selected = False
        elif name == "home" and "shift" in mods:
            top.selected = True
        elif name == "tab":
            if top.focused == "symbol":
                typed = top.fields["symbol"]
                if self.market_watch is not None and typed not in self.market_watch:
                    top.fields["symbol"] = self.current_symbol      # refused: previous symbol kept
                else:
                    self.current_symbol = typed
                    top.title = self._order_title(typed)
            top.focused = None
            top.selected = False

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))
        self._tick()
        if self.active_pid != MT5_PID:
            self.lost_keys.append(text)
            return
        top = self._top_dialog()
        if top is None or top.kind not in ("order", "position", "symbols") or top.page != "form" \
                or top.focused is None:
            return
        name = top.focused
        if name == "volume" and top.kind == "order" and self.volume_ignores_typing:
            return
        if top.kind == "position" and self.position_volume_ignores_typing:
            return
        if top.selected:
            top.fields[name] = text
        else:
            top.fields[name] = top.fields.get(name, "") + text
        top.selected = False

    def mouse_location(self) -> Tuple[float, float]:
        return self._mouse

    def sleep(self, s: float) -> None:
        self.actions.append(("sleep", s))
        self.now += float(s)
        self._tick()

    def permissions(self) -> Dict[str, bool]:
        return dict(self.perms)

    def request_permissions(self) -> None:
        self.actions.append(("request_permissions",))

    # ------------------------------------------------------------------ recordings helpers

    def clicks(self) -> List[Tuple[float, float, int]]:
        return [(a[1], a[2], a[3]) for a in self.actions if a[0] == "click"]

    def keys(self) -> List[str]:
        return [a[1] for a in self.actions if a[0] == "key"]

    def typed(self) -> List[str]:
        return [a[1] for a in self.actions if a[0] == "type"]

    def captures(self) -> List[str]:
        return [a[2] for a in self.actions if a[0] == "capture"]

    def clicks_on_order_buttons(self) -> List[Tuple[float, float, int]]:
        """Clicks that landed on either Buy/Sell button area (of a dialog at the standard origin)."""
        ox, oy = ORDER_DIALOG_ORIGIN
        out = []
        for (x, y, n) in self.clicks():
            for side in ("buy", "sell"):
                px, py = ORDER_POINTS[side]
                if _hit(x, y, ox + px, oy + py, BUTTON_HALF):
                    out.append((x, y, n))
        return out

    # ------------------------------------------------------------------ MT5 behaviour

    def _order_title(self, symbol: str) -> str:
        return "Order: %s - %s" % (symbol, DESCRIPTIONS.get(symbol, symbol))

    def _open_order_dialog(self) -> None:
        w, h = self.dialog_size
        d = self._new_dialog("order", self._order_title(self.current_symbol),
                             ORDER_DIALOG_ORIGIN[0], ORDER_DIALOG_ORIGIN[1], w, h)
        d.fields = {"symbol": self.current_symbol, "volume": "0.01", "sl": "0.00000", "tp": "0.00000"}
        self.dialogs.append(d)
        self.active_pid = MT5_PID

    def _open_symbols_window(self) -> None:
        """View > Symbols (Ctrl+U): a search field, the matching row and a "Show Symbol" button."""
        x, y, w, h = SYMBOLS_WINDOW_RECT
        d = self._new_dialog("symbols", "Symbols", x, y, w, h)
        d.fields = {"search": "", "row": ""}
        self.dialogs.append(d)
        self.active_pid = MT5_PID

    def _open_position_dialog(self, pos: Dict[str, Any]) -> None:
        ox, oy = POSITION_DIALOG_ORIGIN
        w, h = POSITION_DIALOG_SIZE
        d = self._new_dialog("position", "Position #%s" % pos["ticket"], ox, oy, w, h)
        d.ticket = str(pos["ticket"])
        d.fields = {"volume": "%.2f" % pos["lots"]}
        self.dialogs.append(d)
        self.active_pid = MT5_PID

    def _button_side_at(self, d: FakeDialog, x: float, y: float) -> Optional[str]:
        for side in ("buy", "sell"):
            px, py = ORDER_POINTS[side]
            if _hit(x, y, d.x + px, d.y + py, BUTTON_HALF):
                if self.swap_buttons:
                    return "sell" if side == "buy" else "buy"
                return side
        return None

    def _click_dialog(self, d: FakeDialog, x: float, y: float, count: int) -> None:
        if d.page != "form" or d.pending:
            return
        if d.kind == "symbols":
            sx, sy = SYMBOLS_SEARCH_POINT
            if _hit(x, y, d.x + sx, d.y + sy, FIELD_HALF):
                d.focused = "search"
                d.selected = False
                return
            rx, ry = SYMBOLS_ROW_POINT
            if _hit(x, y, d.x + rx, d.y + ry, FIELD_HALF) and d.fields["search"] in self.server_symbols:
                d.fields["row"] = d.fields["search"]
                return
            bx, by = SYMBOLS_SHOW_POINT
            if _hit(x, y, d.x + bx, d.y + by, BUTTON_HALF) and d.fields["row"]:
                sym = d.fields["row"]
                self.shown_symbols.append(sym)
                if self.market_watch is not None and sym not in self.market_watch:
                    self.market_watch.append(sym)
            return
        if d.kind == "order":
            for name in ("symbol", "volume", "sl", "tp"):
                px, py = ORDER_POINTS[name]
                if _hit(x, y, d.x + px, d.y + py, FIELD_HALF):
                    d.focused = name
                    d.selected = False
                    return
            side = self._button_side_at(d, x, y)
            if side is not None:
                self.order_button_clicks.append(side)
                self._execute_order(d, side)
                if self.raise_after_click == "click":
                    raise RuntimeError("simulated: driver failure right after the click")
                if self.raise_after_click == "capture":
                    self._capture_broken = True
            return
        if d.kind == "position":
            vx, vy = POSITION_VOLUME_POINT
            if _hit(x, y, d.x + vx, d.y + vy, FIELD_HALF):
                d.focused = "volume"
                d.selected = False
                return
            cx, cy = CLOSE_BUTTON_POINT
            if _hit(x, y, d.x + cx, d.y + cy, CLOSE_HALF):
                self.close_button_clicks.append(d.ticket or "")
                self._execute_close(d)

    def _execute_order(self, d: FakeDialog, side: str) -> None:
        f = d.fields
        order = {"side": side, "symbol": f.get("symbol"), "volume": f.get("volume"),
                 "sl": f.get("sl"), "tp": f.get("tp")}
        self.orders_sent.append(order)
        d.pending = True
        outcome = self.order_outcome

        def add_position() -> str:
            ticket = str(self.next_ticket)
            self.next_ticket += 1
            self.positions.append({
                "ticket": ticket, "symbol": f.get("symbol"), "side": side, "lots": float(f.get("volume") or 0),
                "open_price": self.fill_price, "sl": float(f.get("sl") or 0), "tp": float(f.get("tp") or 0),
                "price": self.fill_price, "profit": 0.0,
            })
            return ticket

        def show(text: str) -> None:
            if d in self.dialogs:
                d.page = "result"
                d.result_text = text
                d.pending = False

        def done() -> None:
            if outcome == "filled":
                t = add_position()
                show("Done: %s %s %s at %s #%s" % (side, f.get("volume"), f.get("symbol"), self.fill_price, t))
            elif outcome == "wrong_side":
                t = add_position()
                other = "sell" if side == "buy" else "buy"
                show("Done: %s %s %s at %s #%s" % (other, f.get("volume"), f.get("symbol"), self.fill_price, t))
            elif outcome == "done_no_ticket":
                add_position()
                show("Done")
            elif outcome == "rejected":
                show(self.reject_text)
            elif outcome == "unknown":
                show(self.unknown_text)
            elif outcome == "vanish":
                add_position()
                self._remove_dialog(d)
            elif outcome == "nothing":
                d.pending = False
            elif outcome == "custom":
                show(self.custom_result_text)
            elif outcome == "custom_fill":     # the custom text, and the order did fill
                add_position()
                show(self.custom_result_text)
            else:  # pragma: no cover
                raise AssertionError("unknown outcome %r" % outcome)

        self._schedule(self.result_delay_s, done)

    def _execute_close(self, d: FakeDialog) -> None:
        pos = next((p for p in self.positions if str(p["ticket"]) == d.ticket), None)
        d.pending = True
        outcome = self.close_outcome
        try:
            volume = float(d.fields.get("volume") or 0.0)
        except ValueError:
            volume = 0.0
        full_lots = float(pos["lots"]) if pos is not None else 0.0
        if volume <= 0 or volume > full_lots - 1e-9:
            volume = full_lots

        def remove() -> None:
            if pos not in self.positions:
                return
            if volume < float(pos["lots"]) - 1e-9:      # partial close: the rest stays open
                pos["lots"] = round(float(pos["lots"]) - volume, 2)
                self.partial_closes.append((str(pos["ticket"]), volume))
                return
            self.positions.remove(pos)
            self.closed_tickets.append(str(pos["ticket"]))

        def show(text: str) -> None:
            if d in self.dialogs:
                d.page = "result"
                d.result_text = text
                d.pending = False

        def done() -> None:
            if pos is None:
                show("Position not found")  # pragma: no cover
                return
            if outcome == "filled":
                remove()
                show("Done: close #%s %s %.2f %s at %s" % (pos["ticket"], pos["side"], volume, pos["symbol"],
                                                         self.close_price))
            elif outcome == "done_keep":   # MT5 says done but the Toolbox keeps listing the row
                show("Done: close #%s %s %.2f %s at %s" % (pos["ticket"], pos["side"], volume, pos["symbol"],
                                                         self.close_price))
            elif outcome == "rejected":
                show(self.close_reject_text)
            elif outcome == "vanish":
                remove()
                self._remove_dialog(d)
            elif outcome == "vanish_keep":
                self._remove_dialog(d)
            else:  # pragma: no cover
                raise AssertionError("unknown close outcome %r" % outcome)

        self._schedule(self.result_delay_s, done)

    def _click_main(self, x: float, y: float, count: int) -> None:
        mx, my, _, _ = MAIN_RECT
        if _hit(x, y, mx + TRADE_TAB_POINT[0], my + TRADE_TAB_POINT[1] - self.tab_dy(), (30.0, 9.0)):
            if self.trade_tab_works:
                self.toolbox_tab = "trade"
            return
        if count == 2 and self.toolbox_tab == "trade":
            for i, pos in enumerate(self.positions):
                ry = self.row_y(i)
                if abs(y - ry) <= ROW_STEP / 2 and mx <= x <= mx + TOOLBOX_REGION[2]:
                    self._schedule(self.dialog_delay_s, lambda p=pos: self._open_position_dialog(p))
                    return

    # ------------------------------------------------------------------ OCR content

    @staticmethod
    def _item(text: str, cx: float, cy: float, conf: float = 0.95) -> OcrItem:
        w = max(8.0, 7.0 * len(text))
        h = 14.0
        return OcrItem(text=text, conf=conf, x=cx - w / 2.0, y=cy - h / 2.0, w=w, h=h)

    def equity(self) -> float:
        if self.equity_override is not None:
            return self.equity_override
        return self.balance + sum(float(p.get("profit") or 0.0) for p in self.positions)

    def _main_items(self) -> List[OcrItem]:
        mx, my, _, _ = MAIN_RECT
        it = self._item
        tab_y = my + TRADE_TAB_POINT[1] - self.tab_dy()
        items = [
            it("File  Edit  View  Insert  Charts  Tools  Window  Help", mx + 220, my + 35),
            it("EURUSD.h,H1", mx + 60, my + 80),
            it("Trade", mx + TRADE_TAB_POINT[0], tab_y),
            it("Exposure", mx + 110, tab_y),
            it("History", mx + 180, tab_y),
            it("Journal", mx + 250, tab_y),
        ]
        top = my - self.list_dy()          # the Trade list moves with the Toolbox
        if self.toolbox_tab != "trade":
            items += [
                it("Time", mx + 60, top + 615), it("Source", mx + 200, top + 615),
                it("Message", mx + 400, top + 615),
                it("2026.10.01 09:55:00.123", mx + 80, top + 635), it("Network", mx + 200, top + 635),
                it("'12345678': authorized on HantecMarketsMU-MT5", mx + 450, top + 635),
            ]
            return items
        header_y = top + 615
        for text, x in (("Symbol", 30), ("Ticket", 130), ("Time", 250), ("Type", 370), ("Volume", 440),
                        ("Price", 510), ("S / L", 590), ("T / P", 670), ("Price", 750), ("Profit", 900)):
            items.append(it(text, mx + x, header_y))
        for i, p in enumerate(self.positions):
            if str(p["ticket"]) in self.hidden_tickets:
                continue
            y = self.row_y(i)
            digits = 3 if "JPY" in p["symbol"] else (2 if "XAU" in p["symbol"] else 5)
            items += [
                it(p["symbol"], mx + 30, y),
                it(str(p["ticket"]), mx + 130, y),
                it("2026.10.01 09:56:01", mx + 250, y),
                it(p["side"], mx + 370, y),
                it("%.2f" % p["lots"], mx + 440, y),
                it("%.*f" % (digits, p["open_price"]), mx + 510, y),
                it("%.*f" % (digits, p.get("sl") or 0), mx + 590, y),
                it("%.*f" % (digits, p.get("tp") or 0), mx + 670, y),
                it("%.*f" % (digits, p.get("price") or p["open_price"]), mx + 750, y),
                it(fmt_money(float(p.get("profit") or 0.0)), mx + 900, y),
            ]
        acct_y = self.row_y(len(self.positions))
        margin = 0.0 if not self.positions else 1085.0
        equity = self.equity()
        items += [
            it("Balance: %s USD" % fmt_money(self.balance), mx + 90, acct_y),
            it("Equity: %s" % fmt_money(equity), mx + 260, acct_y),
            it("Margin: %s" % fmt_money(margin), mx + 400, acct_y),
            it("Free Margin: %s" % fmt_money(equity - margin), mx + 560, acct_y),
        ]
        if margin:
            items.append(it("Margin Level: %s %%" % fmt_money(equity / margin * 100.0), mx + 760, acct_y))
        # OCR noise below the confidence threshold
        items.append(it("lI1|", mx + 1300, my + 700, conf=0.1))
        return items

    def _dialog_items(self, d: FakeDialog) -> List[OcrItem]:
        it = self._item
        if d.kind == "stray":
            return [it("Alert", d.x + 160, d.y + 40), it("Connection restored", d.x + 160, d.y + 80)]
        if d.kind == "symbols":
            sx, sy = SYMBOLS_SEARCH_POINT
            bx, by = SYMBOLS_SHOW_POINT
            out = [it("Symbols", d.x + 40, d.y + 10, 1.0), it(d.fields["search"] or "Search", d.x + sx, d.y + sy, 1.0),
                   it("Show Symbol", d.x + bx, d.y + by, 1.0)]
            q = d.fields["search"]
            if q and q in self.server_symbols:
                rx, ry = SYMBOLS_ROW_POINT
                out.append(it(q, d.x + rx, d.y + ry, 0.9))    # a pixel-read row (confidence < 1)
            return out
        if d.page == "result":
            return [it(d.result_text, d.x + d.w / 2, d.y + 200), it("OK", d.x + d.w / 2, d.y + 420)]
        if d.kind == "position":
            pos = next((p for p in self.positions if str(p["ticket"]) == d.ticket), None)
            if pos is None:  # pragma: no cover
                return [it("Position not found", d.x + 300, d.y + 200)]
            vol_text = d.fields.get("volume", "%.2f" % pos["lots"])
            try:
                vol_shown = "%.2f" % float(vol_text)
            except ValueError:
                vol_shown = vol_text
            button = self.close_button_text or "Close #%s %s %s %s by Market" % (
                pos["ticket"], pos["side"], vol_shown, pos["symbol"])
            cx, cy = CLOSE_BUTTON_POINT
            vx, vy = POSITION_VOLUME_POINT
            return [
                it("Symbol:", d.x + 60, d.y + 60), it(pos["symbol"], d.x + 310, d.y + 60),
                it("Type:", d.x + 60, d.y + 95), it("Market Execution", d.x + 310, d.y + 95),
                it("Volume:", d.x + 60, d.y + vy), it(vol_text, d.x + vx, d.y + vy),
                it("Modify", d.x + 310, d.y + 300),
                it(button, d.x + cx, d.y + cy),
            ]
        # order form
        f = d.fields
        sym = f.get("symbol", "")
        sym_text = "%s, %s" % (sym, DESCRIPTIONS[sym]) if sym in DESCRIPTIONS else sym
        p = ORDER_POINTS
        sell_label, buy_label = "Sell by Market", "Buy by Market"
        if self.swap_buttons:
            sell_label, buy_label = buy_label, sell_label
        items = [
            it("Symbol:", d.x + 50, d.y + p["symbol"][1]),
            it(sym_text, d.x + p["symbol"][0], d.y + p["symbol"][1]),
            it("Type:", d.x + 50, d.y + 95),
            it("Market Execution", d.x + 310, d.y + 95),
            it("Volume:", d.x + 50, d.y + p["volume"][1]),
            it(f.get("volume", ""), d.x + p["volume"][0], d.y + p["volume"][1]),
            it("Stop Loss:", d.x + 50, d.y + p["sl"][1]),
            it(f.get("sl", ""), d.x + p["sl"][0], d.y + p["sl"][1]),
            it("Take Profit:", d.x + 370, d.y + p["tp"][1]),
            it(f.get("tp", ""), d.x + p["tp"][0], d.y + p["tp"][1]),
            it("Comment:", d.x + 50, d.y + 240),
            it(self.order_quote, d.x + 310, d.y + 300),
            it(sell_label, d.x + p["sell"][0], d.y + p["sell"][1]),
            it(buy_label, d.x + p["buy"][0], d.y + p["buy"][1]),
            it(WARNING_TEXT, d.x + 310, d.y + 430),
        ]
        return [i for i in items if i.text]
