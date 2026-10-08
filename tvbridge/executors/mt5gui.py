"""Deterministic MetaTrader 5 GUI executor (rehearsal and live modes).

Every interaction with the screen goes through the :class:`~tvbridge.gui.driver.Driver`
interface, so this module is fully testable with a fake driver.

Safety design (fail closed for entries, fail open for exits):

* Before an order is sent, every problem raises :class:`ExecutorError` or returns an
  ``"error"`` result, and the order dialog is cancelled with Escape. Return is never
  pressed on an order form, because it could trigger a default Buy/Sell button.
* The Buy/Sell button is clicked at most once, and only after OCR has read back the symbol,
  volume, stop-loss and take-profit exactly and the label under the calibrated button point
  reads the wanted side.
* Before the click every problem is an :class:`ExecutorError` (unexpected exceptions are
  wrapped as ``GUI_ERROR``) or an ``"error"`` result: nothing was sent. A flag is set
  immediately before the single click; from then on nothing is raised: any surprise
  (exception, vanished dialog, unreadable or ambiguous result, a result for another side,
  volume, symbol or price) becomes an ``"uncertain"`` result, which makes the engine halt
  new entries until a human checks MT5.
* The main window is chosen among the MT5-like windows that carry the configured account
  login/server in their title, preferring the calibrated size; several equally good
  candidates are refused (``AMBIGUOUS_MAIN_WINDOW``) rather than guessed.
* A Toolbox position list is only trusted when it can be checked: main window size as
  calibrated, the Trade-list header above the rows, no gaps between rows, and equity -
  balance matching the rows' profit. Otherwise ``read_account`` reports the positions as
  unknown (``positions=None``, note ``TOOLBOX_INCOMPLETE``) and closes still close every
  visible row but end with an ``"uncertain"`` ``TOOLBOX_INCOMPLETE`` result.
* Only text that appeared *after* the click counts as a result. This stops the order form's
  own wording ("... will be executed at market conditions ...") from being read as a fill;
  a fill also needs a ticket or a price before it is believed.
* Mirror mode (``OrderRequest.sl_distance``): the stop (and target) are computed from the
  bid/ask quote read by OCR in the order ticket itself, never from the alert's price (the two
  feeds differ). An unreadable quote (``QUOTE_UNREADABLE``) or one too far from the alert
  price (``PRICE_GAP``, which also catches a wrong symbol) cancels the ticket before any click.
* A partial close types the volume into the position dialog and clicks its close button only
  after OCR reads the button back with exactly that volume and the position's ticket
  (``PARTIAL_VERIFY_FAILED`` otherwise: Escape, nothing sent).
* Rehearsal mode performs every step, including the button-label check, but presses Escape
  instead of clicking Buy, Sell or Close.
* A cross-process lock file (``<home>/gui.lock``) keeps two tvbridge processes (for example
  the engine and a ``tvbridge rehearse`` started in Terminal) from driving MT5 at once.

Screenshots go to ``shots_dir/YYYYMMDD/HHMMSS_mmm_<tag>.png`` (UTC) and are returned as
evidence. Successful account polls, which run every few seconds, keep only the latest
screenshot of the day (``account_latest.png``); failed account reads keep the first
screenshot of a failure streak plus ``account_failed_latest.png``, so the disk does not fill
up while reads keep failing. A capture that raises leaves no file behind.
"""

import errno
from .. import _fcntl as fcntl
import logging
import math
import os
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple, TypeVar

from .. import clock
from ..config import Config
from ..gui import parse
from ..models import SIDES, AccountSnapshot, ObservedPosition, OrderRequest, OrderResult, opposite_side
from .base import Executor, ExecutorError

log = logging.getLogger("tvbridge.executors.mt5gui")

T = TypeVar("T")

POLL_INTERVAL_S = 0.1          # window polling interval
RESULT_POLL_S = 0.5            # result OCR polling interval
SYMBOL_SETTLE_S = 0.5          # MT5 reloads prices after the symbol changes
SYMBOL_SELECT_TIMEOUT_S = 3.0  # the order window's title must name the typed symbol within this
CONFIRM_FILL_TIMEOUT_S = 6.0   # after the ticket vanished without a result: wait this long for the new row
CONFIRM_FILL_POLL_S = 1.0
SYMBOLS_WINDOW_TITLE = "Symbols"
SYMBOLS_SEARCH_OFFSET = (350.0, 78.0)   # search field of MT5's Symbols window (Windows build)
SYMBOLS_FILTER_SETTLE_S = 3.0           # the Symbols window filters its tree after typing
SYMBOLS_SHOW_BUTTON = "show symbol"
STRAY_CLOSE_WAIT_S = 1.0       # wait for a stray dialog to close after Escape
LABEL_MAX_DIST = 80.0          # max distance (points) between button point and its OCR label
MAX_CLOSE_ITERATIONS = 10
STALE_SCAN_RETRIES = 2         # re-reads of the Toolbox while a closed ticket is still listed
STALE_SCAN_WAIT_S = 1.0
MIN_DIALOG_SIDE = 20.0         # smaller same-process windows are invisible helpers, not dialogs
MOVE_TOLERANCE = 1.0           # points the dialog may move between verification and click
GUI_LOCK_TIMEOUT_S = 30.0
ACCOUNT_LATEST_NAME = "account_latest.png"
ACCOUNT_FAILED_LATEST_NAME = "account_failed_latest.png"
MIN_MAIN_W = 600.0
MIN_MAIN_H = 400.0
# Toolbox completeness heuristics (see Mt5GuiExecutor._toolbox_issue)
ROW_GAP_FACTOR = 1.75          # a gap this many times the row pitch means a row was not read
SINGLE_GAP_FACTOR = 2.5        # header -> account line (no rows): at most this many text heights
MONEY_TOL_USD = 2.0
MONEY_TOL_REL = 0.02
SWAP_ALLOWANCE_USD_PER_LOT = 40.0  # swap per lot tolerated when no Swap column is shown (metals held several nights accrue 10-15 USD/lot/night)
PRICE_BAND_REL = 0.01          # a fill further than this from the alert price is a mismatch ...
PRICE_BAND_SL_POINTS = 100     # ... unless within 100 x min_sl_points x point

_TICKET_RE = re.compile(r"#\s?(\d{4,})")
_ORDER_TITLE_RE = re.compile(r"^\s*order\s*:\s*([^\s,\-]+)", re.IGNORECASE)   # "Order: XAUUSD.h - Gold ..."
_SIDE_RE = re.compile(r"\b(buy|sell)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"[^A-Za-z0-9_-]+")
_VOLUME_LABEL_RE = re.compile(r"\bvolume\b\s*[:;.]?", re.IGNORECASE)
LOTS_EPS = 1e-9


# --------------------------------------------------------------------------- small helpers


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").split()).lower()


def _text_set(items: Sequence[Any]) -> Set[str]:
    return {_norm(i.text) for i in items if _norm(i.text)}


def _join_text(items: Sequence[Any]) -> str:
    """All item texts in reading order (rows top to bottom, left to right)."""
    if not items:
        return ""
    rows = parse.group_rows(list(items))
    return " ".join(" ".join(i.text for i in row) for row in rows).strip()


def _side_words(text: str) -> Set[str]:
    return {m.lower() for m in _SIDE_RE.findall(text or "")}


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def login_in_title(login: str, title: Optional[str]) -> bool:
    """True if ``login`` appears in ``title`` as a whole number ("12345678" not in "112345678")."""
    login = (login or "").strip()
    if not login:
        return True
    return re.search(r"(?<!\d)" + re.escape(login) + r"(?!\d)", title or "", re.IGNORECASE) is not None


def select_main_window(windows: Sequence[Any], cfg: Config, calib: Any = None) -> Any:
    """The MT5 main window among ``windows`` (see the module docstring), or ExecutorError.

    Candidates are windows of at least 600x400 (and containing ``main_title_contains``).
    Those whose title lacks ``account_login`` (as a whole number) or ``server_name`` are
    dropped first (none left: ``WRONG_ACCOUNT``). One left: that one. Several: the one
    within ``size_tolerance_px`` of the calibrated main-window size, else
    ``AMBIGUOUS_MAIN_WINDOW``.
    """
    gui = cfg.executor.gui
    needle = _norm(gui.main_title_contains)
    cands = [w for w in windows
             if w.w >= MIN_MAIN_W and w.h >= MIN_MAIN_H and (not needle or needle in _norm(w.title))]
    if not cands:
        raise ExecutorError(
            "MT5_NOT_FOUND",
            "no MetaTrader 5 main window (>= 600x400%s) on screen for owners %s"
            % (", title containing %r" % gui.main_title_contains if needle else "", list(gui.owner_names)))
    acct = cfg.account
    login = (acct.account_login or "").strip()
    server = (acct.server_name or "").strip()
    matches = list(cands)
    for label, want, test in (("account_login", login, lambda w, v: login_in_title(v, w.title)),
                              ("server_name", server, lambda w, v: v.lower() in (w.title or "").lower())):
        if not want:
            continue
        kept = [w for w in matches if test(w, want)]
        if not kept:
            titles = ", ".join(repr(w.title) for w in sorted(matches, key=lambda w: -(w.w * w.h))[:4])
            raise ExecutorError("WRONG_ACCOUNT", "no MT5 window title contains %s %r (windows: %s)"
                                % (label, want, titles))
        matches = kept
    if len(matches) == 1:
        return matches[0]
    mw = getattr(calib, "main_window", None) or {}
    try:
        cw, ch = float(mw["w"]), float(mw["h"])
    except (KeyError, TypeError, ValueError):
        cw = ch = None
    if cw is not None:
        tol = float(gui.size_tolerance_px)
        sized = [w for w in matches if abs(w.w - cw) <= tol and abs(w.h - ch) <= tol]
        if len(sized) == 1:
            return sized[0]
    raise ExecutorError(
        "AMBIGUOUS_MAIN_WINDOW",
        "%d windows could be the MT5 main window (%s); close the others (MetaEditor, MT4, a second terminal) "
        "or set account.account_login" % (len(matches), ", ".join(
            "%r %.0fx%.0f" % (w.title, w.w, w.h) for w in matches[:4])))


class _Outcome(object):
    """Result of waiting for MT5's answer after a click."""

    def __init__(self, status: str, message: str, ticket: Optional[str] = None,
                 price: Optional[float] = None, vanished: bool = False, text: str = ""):
        self.status = status          # "filled" | "rejected" | "uncertain"
        self.message = message
        self.ticket = ticket
        self.price = price
        self.vanished = vanished      # the dialog disappeared while waiting
        self.text = text or message   # all new text read (for the post-fill checks)


class _GuiLock(object):
    """Re-entrant in-process lock plus an exclusive ``flock`` on a file across processes.

    If the lock file cannot be created the executor still works (with a warning), but if
    another process holds the lock for longer than ``timeout`` -> ExecutorError("GUI_BUSY").
    """

    def __init__(self, path: Optional[Path], timeout: float = GUI_LOCK_TIMEOUT_S):
        self.path = path
        self.timeout = float(timeout)
        self._tlock = threading.RLock()
        self._depth = 0
        self._fh = None  # type: Any

    def __enter__(self) -> "_GuiLock":
        if not self._tlock.acquire(timeout=self.timeout):
            raise ExecutorError("GUI_BUSY", "another thread is driving MetaTrader")
        try:
            if self._depth == 0:
                self._acquire_file()
        except BaseException:
            self._tlock.release()
            raise
        self._depth += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        self._depth -= 1
        try:
            if self._depth == 0:
                self._release_file()
        finally:
            self._tlock.release()

    def _acquire_file(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(str(self.path), "a+")
        except OSError as e:
            log.warning("cannot open GUI lock file %s (%s); continuing without the cross-process lock",
                        self.path, e)
            return
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                return
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    fh.close()
                    log.warning("cannot lock %s (%s); continuing without the cross-process lock", self.path, e)
                    return
                if time.monotonic() >= deadline:
                    fh.close()
                    raise ExecutorError(
                        "GUI_BUSY", "another tvbridge process is driving MetaTrader (lock %s)" % self.path)
                time.sleep(0.1)  # real wait: another process holds the lock

    def _release_file(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        fh.close()


# --------------------------------------------------------------------------- executor


class Mt5GuiExecutor(Executor):
    """Places and closes market orders by driving the MetaTrader 5 window.

    ``rehearsal=True`` does everything except the final Buy/Sell/Close click.
    """

    name = "mt5gui"

    def __init__(self, cfg: Config, driver: Any, calib: Any, rehearsal: bool, shots_dir: Path):
        self.cfg = cfg
        self.gui = cfg.executor.gui
        self.driver = driver
        self.calib = calib
        self.rehearsal = bool(rehearsal)
        self.shots_dir = Path(shots_dir)
        self._recent_shots = deque(maxlen=512)  # type: Deque[str]
        shared = str(getattr(cfg.executor.gui, "lock_path", "") or "")
        if shared:
            lock_path = Path(os.path.expanduser(shared))  # type: Optional[Path]
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            lock_path = Path(cfg.home) / "gui.lock" if cfg.home else None
        self._gui_lock = _GuiLock(lock_path, float(getattr(cfg.executor.gui, "lock_timeout_s", GUI_LOCK_TIMEOUT_S)))
        #: Optional callable returning a reason (str) to abort an entry right before the click
        #: (the engine checks pause/halt/flatten there), or None to go ahead.
        self.abort_check = None  # type: Optional[Callable[[], Optional[str]]]
        self._acct_fail_streak = 0
        self._scan_pngs = []  # type: List[str]
        self._last_toolbox_issue = ""
        #: Add a missing symbol to the Market Watch through MT5's Symbols window (Ctrl+U) when the
        #: order window refuses it. Native Windows MT5 only: its Symbols window is calibrated here.
        self.can_add_symbols = os.name == "nt"

    @property
    def mode(self) -> str:
        return "rehearsal" if self.rehearsal else "live"

    # ------------------------------------------------------------------ windows

    def _windows(self) -> List[Any]:
        return list(self.driver.list_windows(list(self.gui.owner_names)))

    def _find_window(self, wid: int) -> Optional[Any]:
        for w in self._windows():
            if w.wid == wid:
                return w
        return None

    def _main_window(self) -> Any:
        """The MT5 main window, account-checked (see :func:`select_main_window`)."""
        return select_main_window(self._windows(), self.cfg, self.calib)

    def _main_size_issue(self, main: Any) -> str:
        """"" if the main window has its calibrated size (within size_tolerance_px), else why not."""
        mw = getattr(self.calib, "main_window", None) or {}
        try:
            cw, ch = float(mw["w"]), float(mw["h"])
        except (KeyError, TypeError, ValueError):
            return ""
        tol = float(self.gui.size_tolerance_px)
        if abs(main.w - cw) > tol or abs(main.h - ch) > tol:
            return ("MT5 main window is %.0fx%.0f, calibrated %.0fx%.0f (tolerance %.0f)"
                    % (main.w, main.h, cw, ch, tol))
        return ""

    def _other_windows(self, main: Any) -> List[Any]:
        """Visible windows of the MT5 process other than the main window."""
        return [w for w in self._windows()
                if w.pid == main.pid and w.wid != main.wid
                and w.w >= MIN_DIALOG_SIDE and w.h >= MIN_DIALOG_SIDE]

    def _dismiss_stray_dialogs(self, main: Any) -> None:
        """Escape (twice at most) every other MT5 window; ExecutorError("STRAY_DIALOG") if any remain."""
        strays = self._other_windows(main)
        if not strays:
            return
        log.warning("dismissing %d stray MT5 window(s): %s", len(strays), [w.title for w in strays])
        for w in strays:
            for _ in range(2):
                if self._find_window(w.wid) is None:
                    break
                self.driver.activate(main.pid)
                self.driver.key("escape")
                if self._wait_gone(w.wid, min(STRAY_CLOSE_WAIT_S, float(self.gui.dialog_timeout_s))):
                    break
        remaining = self._other_windows(main)
        close = getattr(self.driver, "close_window", None)
        if remaining and close is not None:
            for w in remaining:
                try:
                    close(w.wid)
                except Exception as e:
                    log.warning("close request failed for window %s: %s", w.wid, e)
                self._wait_gone(w.wid, min(STRAY_CLOSE_WAIT_S, float(self.gui.dialog_timeout_s)))
            remaining = self._other_windows(main)
        if remaining:
            raise ExecutorError("STRAY_DIALOG", "MT5 window(s) still open after Escape: %s"
                                % ", ".join(repr(w.title) for w in remaining))

    def _strays_for_exit(self, main: Any) -> List[Any]:
        """Like :meth:`_dismiss_stray_dialogs` but never raises (closes fail open): returns the
        MT5 windows still open after Escape. Nothing is ever clicked inside them."""
        try:
            self._dismiss_stray_dialogs(main)
            return []
        except ExecutorError as e:
            log.warning("continuing the close despite %s", e.reason)
            return self._other_windows(main)

    @staticmethod
    def _covers(win: Any, x: float, y: float) -> bool:
        return win.x <= x <= win.x + win.w and win.y <= y <= win.y + win.h

    def _focus(self, main: Any) -> None:
        self.driver.activate(main.pid)
        x, y = self._point(main, self.calib.focus_point)
        self.driver.click(x, y)
        self.driver.sleep(self.gui.action_delay_s)

    def _poll(self, timeout: float, interval: float, fn: Callable[[], Optional[T]]) -> Optional[T]:
        """Call ``fn`` until it returns non-None or ``timeout`` elapses (driver time or wall time)."""
        start = time.monotonic()
        waited = 0.0
        while True:
            r = fn()
            if r is not None:
                return r
            if waited >= timeout - 1e-9 or time.monotonic() - start >= timeout:
                return None
            self.driver.sleep(interval)
            waited += interval

    def _wait_for_new_window(self, pid: int, before_ids: Set[int], title_needles: Sequence[str],
                             timeout: float) -> Optional[Any]:
        needles = [_norm(n) for n in title_needles if _norm(n)]

        def probe() -> Optional[Any]:
            for w in self._windows():
                if w.pid != pid or w.wid in before_ids:
                    continue
                if w.w < MIN_DIALOG_SIDE or w.h < MIN_DIALOG_SIDE:
                    continue
                title = _norm(w.title)
                if not needles or any(n in title for n in needles):
                    return w
            return None

        return self._poll(float(timeout), POLL_INTERVAL_S, probe)

    def _wait_gone(self, wid: int, timeout: float) -> bool:
        return bool(self._poll(float(timeout), POLL_INTERVAL_S,
                               lambda: True if self._find_window(wid) is None else None))

    def _escape_window(self, win: Any, pid: int, allow_return: bool = False) -> bool:
        """Close ``win`` with Escape (twice), then Return only if ``allow_return``. True if gone.

        ``allow_return`` must only be set on a result page (its default button is OK); on an
        order form Return could press Buy or Sell.
        """
        keys = ["escape", "escape"] + (["return"] if allow_return else [])
        for k in keys:
            if self._find_window(win.wid) is None:
                return True
            self.driver.activate(pid)
            self.driver.key(k)
            if self._wait_gone(win.wid, float(self.gui.dialog_timeout_s)):
                return True
        cur = self._find_window(win.wid)
        if cur is None:
            return True
        close = getattr(self.driver, "close_window", None)
        if close is not None:
            # Windows: a close request to the window itself (a result page closes, a form is cancelled)
            try:
                close(win.wid)
            except Exception as e:
                log.warning("close request failed for window %s: %s", win.wid, e)
            return self._wait_gone(win.wid, float(self.gui.dialog_timeout_s))
        # Keys sometimes do not reach a second Wine process: use the window's own close button
        # (top-left of the title bar). Closing an order form cancels it; nothing is sent.
        try:
            self.driver.activate(pid)
            self.driver.click(float(cur.x) + 14.0, float(cur.y) + 13.0)
        except Exception as e:
            log.warning("close-button click failed for window %s: %s", win.wid, e)
        return self._wait_gone(win.wid, float(self.gui.dialog_timeout_s))

    def _open_order_dialog(self, main: Any, before: Any) -> Any:
        """Open the New Order window: F9, or the toolbar's "New Order" button (found by OCR)."""
        gui = self.gui
        via = str(getattr(gui, "open_order_via", "f9") or "f9").lower()
        if via != "toolbar":
            self.driver.key("f9")
            dlg = self._wait_for_new_window(main.pid, before, gui.order_dialog_title_contains, gui.dialog_timeout_s)
            if dlg is not None:
                return dlg
        items, png = self._ocr(main, (0.0, 40.0, float(main.w), 40.0), "toolbar")
        try:
            os.remove(png)
        except OSError:
            pass
        btn = [i for i in items if re.search(r"\bNew Order\b", i.text)]
        if not btn:
            return None
        it = btn[0]
        m = re.search(r"\bNew Order\b", it.text)
        n = float(max(1, len(it.text)))
        x = it.x + it.w * ((m.start() + m.end()) / 2.0) / n
        self.driver.click(x, it.cy)
        return self._wait_for_new_window(main.pid, before, gui.order_dialog_title_contains, gui.dialog_timeout_s)

    def _ticket_snapshot(self, main: Any) -> Optional[Set[str]]:
        """Tickets of the positions MT5 lists right now, or None when the list is not readable
        (then a vanished ticket can never be confirmed from the list)."""
        try:
            rows, issue = self._scan_positions(main, "pre_order")
        except Exception as e:
            log.info("Trade list not readable before the order (%s); a vanished ticket will stay uncertain", e)
            return None
        if issue or any(not p.ticket for p, _ in rows):
            return None
        return {str(p.ticket) for p, _ in rows}

    def _confirm_fill_from_toolbox(self, main: Any, req: OrderRequest, pre_tickets: Optional[Set[str]],
                                   evidence: List[str]) -> Optional["_Outcome"]:
        """After the order window vanished without a result: the fill it made, read from the
        Trade list. Exactly one NEW row (ticket not listed before the click) with the request's
        symbol, side and lots within CONFIRM_FILL_TIMEOUT_S proves the fill; otherwise None."""
        if pre_tickets is None:
            return None
        want_lots = float(req.lots)
        start = time.monotonic()
        waited = 0.0
        while True:
            try:
                rows, issue = self._scan_positions(main, "fill_confirm")
            except Exception as e:
                log.info("Trade list not readable while confirming the fill: %s", e)
                rows, issue = [], str(e)
            if not issue:
                new = [p for p, _ in rows
                       if p.ticket and str(p.ticket) not in pre_tickets
                       and (p.symbol or "").lower() == (req.symbol or "").lower()
                       and p.side == req.side and abs(float(p.lots) - want_lots) <= LOTS_EPS]
                if len(new) == 1:
                    p = new[0]
                    if self._scan_pngs:
                        evidence.append(self._scan_pngs[-1])
                    msg = "confirmed from the Trade list: %s %s %s #%s at %s" % (
                        p.side, "%.*f" % (int(req.lot_decimals), float(p.lots)), p.symbol, p.ticket,
                        p.open_price if p.open_price is not None else "?")
                    log.info("the order window closed without a result; %s", msg)
                    return _Outcome("filled", msg, str(p.ticket), p.open_price, text=msg)
                if len(new) > 1:
                    log.warning("%d new positions match %s %s %s; cannot tell which is ours",
                                len(new), req.side, req.lots, req.symbol)
                    return None
            if waited >= CONFIRM_FILL_TIMEOUT_S - 1e-9 or time.monotonic() - start >= CONFIRM_FILL_TIMEOUT_S:
                return None
            self.driver.sleep(CONFIRM_FILL_POLL_S)
            waited += CONFIRM_FILL_POLL_S

    def _open_checked_dialog(self, main: Any, state: Dict[str, Any]) -> Any:
        """Open the New Order window and check its calibrated size (ExecutorError otherwise)."""
        gui = self.gui
        before = {w.wid for w in self._windows()}
        dlg = self._open_order_dialog(main, before)
        if dlg is None:
            raise ExecutorError("ORDER_DIALOG_NOT_OPENED", "F9 did not open a window titled %s within %.1f s"
                                % (list(gui.order_dialog_title_contains), float(gui.dialog_timeout_s)))
        state["dlg"] = dlg
        od = self.calib.order_dialog
        cw, ch = float(od["w"]), float(od["h"])
        tol = float(gui.size_tolerance_px)
        if abs(dlg.w - cw) > tol or abs(dlg.h - ch) > tol:
            raise ExecutorError(
                "DIALOG_LAYOUT_CHANGED",
                "order dialog is %.0fx%.0f, calibrated %.0fx%.0f (tolerance %.0f); run `tvbridge calibrate`"
                % (dlg.w, dlg.h, cw, ch, tol))
        return dlg

    def _wait_symbol_shown(self, dlg: Any, symbol: str) -> Optional[bool]:
        """Does the order window's title name ``symbol``? True / False, or None when the title
        names no symbol at all (older builds: the field verification catches it later)."""
        want = _norm(symbol)

        def probe() -> Optional[bool]:
            cur = self._find_window(dlg.wid)
            if cur is None:
                return None
            m = _ORDER_TITLE_RE.match(cur.title or "")
            if not m:
                return None
            return True if _norm(m.group(1)) == want else None

        if self._poll(SYMBOL_SELECT_TIMEOUT_S, POLL_INTERVAL_S, probe):
            return True
        cur = self._find_window(dlg.wid)
        if cur is None or not _ORDER_TITLE_RE.match(cur.title or ""):
            return None
        return False

    def _add_symbol_to_market_watch(self, main: Any, symbol: str) -> bool:
        """Show ``symbol`` in the Market Watch through MT5's Symbols window (Ctrl+U): type it in
        the search field, click its row, click "Show Symbol", close the window. True on success;
        never raises (a failure just means the entry is refused)."""
        gui = self.gui
        try:
            self._focus(main)
            before = {w.wid for w in self._windows()}
            self.driver.key("u", ("ctrl",))
            win = self._wait_for_new_window(main.pid, before, [SYMBOLS_WINDOW_TITLE], gui.dialog_timeout_s)
            if win is None:
                log.warning("Ctrl+U did not open the Symbols window")
                return False
            try:
                dx, dy = SYMBOLS_SEARCH_OFFSET
                self.driver.click(win.x + dx, win.y + dy)
                self.driver.sleep(gui.action_delay_s)
                self.driver.type_text(symbol)
                self.driver.sleep(SYMBOLS_FILTER_SETTLE_S)
                cur = self._find_window(win.wid) or win
                png = str(self._shot_path("symbols_%s" % symbol))
                scale = self.driver.capture(cur, png)
                items = self.driver.ocr(png, cur, scale)
                rows = [i for i in items
                        if parse.find_symbol(i.text or "", [symbol]) is not None and float(i.conf or 0) < 1.0]
                buttons = [i for i in items if _norm(i.text) == SYMBOLS_SHOW_BUTTON]
                if not rows or not buttons:
                    log.warning("Symbols window: row for %s %s, Show Symbol button %s (%s)", symbol,
                                "found" if rows else "not found", "found" if buttons else "not found", png)
                    return False
                row = sorted(rows, key=lambda i: i.y)[0]
                self.driver.click(row.cx, row.cy)
                self.driver.sleep(gui.action_delay_s)
                self.driver.click(buttons[0].cx, buttons[0].cy)
                self.driver.sleep(gui.action_delay_s)
                log.info("added %s to the Market Watch (Symbols window)", symbol)
                return True
            finally:
                cur = self._find_window(win.wid)
                if cur is not None and not self._escape_window(cur, main.pid):
                    log.warning("the Symbols window did not close")
        except Exception as e:   # pragma: no cover - defensive
            log.warning("adding %s to the Market Watch failed: %s: %s", symbol, type(e).__name__, e)
            return False

    @staticmethod
    def _point(win: Any, rel: Sequence[float]) -> Tuple[float, float]:
        return float(win.x) + float(rel[0]), float(win.y) + float(rel[1])

    # ------------------------------------------------------------------ screenshots & OCR

    def _shot_path(self, tag: str) -> Path:
        now = clock.utcnow()
        day_dir = self.shots_dir / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        safe = _TAG_RE.sub("_", tag or "shot").strip("_") or "shot"
        base = "%s_%03d_%s" % (now.strftime("%H%M%S"), now.microsecond // 1000, safe)
        path = day_dir / (base + ".png")
        n = 2
        while path.exists() or str(path) in self._recent_shots:
            path = day_dir / ("%s-%d.png" % (base, n))
            n += 1
        self._recent_shots.append(str(path))
        return path

    def _ocr(self, win: Any, region: Optional[Sequence[float]] = None,
             tag: str = "shot") -> Tuple[List[Any], str]:
        """Screenshot ``win`` (evidence) and OCR it; low-confidence items are dropped."""
        path = str(self._shot_path(tag))
        try:
            scale = self.driver.capture(win, path)
        except BaseException:
            _unlink(path)   # e.g. the blank image of a refused capture
            raise
        reg = tuple(float(v) for v in region) if region else None
        items = self.driver.ocr(path, win, scale, reg)
        min_conf = float(self.gui.ocr_min_confidence)
        kept = [i for i in items if float(i.conf or 0.0) >= min_conf and (i.text or "").strip()]
        return kept, path

    def _rotate_failed_account_shots(self, pngs: Sequence[str]) -> str:
        """After a failed account read: keep the streak's first screenshot and the newest one
        as ``account_failed_latest.png``; delete the rest. Returns the newest path kept."""
        pngs = [p for p in pngs if p]
        if not pngs:
            return ""
        newest = pngs[-1]
        latest = str(Path(newest).parent / ACCOUNT_FAILED_LATEST_NAME)
        first = self._acct_fail_streak == 0
        self._acct_fail_streak += 1
        try:
            if first:
                if os.path.exists(newest):
                    shutil.copyfile(newest, latest)
                for p in pngs[1:]:
                    _unlink(p)
            else:
                if os.path.exists(newest):
                    os.replace(newest, latest)
                for p in pngs[:-1]:
                    _unlink(p)
        except OSError as e:  # pragma: no cover - best effort
            log.debug("could not rotate failed account screenshots: %s", e)
        return latest

    def _keep_as_latest(self, png: str) -> None:
        """Replace the day's ``account_latest.png`` with ``png`` (routine polls leave one file)."""
        try:
            src = Path(png)
            if src.exists():
                os.replace(str(src), str(src.parent / ACCOUNT_LATEST_NAME))
        except OSError as e:  # pragma: no cover - best effort
            log.debug("could not rotate account screenshot %s: %s", png, e)

    # ------------------------------------------------------------------ fields

    def _set_field(self, x: float, y: float, text: str) -> None:
        """Click a field, select its content, type ``text`` and commit with Tab."""
        self.driver.click(x, y)
        self.driver.sleep(self.gui.action_delay_s)
        self.driver.key("end")
        self.driver.key("home", ("shift",))
        self.driver.type_text(text)
        self.driver.key("tab")
        self.driver.sleep(self.gui.action_delay_s)

    def _mt5_name(self, symbol: str) -> str:
        tv = self.cfg.tv_symbol_for(symbol)
        return self.cfg.mt5_symbol(tv) if tv else (symbol or "").strip()

    # ------------------------------------------------------------------ results

    def _await_result(self, dlg: Any, baseline: Set[str], tag: str, evidence: List[str]) -> _Outcome:
        """Poll the dialog's OCR every 0.5 s for a fill or rejection (only text new since the click)."""
        timeout = float(self.gui.result_timeout_s)
        start = time.monotonic()
        waited = 0.0
        last_png = None  # type: Optional[str]
        last_text = ""
        note = ""
        while waited < timeout - 1e-9 and time.monotonic() - start < timeout:
            self.driver.sleep(RESULT_POLL_S)
            waited += RESULT_POLL_S
            cur = self._find_window(dlg.wid)
            if cur is None:
                if last_png:
                    evidence.append(last_png)
                return _Outcome("uncertain", "the dialog closed before a result could be read" + note,
                                vanished=True)
            try:
                items, png = self._ocr(cur, tag=tag)
            except Exception:
                if self._find_window(dlg.wid) is None:
                    if last_png:
                        evidence.append(last_png)
                    return _Outcome("uncertain", "the dialog closed before a result could be read" + note,
                                    vanished=True)
                raise
            last_png = png
            text = _join_text([i for i in items if _norm(i.text) not in baseline])
            if not text:
                continue
            last_text = text
            status, msg, ticket, price = parse.parse_order_result(text)
            if status == "uncertain":
                # timeout / no connection / error: the order may or may not have executed
                evidence.append(png)
                return _Outcome("uncertain", "ambiguous result %r" % (msg or text), ticket, price, text=text)
            if status == "rejected":
                evidence.append(png)
                return _Outcome("rejected", msg or text, ticket, price, text=text)
            if status == "filled":
                if ticket or price is not None:
                    evidence.append(png)
                    return _Outcome("filled", msg or text, ticket, price, text=text)
                note = "; the result looked like a fill but no ticket or price was readable: %r" % text
        if last_png:
            evidence.append(last_png)
        if not note and last_text:
            note = "; last text read: %r" % last_text
        return _Outcome("uncertain", "no confirmed result within %.1f s%s" % (timeout, note))

    # ------------------------------------------------------------------ open

    @staticmethod
    def _check_request(req: OrderRequest) -> Optional[str]:
        if req.side not in SIDES:
            return "side must be buy or sell, got %r" % (req.side,)
        if not (req.symbol or "").strip():
            return "symbol is empty"
        if not _finite(req.lots) or float(req.lots) <= 0:
            return "lots must be > 0, got %r" % (req.lots,)
        if req.sl_distance is not None:
            # mirror mode: the stop comes from the ticket's own quote, never from req.sl
            if not _finite(req.sl_distance) or float(req.sl_distance) <= 0:
                return "sl_distance must be > 0, got %r" % (req.sl_distance,)
            if req.tp_distance is not None and (not _finite(req.tp_distance) or float(req.tp_distance) < 0):
                return "invalid tp_distance %r" % (req.tp_distance,)
        elif req.sl is None or not _finite(req.sl) or float(req.sl) <= 0:
            return "a stop-loss is required, got %r" % (req.sl,)
        if req.tp is not None and (not _finite(req.tp) or float(req.tp) < 0):
            return "invalid take-profit %r" % (req.tp,)
        if not (0 <= int(req.digits) <= 10) or not (0 <= int(req.lot_decimals) <= 4):
            return "invalid digits/lot_decimals (%r, %r)" % (req.digits, req.lot_decimals)
        return None

    def open_market(self, req: OrderRequest) -> OrderResult:
        """Fill the New Order ticket, verify it by OCR and click Buy/Sell once (live only)."""
        problem = self._check_request(req)
        if problem:
            return OrderResult("error", "BAD_REQUEST: " + problem)
        with self._gui_lock:
            return self._open_market_locked(req)

    def _open_market_locked(self, req: OrderRequest) -> OrderResult:
        """Run the order sequence; pre-click failures raise ExecutorError, post-click never raises."""
        side = req.side
        state = {"clicked": False, "dlg": None, "pid": None}   # type: Dict[str, Any]
        evidence = []  # type: List[str]
        try:
            return self._open_market_steps(req, state, evidence)
        except Exception as e:
            dlg, pid = state.get("dlg"), state.get("pid")
            if state["clicked"]:
                log.exception("exception after clicking %s", side)
                if dlg is not None:
                    self._best_effort_escape(dlg, pid)
                return OrderResult(
                    "uncertain",
                    "UNCERTAIN_EXECUTION: %s after clicking %s: %s" % (type(e).__name__, side, e),
                    lots=float(req.lots), evidence=evidence)
            if dlg is not None:
                self._best_effort_escape(dlg, pid)
            if isinstance(e, ExecutorError):
                raise
            log.exception("GUI error before the %s click", side)
            raise ExecutorError("GUI_ERROR", "%s before the click: %s; nothing was sent" % (type(e).__name__, e))

    def _open_market_steps(self, req: OrderRequest, state: Dict[str, Any], evidence: List[str]) -> OrderResult:
        gui = self.gui
        side = req.side
        lots_str = "%.*f" % (int(req.lot_decimals), float(req.lots))
        by_distance = req.sl_distance is not None
        sl_val = None if by_distance else float(req.sl)                 # type: Optional[float]
        tp_val = None if by_distance else (float(req.tp) if req.tp else None)   # type: Optional[float]

        # 1. main window (calibrated size), stray dialogs, focus, F9
        main = self._main_window()
        state["pid"] = main.pid
        issue = self._main_size_issue(main)
        if issue:
            raise ExecutorError("MAIN_LAYOUT_CHANGED", "%s; run `tvbridge calibrate`" % issue)
        self._dismiss_stray_dialogs(main)
        self._focus(main)
        pre_tickets = self._ticket_snapshot(main)       # to recognise our own fill in the Trade list
        dlg = self._open_checked_dialog(main, state)   # 2. incl. layout check
        od = self.calib.order_dialog
        pts = od["points"]

        # 3. the symbol: MT5 silently keeps the previous symbol when the typed one is not in
        # its Market Watch (MT5 hides symbols no chart or position uses after a while). The
        # window title names the selected symbol: wait for it, add the symbol once if needed.
        for attempt in (1, 2):
            sx, sy = self._point(dlg, pts["symbol"])
            self._set_field(sx, sy, req.symbol)
            self.driver.sleep(SYMBOL_SETTLE_S)
            shown = self._wait_symbol_shown(dlg, req.symbol)
            if shown is not False:
                break
            cur = self._find_window(dlg.wid)
            if cur is not None:
                self._escape_window(cur, main.pid)
            state["dlg"] = None
            added = attempt == 1 and self.can_add_symbols and self._add_symbol_to_market_watch(main, req.symbol)
            if not added:
                msg = ("SYMBOL_NOT_SELECTED: the order window kept its previous symbol after %r was typed: "
                       "%s is not in MT5's Market Watch (View > Symbols > Show Symbol)%s; nothing was sent"
                       % (req.symbol, req.symbol, "" if attempt == 1 else " and adding it did not help"))
                log.warning(msg)
                return OrderResult("error", msg, evidence=evidence)
            log.warning("%s was not selectable in the order window; added it to the Market Watch, retrying",
                        req.symbol)
            self._focus(main)
            dlg = self._open_checked_dialog(main, state)

        # 3b. mirror mode: stop/target at a fixed distance from this ticket's own quote
        if by_distance:
            problem, sl_val, tp_val = self._levels_from_quote(req, dlg, evidence)
            if problem:
                cur = self._find_window(dlg.wid)
                closed = True if cur is None else self._escape_window(cur, main.pid)
                state["dlg"] = None
                msg = "%s; nothing was sent" % problem
                if not closed:
                    msg += " (order dialog did not close)"
                log.warning(msg)
                return OrderResult("error", msg, evidence=evidence)
        sl_str = "%.*f" % (int(req.digits), float(sl_val))   # type: ignore[arg-type]
        tp_str = "%.*f" % (int(req.digits), float(tp_val)) if tp_val else None

        vx, vy = self._point(dlg, pts["volume"])
        self._set_field(vx, vy, lots_str)
        lx, ly = self._point(dlg, pts["sl"])
        self._set_field(lx, ly, sl_str)
        tx, ty = self._point(dlg, pts["tp"])
        self._set_field(tx, ty, tp_str or "0")

        cur = self._find_window(dlg.wid)
        if cur is None:
            state["dlg"] = None
            return OrderResult("error", "ORDER_DIALOG_CLOSED: the order dialog closed while it was being "
                                        "filled; nothing was sent", evidence=evidence)
        dlg = cur
        state["dlg"] = dlg

        # 4. OCR verification (every field paired with its own label)
        items, png = self._ocr(dlg, tag="order_%s_verify" % side)
        evidence.append(png)
        ok, problems = parse.verify_dialog_fields(items, req.symbol, lots_str, sl_str, tp_str,
                                                  list(gui.require_dialog_text))
        if not ok and self._find_window(dlg.wid) is not None:
            # Keystrokes are occasionally dropped (seen on Wine): fill the fields once more before
            # giving up. Nothing has been clicked, so this can never send anything twice.
            log.warning("ticket fields did not verify (%s); filling them again once", "; ".join(problems))
            self._set_field(sx, sy, req.symbol)
            self.driver.sleep(SYMBOL_SETTLE_S)
            self._set_field(vx, vy, lots_str)
            self._set_field(lx, ly, sl_str)
            self._set_field(tx, ty, tp_str or "0")
            cur = self._find_window(dlg.wid)
            if cur is not None:
                dlg = cur
                state["dlg"] = dlg
                items, png = self._ocr(dlg, tag="order_%s_verify2" % side)
                evidence.append(png)
                ok, problems = parse.verify_dialog_fields(items, req.symbol, lots_str, sl_str, tp_str,
                                                          list(gui.require_dialog_text))
        if not ok:
            closed = self._escape_window(dlg, main.pid)
            state["dlg"] = None
            msg = "VERIFY_FAILED: %s; nothing was sent" % ("; ".join(problems) or "fields did not read back")
            if not closed:
                msg += " (order dialog did not close)"
            log.warning(msg)
            return OrderResult("error", msg, evidence=evidence)

        # 6. button guard (also run in rehearsal, so a rehearsal proves the calibration)
        bx, by = self._point(dlg, pts[side])
        label = parse.nearest_label(items, (bx, by), ["buy", "sell"], LABEL_MAX_DIST)
        if (label or "").lower() != side:
            self._escape_window(dlg, main.pid)
            state["dlg"] = None
            msg = ("BUTTON_LABEL_MISMATCH: expected %r at the calibrated %s button (%.0f, %.0f), OCR read %r; "
                   "nothing was sent" % (side, side, bx, by, label))
            log.warning(msg)
            return OrderResult("error", msg, evidence=evidence)

        summary = "%s %s %s sl %s tp %s" % (side, lots_str, req.symbol, sl_str, tp_str or "-")

        # 5. rehearsal stops here
        if self.rehearsal:
            closed = self._escape_window(dlg, main.pid)
            state["dlg"] = None
            msg = "REHEARSED: verified %s and the %s button; pressed Escape, nothing was sent" % (summary, side)
            if not closed:
                msg += " (order dialog did not close)"
            log.info(msg)
            return OrderResult("rehearsed", msg, lots=float(req.lots), evidence=evidence, sl=sl_val, tp=tp_val)

        # the dialog must still be exactly where it was verified
        cur = self._find_window(dlg.wid)
        if cur is None or abs(cur.x - dlg.x) > MOVE_TOLERANCE or abs(cur.y - dlg.y) > MOVE_TOLERANCE \
                or abs(cur.w - dlg.w) > MOVE_TOLERANCE or abs(cur.h - dlg.h) > MOVE_TOLERANCE:
            if cur is not None:
                self._escape_window(cur, main.pid)
            state["dlg"] = None
            return OrderResult("error", "DIALOG_MOVED: the order dialog moved or closed after verification; "
                                        "nothing was sent", evidence=evidence)

        # last chance to stop (pause / halt / flatten requested while the ticket was filled)
        reason = self._abort_reason()
        if reason:
            closed = self._escape_window(dlg, main.pid)
            state["dlg"] = None
            msg = "ABORTED: %s; pressed Escape, nothing was sent" % reason
            if not closed:
                msg += " (order dialog did not close)"
            log.warning(msg)
            return OrderResult("error", msg, evidence=evidence)

        # 7. the single click: from here on nothing is raised (state["clicked"])
        baseline = _text_set(items)
        log.info("LIVE: clicking %s for %s", side.upper(), summary)
        state["clicked"] = True
        self.driver.click(bx, by)
        outcome = self._await_result(dlg, baseline, "order_%s_result" % side, evidence)
        if outcome.vanished:
            # MT5 sometimes closes the ticket right after a fill without showing its result page
            # (seen on the Mac build): the Trade list is the proof then.
            confirmed = self._confirm_fill_from_toolbox(main, req, pre_tickets, evidence)
            if confirmed is not None:
                outcome = confirmed

        status, message = outcome.status, outcome.message
        if status == "filled":
            mismatch = self._fill_mismatch(req, lots_str, outcome)
            if mismatch:
                status = "uncertain"
                message = "RESULT_MISMATCH: %s; MT5 reported %r" % (mismatch, outcome.message)

        # 8. close the dialog
        if not outcome.vanished:
            closed = self._escape_window(dlg, main.pid, allow_return=status in ("filled", "rejected"))
            if not closed:
                message += " (result dialog did not close)"
        state["dlg"] = None

        # 9. report
        if status == "filled":
            log.info("FILLED: %s -> %s", summary, message)
            return OrderResult("filled", message, fill_price=outcome.price, ticket=outcome.ticket,
                               lots=float(req.lots), evidence=evidence, sl=sl_val, tp=tp_val)
        if status == "rejected":
            log.warning("REJECTED: %s -> %s", summary, message)
            return OrderResult("rejected", "REJECTED: " + message, evidence=evidence)
        if not message.startswith("RESULT_MISMATCH"):
            message = "UNCERTAIN_EXECUTION: clicked %s for %s; %s" % (side, summary, message)
        log.error(message)
        return OrderResult("uncertain", message, lots=float(req.lots), evidence=evidence, sl=sl_val, tp=tp_val)

    def _levels_from_quote(self, req: OrderRequest, dlg: Any, evidence: List[str]
                           ) -> Tuple[str, Optional[float], Optional[float]]:
        """(problem, sl, tp) from the order ticket's own bid/ask quote (mirror mode).

        buy: sl = ask - sl_distance, tp = ask + tp_distance; sell: sl = bid + sl_distance,
        tp = bid - tp_distance (rounded to ``req.digits``). ``problem`` is "" or a
        "QUOTE_UNREADABLE: ..." / "PRICE_GAP: ..." text: nothing may be sent then.
        """
        cur = self._find_window(dlg.wid)
        if cur is None:
            return "ORDER_DIALOG_CLOSED: the order dialog closed while it was being filled", None, None
        items, png = self._ocr(cur, tag="order_%s_quote" % req.side)
        evidence.append(png)
        quote = parse.parse_ticket_quote(items)
        if quote is None:
            return ("QUOTE_UNREADABLE: the bid / ask quote of %s was not readable in the order ticket"
                    % req.symbol), None, None
        bid, ask = quote
        ref = ask if req.side == "buy" else bid
        hint = req.price_hint
        if hint is not None and _finite(hint) and float(hint) > 0:
            max_gap = self.cfg.mirror_max_price_gap_pct(req.symbol)
            override = getattr(req, "max_price_gap_pct", None)
            if override is not None and _finite(override) and float(override) > 0:
                max_gap = max(float(max_gap), float(override))
            gap = abs(ref - float(hint)) / float(hint) * 100.0
            if gap > max_gap:
                return ("PRICE_GAP: the MT5 quote %s / %s is %.2f %% from the alert price %s (mirror."
                        "max_price_gap_pct=%s): wrong symbol or a stale alert" % (bid, ask, gap, hint, max_gap),
                        None, None)
        digits = int(req.digits)
        sign = 1.0 if req.side == "buy" else -1.0
        sl = round(ref - sign * float(req.sl_distance), digits)   # type: ignore[arg-type]
        tp = None  # type: Optional[float]
        if req.tp_distance:
            tp = round(ref + sign * float(req.tp_distance), digits)
        if sl <= 0 or (tp is not None and tp <= 0):
            return "QUOTE_UNREADABLE: quote %s / %s gives an invalid stop %s" % (bid, ask, sl), None, None
        log.info("quote %s / %s for %s %s: sl %s tp %s", bid, ask, req.side, req.symbol, sl, tp or "-")
        return "", sl, tp

    def _abort_reason(self) -> Optional[str]:
        check = self.abort_check
        if check is None:
            return None
        try:
            reason = check()
        except Exception as e:   # fail closed: an entry is never clicked on a failed check
            return "abort check failed (%s: %s)" % (type(e).__name__, e)
        return str(reason) if reason else None

    def _fill_mismatch(self, req: OrderRequest, lots_str: str, outcome: "_Outcome") -> str:
        """Why a "filled" result does not match the request ("" when it does)."""
        side = req.side
        text = outcome.text or outcome.message
        words = _side_words(text)
        if side not in words and opposite_side(side) in words:
            return "wanted %s" % side
        sv = parse.result_side_volume(text)
        want_lots = parse.parse_number(lots_str)
        if sv is not None and sv[0] == side and want_lots is not None and abs(sv[1] - want_lots) > 1e-9:
            return "wanted volume %s, result shows %s" % (lots_str, sv[1])
        found = parse.find_symbol(text, list(self.cfg.known_mt5_symbols()) + [req.symbol])
        if found is not None and found[0].lower() != (req.symbol or "").lower():
            return "wanted %s, result names %s" % (req.symbol, found[0])
        hint = req.price_hint
        if outcome.price is not None and _finite(hint) and float(hint) > 0:   # type: ignore[arg-type]
            band = PRICE_BAND_REL * float(hint)   # type: ignore[arg-type]
            spec = self.cfg.spec_for(req.symbol)
            if spec is not None:
                band = max(band, PRICE_BAND_SL_POINTS * float(spec.min_sl_points) * float(spec.point))
            if abs(float(outcome.price) - float(hint)) > band:   # type: ignore[arg-type]
                return "fill price %s is far from the alert price %s (band %.5g)" % (outcome.price, hint, band)
        return ""

    def _best_effort_escape(self, win: Any, pid: Any) -> None:
        """Escape a dialog, swallowing errors (used on exception paths; never presses Return)."""
        try:
            self._escape_window(win, pid if pid is not None else getattr(win, "pid", 0))
        except Exception as e:  # pragma: no cover - best effort
            log.warning("could not close dialog %r: %s", getattr(win, "title", "?"), e)

    # ------------------------------------------------------------------ account

    def _scan_toolbox(self, main: Any, tag: str) -> Tuple[List[Any], str, Dict[str, float]]:
        """OCR the Toolbox; select the Trade tab and retry once if the account line is missing.

        The Trade tab is only clicked while the main window has its calibrated size (the
        tab point is relative to it). Screenshots taken are listed in ``self._scan_pngs``.
        """
        region = self.calib.toolbox_region
        self._scan_pngs = []
        items, png = self._ocr(main, region, tag)
        self._scan_pngs.append(png)
        acct = parse.parse_account_line(items)
        # Native MT5 exposes the Trade tab's account line even while another tab is shown, so
        # the History tab (closed deals) would read as positions: it is detected on its own.
        history = parse.looks_like_history_tab(items)
        if (acct is None or history) and self.calib.trade_tab_point and not self._main_size_issue(main):
            # Find the "Trade" tab by reading the Toolbox tab row (bottom of the window). MT5
            # hides Trade/Exposure/History while it is not connected: then nothing is clicked.
            strip = (0.0, float(main.h) - 50.0, min(float(main.w), 900.0), 50.0)
            tab_items, _tab_png = self._ocr(main, strip, tag + "_tabs")
            try:
                os.remove(_tab_png)     # a scratch capture: not evidence
            except OSError:
                pass
            spot = None
            for it in tab_items:
                m = re.search(r"\bTrade\b", it.text)
                if m and re.search(r"\b(News|Journal|Mailbox|History|Exposure)\b", " ".join(t.text for t in tab_items)):
                    n = float(max(1, len(it.text)))
                    spot = (it.x + it.w * ((m.start() + m.end()) / 2.0) / n, it.cy)
                    break
            if spot is None:
                raise ExecutorError("MT5_DISCONNECTED",
                                    "the Toolbox has no Trade tab: MT5 is not connected to the account "
                                    "(screenshot %s)" % png)
            log.info("%s (%s); selecting the Trade tab and retrying",
                     "the Toolbox shows the History tab" if history else "account line not readable", png)
            self._focus(main)
            x, y = spot
            self.driver.click(x, y)
            self.driver.sleep(max(float(self.gui.action_delay_s), 0.3))
            items, png = self._ocr(main, region, tag + "_retry")
            self._scan_pngs.append(png)
            acct = parse.parse_account_line(items)
            history = parse.looks_like_history_tab(items)
        if acct is None:
            raise ExecutorError("ACCOUNT_UNREADABLE",
                                "Balance/Equity not readable in the MT5 Toolbox (screenshot %s)" % png)
        if history:
            raise ExecutorError("TOOLBOX_HISTORY_TAB",
                                "the MT5 Toolbox shows the History tab (closed deals), not the Trade tab, and "
                                "selecting the Trade tab did not help; select it by hand (screenshot %s)" % png)
        return items, png, acct

    def _positions(self, items: List[Any]) -> List[Tuple[ObservedPosition, Any]]:
        return list(parse.parse_position_rows(items, self.cfg.known_mt5_symbols()))

    def _toolbox_issue(self, main: Any, items: List[Any], acct: Dict[str, float],
                       rows: List[Tuple[ObservedPosition, Any]]) -> str:
        """"" if the parsed position list can be trusted as complete, else the reason.

        Checks: (a) the main window has its calibrated size; (b) the Trade-list header
        ("Symbol ... Profit") is read above the rows and the account line; (c) no gap between
        header, rows and account line is wider than a missing row would leave; (d) no rows but
        margin in use; (e) equity - balance matches the rows' profit (+ swap) within a
        tolerance for rounding, commission and unshown swap.
        """
        issue = self._main_size_issue(main)
        if issue:
            return issue
        header = parse.find_trade_header(items)
        if header is None:
            return "the Trade list header (Symbol ... Profit) is not readable"
        acct_y = parse.account_line_y(items)
        row_ys = sorted(float(a.cy) for _, a in rows)
        hy = float(header["cy"])  # type: ignore[arg-type]
        tops = row_ys + ([acct_y] if acct_y is not None else [])
        if tops and hy >= min(tops):
            return "the Trade list header is not above the position rows"
        seq = [hy] + [y for y in row_ys if acct_y is None or y < acct_y] + ([acct_y] if acct_y is not None else [])
        gaps = [b - a for a, b in zip(seq, seq[1:])]
        if len(gaps) >= 2:
            pitch = min(gaps)
            if pitch > 0 and max(gaps) > ROW_GAP_FACTOR * pitch:
                return "a gap in the Trade list (%.0f pt, rows %.0f pt apart): a row was not read" % (
                    max(gaps), pitch)
        elif len(gaps) == 1:
            text_h = float(header.get("h") or 0.0)  # type: ignore[union-attr]
            if text_h > 0 and gaps[0] > SINGLE_GAP_FACTOR * text_h:
                return "a gap between the Trade list header and the account line: a row was not read"
        margin = acct.get("margin")
        if not rows and margin is not None and _finite(margin) and float(margin) > 0.005:
            return "no position rows readable but MT5 reports margin %.2f" % float(margin)
        balance, equity = float(acct["balance"]), float(acct["equity"])
        floating = equity - balance
        if all(p.profit is not None for p, _ in rows):
            visible = sum(float(p.profit) + float(p.swap or 0.0) for p, _ in rows)   # type: ignore[arg-type]
            swap_allow = 0.0 if header.get("swap") else SWAP_ALLOWANCE_USD_PER_LOT  # type: ignore[union-attr]
            allowance = sum((self.cfg.commission_per_lot(p.symbol) + swap_allow) * float(p.lots) for p, _ in rows)
            tol = max(MONEY_TOL_USD, MONEY_TOL_REL * abs(floating)) + allowance
            if abs(floating - visible) > tol:
                return ("equity - balance is %.2f but the readable rows show %.2f: a position row is missing"
                        % (floating, visible))
        return ""

    def _scan_positions(self, main: Any, tag: str) -> Tuple[List[Tuple[ObservedPosition, Any]], str]:
        """(rows, completeness issue) from one Toolbox scan."""
        items, _png, acct = self._scan_toolbox(main, tag)
        rows = self._positions(items)
        return rows, self._toolbox_issue(main, items, acct, rows)

    def read_account(self) -> AccountSnapshot:
        with self._gui_lock:
            main = self._main_window()
            try:
                items, png, acct = self._scan_toolbox(main, "account")
            except ExecutorError as e:
                if e.code != "ACCOUNT_UNREADABLE":
                    raise
                latest = self._rotate_failed_account_shots(self._scan_pngs)
                raise ExecutorError("ACCOUNT_UNREADABLE", "Balance/Equity not readable in the MT5 Toolbox "
                                    "(screenshot %s)" % (latest or "?"))
            self._acct_fail_streak = 0
            for other in self._scan_pngs:
                if other != png:
                    _unlink(other)     # the unreadable first try before a Trade-tab click
            rows = self._positions(items)
            issue = self._toolbox_issue(main, items, acct, rows)
            self._keep_as_latest(png)
            if issue != self._last_toolbox_issue:
                if issue:
                    log.warning("Toolbox position list not trusted: %s", issue)
                elif self._last_toolbox_issue:
                    log.info("Toolbox position list is complete again")
                self._last_toolbox_issue = issue
            return AccountSnapshot(
                ts=clock.utcnow(), balance=float(acct["balance"]), equity=float(acct["equity"]),
                margin=acct.get("margin"), free_margin=acct.get("free_margin"),
                positions=None if issue else [p for p, _ in rows], source="mt5gui",
                positions_note=("TOOLBOX_INCOMPLETE: %s" % issue) if issue else "",
            )

    # ------------------------------------------------------------------ close

    def _sym_key(self, symbol: str) -> str:
        s = (symbol or "").strip()
        return (self.cfg.tv_symbol_for(s) or self.cfg.normalize_tv_symbol(s)).lower()

    def close_positions(self, symbol: str, side: Optional[str] = None) -> List[OrderResult]:
        """Close matching positions one at a time (Toolbox row -> position dialog -> Close button).

        Raises ExecutorError only if the very first step fails (nothing was touched); later
        failures are appended as "error"/"uncertain" results so earlier closes are not lost.
        When the Toolbox list cannot be verified as complete, every visible matching row is
        still closed and the results end with an "uncertain" ``TOOLBOX_INCOMPLETE`` result.
        """
        if side is not None and side not in SIDES:
            return [OrderResult("error", "BAD_REQUEST: side must be buy, sell or None, got %r" % (side,))]
        with self._gui_lock:
            results, issue = self._close_positions_locked(self._mt5_name(symbol), side)
            if issue:
                results.append(self._incomplete_result(issue, symbol))
            return results

    @staticmethod
    def _incomplete_result(issue: str, what: str) -> OrderResult:
        return OrderResult("uncertain", "TOOLBOX_INCOMPLETE: %s; positions%s may still be open (not visible in "
                                        "the Toolbox)" % (issue, " on %s" % what if what else ""))

    def _close_positions_locked(self, target: str, side: Optional[str]) -> Tuple[List[OrderResult], str]:
        results = []  # type: List[OrderResult]
        closed_tickets = set()  # type: Set[str]
        stale_scans = 0
        issue = ""
        key = self._sym_key(target)
        for _ in range(MAX_CLOSE_ITERATIONS):
            try:
                main = self._main_window()
                rows, issue = self._scan_positions(main, "close_scan")
                matches = [(p, a) for p, a in rows
                           if self._sym_key(p.symbol) == key and (side is None or p.side == side)]
                if not matches:
                    break
                # Never act twice on a ticket reported closed: the Toolbox may lag behind.
                fresh = [(p, a) for p, a in matches if not (p.ticket and p.ticket in closed_tickets)]
                if not fresh:
                    stale_scans += 1
                    if stale_scans > STALE_SCAN_RETRIES:
                        results.append(OrderResult(
                            "uncertain", "POSITION_STILL_LISTED: %s still listed after MT5 reported the close"
                            % ", ".join("#%s" % p.ticket for p, _ in matches)))
                        break
                    self.driver.sleep(STALE_SCAN_WAIT_S)
                    continue
                pos, anchor = fresh[0]
                res = self._close_one(main, pos, anchor, target, side)
                if res.status == "filled" and pos.ticket:
                    closed_tickets.add(pos.ticket)
            except Exception as e:
                if not results:
                    raise
                log.exception("close of %s interrupted", target)
                code = e.reason if isinstance(e, ExecutorError) else "GUI_ERROR: %s: %s" % (type(e).__name__, e)
                results.append(OrderResult("error", code))
                break
            results.append(res)
            if res.status != "filled":
                break
        else:
            log.warning("close_positions(%s, %s) stopped after %d iterations", target, side, MAX_CLOSE_ITERATIONS)
        return results, issue

    def close_partial(self, symbol: str, side: str, lots: float) -> List[OrderResult]:
        """Close ``lots`` of the position(s) on ``symbol``/``side``, largest position first.

        A position the remaining amount covers (within ``mirror.size_tolerance_lots``) is closed
        whole through the normal close path; otherwise the volume is typed into the position
        dialog and the close button is clicked only after it reads back that volume and the
        ticket (see :meth:`_close_one`). Stops at the first result that is not "filled".
        """
        if side not in SIDES:
            return [OrderResult("error", "BAD_REQUEST: side must be buy or sell, got %r" % (side,))]
        if not _finite(lots) or float(lots) <= 0:
            return [OrderResult("error", "BAD_REQUEST: lots must be > 0, got %r" % (lots,))]
        with self._gui_lock:
            results, issue = self._close_partial_locked(self._mt5_name(symbol), side, float(lots))
            if issue:
                results.append(self._incomplete_result(issue, symbol))
            return results

    def _close_partial_locked(self, target: str, side: str, lots: float) -> Tuple[List[OrderResult], str]:
        results = []  # type: List[OrderResult]
        closed_tickets = set()  # type: Set[str]
        stale_scans = 0
        issue = ""
        key = self._sym_key(target)
        tol = float(self.cfg.mirror.size_tolerance_lots)
        remaining = float(lots)
        for _ in range(MAX_CLOSE_ITERATIONS):
            if remaining <= max(tol, LOTS_EPS):
                break
            try:
                main = self._main_window()
                rows, issue = self._scan_positions(main, "close_scan")
                matches = [(p, a) for p, a in rows if self._sym_key(p.symbol) == key and p.side == side]
                if not matches:
                    break
                fresh = [(p, a) for p, a in matches if not (p.ticket and p.ticket in closed_tickets)]
                if not fresh:
                    stale_scans += 1
                    if stale_scans > STALE_SCAN_RETRIES:
                        results.append(OrderResult(
                            "uncertain", "POSITION_STILL_LISTED: %s still listed after MT5 reported the close"
                            % ", ".join("#%s" % p.ticket for p, _ in matches)))
                        break
                    self.driver.sleep(STALE_SCAN_WAIT_S)
                    continue
                pos, anchor = max(fresh, key=lambda pa: float(pa[0].lots))
                whole = remaining >= float(pos.lots) - tol
                if whole:
                    res = self._close_one(main, pos, anchor, target, side)
                else:
                    spec = self.cfg.spec_for(target)
                    decimals = spec.lot_decimals if spec is not None else 2
                    res = self._close_one(main, pos, anchor, target, side,
                                          partial_lots=round(remaining, decimals), lot_decimals=decimals)
                if res.status == "filled":
                    if whole:
                        remaining = round(remaining - float(pos.lots), 8)
                        if pos.ticket:
                            closed_tickets.add(pos.ticket)
                    else:
                        remaining = 0.0
            except Exception as e:
                if not results:
                    raise
                log.exception("partial close of %s interrupted", target)
                code = e.reason if isinstance(e, ExecutorError) else "GUI_ERROR: %s: %s" % (type(e).__name__, e)
                results.append(OrderResult("error", code))
                break
            results.append(res)
            if res.status != "filled":
                break
        else:
            log.warning("close_partial(%s, %s) stopped after %d iterations", target, side, MAX_CLOSE_ITERATIONS)
        return results, issue

    def _volume_field_point(self, items: List[Any]) -> Optional[Tuple[float, float]]:
        """Where to click the position dialog's volume field: ``volume_field_offset_px`` right of
        the right edge of the "Volume" label, level with it. None if the label was not read."""
        best = None  # type: Optional[Tuple[float, float, float]]
        for it in items:
            text = it.text or ""
            m = _VOLUME_LABEL_RE.search(text)
            if m is None or "close" in text.lower():
                continue
            # the label's own right edge (an observation may also hold the value: "Volume: 0.28")
            frac = float(m.end()) / float(max(1, len(text)))
            right = float(it.x) + float(it.w) * frac
            cand = (float(m.start()), right, float(it.cy))
            if best is None or cand[0] < best[0]:
                best = cand
        if best is None:
            return None
        return best[1] + float(self.gui.volume_field_offset_px), best[2]

    def _ticket_lots(self, ticket: str) -> Optional[float]:
        """Lots the Toolbox lists for ``ticket`` (0.0 if it is not listed); None if the list
        cannot be read or verified as complete."""
        try:
            main = self._main_window()
            items, _png = self._ocr(main, self.calib.toolbox_region, "close_check")
        except Exception as e:
            log.warning("could not re-read the Toolbox after a partial close: %s", e)
            return None
        acct = parse.parse_account_line(items)
        if acct is None:
            return None
        rows = self._positions(items)
        if self._toolbox_issue(main, items, acct, rows):
            return None
        for p, _ in rows:
            if p.ticket == str(ticket):
                return float(p.lots)
        return 0.0

    def _find_close_button(self, items: List[Any], target: str, pos: ObservedPosition,
                           side: Optional[str]) -> Tuple[Optional[Any], str, str]:
        """(item to click, its text, problem). The item must mention "close" and the symbol."""
        syms = {s.lower() for s in (target, pos.symbol) if s}

        def _has_sym(low: str) -> bool:
            # whole symbol only: "xauusd" must not match inside "xauusdmicro"
            return any(re.search(r"(?<![a-z0-9])%s(?![a-z0-9._])" % re.escape(s), low) for s in syms)

        btn, text = None, ""
        for it in items:
            low = it.text.lower()
            if "close" in low and _has_sym(low):
                btn, text = it, it.text
                break
        if btn is None:
            # OCR sometimes splits the button text; accept a row that has both parts.
            for row in parse.group_rows(list(items)):
                joined = " ".join(i.text for i in row)
                if "close" in joined.lower() and _has_sym(joined.lower()):
                    closers = [i for i in row if "close" in i.text.lower()]
                    if closers:
                        btn, text = closers[0], joined
                        break
        if btn is None:
            return None, "", "no 'Close ... %s' button readable in the position dialog" % target
        m = _TICKET_RE.search(text)
        if pos.ticket and m and m.group(1) != str(pos.ticket):
            return None, text, "button %r is for ticket #%s, expected #%s" % (text, m.group(1), pos.ticket)
        if side is not None:
            words = _side_words(text)
            if side not in words and opposite_side(side) in words:
                return None, text, "button %r is for a %s position, expected %s" % (
                    text, opposite_side(side), side)
        return btn, text, ""

    def _ticket_listed(self, ticket: str) -> Optional[bool]:
        """True/False if the Toolbox does/doesn't list ``ticket``; None if it can't be read or
        the list cannot be verified as complete."""
        try:
            main = self._main_window()
            items, _png = self._ocr(main, self.calib.toolbox_region, "close_check")
        except Exception as e:
            log.warning("could not re-read the Toolbox after a close: %s", e)
            return None
        acct = parse.parse_account_line(items)
        if acct is None:
            return None
        rows = self._positions(items)
        if self._toolbox_issue(main, items, acct, rows):
            return None
        return any(p.ticket == str(ticket) for p, _ in rows)

    def _close_one(self, main: Any, pos: ObservedPosition, anchor: Any, target: str,
                   side: Optional[str], partial_lots: Optional[float] = None,
                   lot_decimals: int = 2) -> OrderResult:
        """Close one position row; with ``partial_lots`` only that volume of it.

        Partial: before the close button may be clicked, the volume is typed into the dialog's
        volume field and the button must read back that volume and the position's ticket;
        otherwise Escape and ``PARTIAL_VERIFY_FAILED`` (nothing sent).
        """
        gui = self.gui
        desc = "%s %s %s%s" % (pos.side, pos.lots, pos.symbol, " #%s" % pos.ticket if pos.ticket else "")
        partial = partial_lots is not None
        done_lots = float(partial_lots) if partial else pos.lots   # type: ignore[arg-type]
        if partial:
            desc = "%s of %s" % ("%.*f" % (int(lot_decimals), done_lots), desc)
        # Exits fail open: a window that Escape cannot close does not stop the close, but
        # nothing is ever clicked inside it.
        strays = self._strays_for_exit(main)
        for w in strays:
            if self._covers(w, anchor.cx, anchor.cy):
                return OrderResult("error", "STRAY_DIALOG: MT5 window %r covers the %s row; nothing was sent"
                                   % (w.title, desc), ticket=pos.ticket)
        fx, fy = self._point(main, self.calib.focus_point)
        if any(self._covers(w, fx, fy) for w in strays):
            self.driver.activate(main.pid)          # no focus click inside a stray window
            self.driver.sleep(self.gui.action_delay_s)
        else:
            self._focus(main)
        before = {w.wid for w in self._windows()}
        self.driver.click(anchor.cx, anchor.cy, count=2)
        dlg = self._wait_for_new_window(main.pid, before, gui.position_dialog_title_contains,
                                        gui.dialog_timeout_s)
        if dlg is None:
            return OrderResult("error", "POSITION_DIALOG_NOT_OPENED: double-clicking %s did not open a dialog "
                                        "titled %s" % (desc, list(gui.position_dialog_title_contains)))
        evidence = []  # type: List[str]
        clicked = False
        try:
            items, png = self._ocr(dlg, tag="close_dialog")
            evidence.append(png)
            btn, btn_text, problem = self._find_close_button(items, target, pos, side)
            if btn is None:
                self._escape_window(dlg, main.pid)
                return OrderResult("error", "CLOSE_BUTTON_NOT_FOUND: %s; nothing was sent" % problem,
                                   ticket=pos.ticket, evidence=evidence)
            if partial:
                problem, items2, btn2, text2 = self._set_partial_volume(dlg, items, target, pos, side, done_lots,
                                                                        int(lot_decimals), evidence)
                if problem:
                    cur = self._find_window(dlg.wid)
                    closed = True if cur is None else self._escape_window(cur, main.pid)
                    msg = "PARTIAL_VERIFY_FAILED: %s; pressed Escape, nothing was sent" % problem
                    if not closed:
                        msg += " (position dialog did not close)"
                    log.warning(msg)
                    return OrderResult("error", msg, ticket=pos.ticket, evidence=evidence)
                items, btn, btn_text = items2, btn2, text2
            if self.rehearsal:
                closed = self._escape_window(dlg, main.pid)
                msg = "REHEARSED: found %r for %s; pressed Escape, nothing was sent" % (btn_text, desc)
                if not closed:
                    msg += " (position dialog did not close)"
                return OrderResult("rehearsed", msg, ticket=pos.ticket, lots=done_lots, evidence=evidence)

            cur = self._find_window(dlg.wid)
            if cur is None or abs(cur.x - dlg.x) > MOVE_TOLERANCE or abs(cur.y - dlg.y) > MOVE_TOLERANCE:
                if cur is not None:
                    self._escape_window(cur, main.pid)
                return OrderResult("error", "DIALOG_MOVED: the position dialog moved or closed before the "
                                            "click; nothing was sent", ticket=pos.ticket, evidence=evidence)

            baseline = _text_set(items)
            log.info("LIVE: clicking %r to close %s", btn_text, desc)
            clicked = True
            self.driver.click(btn.cx, btn.cy)
            outcome = self._await_result(dlg, baseline, "close_result", evidence)
            status, message = outcome.status, outcome.message
            if not outcome.vanished:
                closed = self._escape_window(dlg, main.pid, allow_return=status in ("filled", "rejected"))
                if not closed:
                    message += " (result dialog did not close)"
            if partial and status == "filled":
                sv = parse.result_side_volume(outcome.text or outcome.message)
                if sv is not None and abs(sv[1] - done_lots) > LOTS_EPS:
                    status = "uncertain"
                    message = "RESULT_MISMATCH: wanted to close %s, MT5 reported %r" % (done_lots, outcome.message)
            elif partial and status == "uncertain" and pos.ticket:
                # the analogue of "the row is gone" for a partial close: the row shows the rest
                left = self._ticket_lots(pos.ticket)
                if left is not None and abs(left - (float(pos.lots) - done_lots)) <= LOTS_EPS:
                    status = "filled"
                    message = "CLOSED: #%s now lists %s lots in the Toolbox (%s)" % (pos.ticket, left, message)
            elif status == "uncertain" and pos.ticket:
                listed = self._ticket_listed(pos.ticket)
                if listed is False:
                    status = "filled"
                    message = "CLOSED: #%s is no longer listed in the Toolbox (%s)" % (pos.ticket, message)
            if status == "filled":
                log.info("CLOSED %s: %s", desc, message)
                ticket = (pos.ticket or outcome.ticket) if partial else (outcome.ticket or pos.ticket)
                return OrderResult("filled", message, fill_price=outcome.price,
                                   ticket=ticket, lots=done_lots, evidence=evidence)
            if status == "rejected":
                log.warning("close of %s rejected: %s", desc, message)
                return OrderResult("rejected", "REJECTED: " + message, ticket=pos.ticket, evidence=evidence)
            if not message.startswith("RESULT_MISMATCH"):
                message = "UNCERTAIN_EXECUTION: clicked close for %s; %s" % (desc, message)
            log.error(message)
            return OrderResult("uncertain", message, ticket=pos.ticket, lots=done_lots, evidence=evidence)
        except Exception as e:
            if clicked:
                log.exception("exception after clicking close for %s", desc)
                self._best_effort_escape(dlg, main.pid)
                return OrderResult("uncertain", "UNCERTAIN_EXECUTION: %s after clicking close for %s: %s"
                                   % (type(e).__name__, desc, e), ticket=pos.ticket, lots=done_lots,
                                   evidence=evidence)
            self._best_effort_escape(dlg, main.pid)
            raise

    def _set_partial_volume(self, dlg: Any, items: List[Any], target: str, pos: ObservedPosition,
                            side: Optional[str], lots: float, lot_decimals: int, evidence: List[str]
                            ) -> Tuple[str, List[Any], Any, str]:
        """Type ``lots`` into the position dialog's volume field and read the close button back.

        Returns (problem, items of the second OCR, close button item, its text); ``problem`` is
        "" only when the button now names exactly that volume and the position's ticket.
        """
        lots_str = "%.*f" % (int(lot_decimals), float(lots))
        point = self._volume_field_point(items)
        if point is None:
            return "no 'Volume' label readable in the position dialog", items, None, ""
        if not (dlg.x <= point[0] <= dlg.x + dlg.w and dlg.y <= point[1] <= dlg.y + dlg.h):
            return "the volume field point (%.0f, %.0f) is outside the position dialog" % point, items, None, ""
        self._set_field(point[0], point[1], lots_str)
        cur = self._find_window(dlg.wid)
        if cur is None:
            return "the position dialog closed while the volume was typed", items, None, ""
        items2, png = self._ocr(cur, tag="close_partial_verify")
        evidence.append(png)
        btn, text, problem = self._find_close_button(items2, target, pos, side)
        if btn is None:
            return "after typing volume %s: %s" % (lots_str, problem), items2, None, text
        tokens = parse.scan_numbers(text, grouped=False)
        if not any(abs(v - float(lots)) <= LOTS_EPS for v in tokens):
            return ("typed volume %s but the close button reads %r" % (lots_str, text)), items2, None, text
        if pos.ticket and not re.search(r"(?<!\d)" + re.escape(str(pos.ticket)) + r"(?!\d)", text):
            return ("the close button %r does not name ticket #%s" % (text, pos.ticket)), items2, None, text
        return "", items2, btn, text

    def close_all(self) -> List[OrderResult]:
        """Close every listed position, symbol by symbol (continues past failures).

        Positions on symbols without a spec are closed too. If the Toolbox list cannot be
        verified as complete at the end, an "uncertain" ``TOOLBOX_INCOMPLETE`` result is
        appended (never ``[]``), so the caller does not mistake hidden rows for "flat".
        """
        with self._gui_lock:
            main = self._main_window()
            rows, issue = self._scan_positions(main, "close_all_scan")
            symbols = []  # type: List[str]
            keys = set()  # type: Set[str]
            for p, _ in rows:
                k = self._sym_key(p.symbol)
                if k not in keys:
                    keys.add(k)
                    symbols.append(p.symbol)
            results = []  # type: List[OrderResult]
            for sym in symbols:
                try:
                    res, issue = self._close_positions_locked(self._mt5_name(sym), None)
                    results.extend(res)
                except Exception as e:
                    log.exception("close_all: closing %s failed", sym)
                    code = e.reason if isinstance(e, ExecutorError) else "GUI_ERROR: %s: %s" % (
                        type(e).__name__, e)
                    results.append(OrderResult("error", "%s (%s)" % (code, sym)))
            if issue:
                results.append(self._incomplete_result(issue, ""))
            return results

    # ------------------------------------------------------------------ health

    def health(self) -> Dict[str, Any]:
        """``{"ok", "detail", "mode"}``: MT5 main window found and driver permissions granted."""
        ok = True
        parts = []  # type: List[str]
        try:
            perms = dict(self.driver.permissions() or {})
        except Exception as e:
            perms = None
            ok = False
            parts.append("permissions unknown (%s)" % e)
        if perms is not None:
            for k in ("accessibility", "screen_recording"):
                if not perms.get(k):
                    ok = False
                    parts.append("%s permission missing" % k.replace("_", " "))
        try:
            main = self._main_window()
            parts.append("MT5 window %r (%.0fx%.0f)" % (main.title, main.w, main.h))
            issue = self._main_size_issue(main)
            if issue:
                ok = False
                parts.append("MAIN_LAYOUT_CHANGED: %s" % issue)
        except ExecutorError as e:
            ok = False
            parts.append(e.reason)
        except Exception as e:
            ok = False
            parts.append("window list failed: %s" % e)
        parts.append("mode %s" % self.mode)
        return {"ok": ok, "detail": "; ".join(parts), "mode": self.mode}
