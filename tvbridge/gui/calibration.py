"""Calibration: where the fields and buttons are in the MT5 windows.

Saved as ``$TVBRIDGE_HOME/calibration.json`` (mode 600). All points are window-relative
points (``[dx, dy]`` from the window's top-left corner), so moving a window is fine;
resizing it is not (the executor checks the order window size against ``order_dialog``).

The interactive wizard (:func:`run_wizard`) never clicks or types anything: the human
opens and closes the order window and only *hovers* the mouse; the wizard reads the mouse
position, takes screenshots and runs OCR.
"""

import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .. import clock
from . import parse
from .driver import Driver, Window

log = logging.getLogger("tvbridge.gui.calibration")

CALIBRATION_VERSION = 1
#: Order-window points, in the order the wizard asks for them.
POINT_NAMES = ("symbol", "volume", "sl", "tp", "sell", "buy")
#: The executor only accepts a main window at least this big; the wizard uses the same rule.
MIN_MAIN_W = 600.0
MIN_MAIN_H = 400.0
#: Same-process windows smaller than this are invisible Wine helpers, not dialogs (as in the executor).
MIN_DIALOG_SIDE = 20.0
#: Same distance the executor's button guard uses.
BUTTON_LABEL_MAX_DIST = 80.0
#: The focus point must be in the title bar strip: clicking anywhere lower could hit a chart
#: (and MT5's one-click trading buttons live on charts).
FOCUS_MAX_DY = 40.0
#: Order-window field labels: each field point must sit right of its own label.
FIELD_LABELS = (("volume", "Volume", re.compile(r"\bvolume\b", re.I)),
                ("sl", "Stop Loss", re.compile(r"\bstop\s*loss\b", re.I)),
                ("tp", "Take Profit", re.compile(r"\btake\s*profit\b", re.I)))
HOVER_SECONDS = 3
MAX_TRIES = 3
DIALOG_WAIT_S = 5.0
POLL_S = 0.25
_EDGE_TOL = 0.5

PathLike = Union[str, Path]


class CalibrationError(Exception):
    """Missing or invalid calibration, or a wizard step that could not be completed."""


def _norm(s: Optional[str]) -> str:
    """Lower-case with whitespace collapsed (how the executor compares window titles)."""
    return " ".join((s or "").split()).lower()


# --------------------------------------------------------------------------- data


def _num(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise CalibrationError("%s must be a number (got %s)" % (where, json.dumps(v)))
    return float(v)


def _numbers(v: Any, n: int, where: str) -> List[float]:
    if not isinstance(v, (list, tuple)) or len(v) != n:
        raise CalibrationError("%s must be a list of %d numbers (got %s)" % (where, n, json.dumps(v)))
    return [_num(x, "%s[%d]" % (where, i)) for i, x in enumerate(v)]


def _inside(pt: List[float], w: float, h: float) -> bool:
    return -_EDGE_TOL <= pt[0] <= w + _EDGE_TOL and -_EDGE_TOL <= pt[1] <= h + _EDGE_TOL


def _window_info(v: Any, where: str) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise CalibrationError("%s must be an object" % where)
    title = v.get("title", "")
    if not isinstance(title, str):
        raise CalibrationError("%s.title must be a string" % where)
    out = dict(v)
    out["title"] = title
    for k in ("w", "h"):
        out[k] = _num(v.get(k), "%s.%s" % (where, k))
        if out[k] <= 0:
            raise CalibrationError("%s.%s must be > 0" % (where, k))
    return out


@dataclass
class Calibration:
    """Window geometry recorded by the wizard. All points are window-relative."""

    version: int = CALIBRATION_VERSION
    created_at: str = ""
    main_window: Dict[str, Any] = field(default_factory=dict)    # {"title", "w", "h"}
    order_dialog: Dict[str, Any] = field(default_factory=dict)   # {"title", "w", "h", "points": {name: [dx, dy]}}
    toolbox_region: List[float] = field(default_factory=list)    # [dx, dy, w, h] in the main window
    focus_point: List[float] = field(default_factory=list)       # [dx, dy] in the main window (title bar)
    trade_tab_point: Optional[List[float]] = None                # [dx, dy] in the main window

    def dialog_point(self, name: str) -> Tuple[float, float]:
        """``(dx, dy)`` of an order-window point ("symbol", "volume", "sl", "tp", "buy", "sell")."""
        p = self.order_dialog["points"][name]
        return float(p[0]), float(p[1])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "main_window": dict(self.main_window),
            "order_dialog": {
                **{k: v for k, v in self.order_dialog.items() if k != "points"},
                "points": {k: list(v) for k, v in (self.order_dialog.get("points") or {}).items()},
            },
            "toolbox_region": list(self.toolbox_region),
            "focus_point": list(self.focus_point),
            "trade_tab_point": None if self.trade_tab_point is None else list(self.trade_tab_point),
        }

    @staticmethod
    def from_dict(d: Any) -> "Calibration":
        """Build and validate. Raises :class:`CalibrationError` with a precise message."""
        if not isinstance(d, dict):
            raise CalibrationError("calibration must be a JSON object")
        version = d.get("version", CALIBRATION_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise CalibrationError("version must be an integer (got %s)" % json.dumps(version))
        if version != CALIBRATION_VERSION:
            raise CalibrationError("unsupported calibration version %d (this tvbridge reads version %d)"
                                   % (version, CALIBRATION_VERSION))
        created_at = d.get("created_at", "")
        if not isinstance(created_at, str):
            raise CalibrationError("created_at must be a string")
        for key in ("main_window", "order_dialog", "toolbox_region", "focus_point"):
            if key not in d:
                raise CalibrationError("%s is missing" % key)

        main = _window_info(d["main_window"], "main_window")
        dialog = _window_info(d["order_dialog"], "order_dialog")
        pts = d["order_dialog"].get("points")
        if not isinstance(pts, dict):
            raise CalibrationError("order_dialog.points must be an object with %s" % ", ".join(POINT_NAMES))
        points = {}  # type: Dict[str, List[float]]
        for name in POINT_NAMES:
            if name not in pts:
                raise CalibrationError("order_dialog.points.%s is missing" % name)
            points[name] = _numbers(pts[name], 2, "order_dialog.points.%s" % name)
        dialog["points"] = points

        calib = Calibration(
            version=version,
            created_at=created_at,
            main_window=main,
            order_dialog=dialog,
            toolbox_region=_numbers(d["toolbox_region"], 4, "toolbox_region"),
            focus_point=_numbers(d["focus_point"], 2, "focus_point"),
            trade_tab_point=(None if d.get("trade_tab_point") is None
                             else _numbers(d["trade_tab_point"], 2, "trade_tab_point")),
        )
        calib.validate()
        return calib

    def validate(self) -> None:
        """Geometric sanity checks. Raises :class:`CalibrationError`."""
        main = _window_info(self.main_window, "main_window")
        dialog = _window_info(self.order_dialog, "order_dialog")
        pts = self.order_dialog.get("points")
        if not isinstance(pts, dict):
            raise CalibrationError("order_dialog.points must be an object with %s" % ", ".join(POINT_NAMES))
        points = {}
        for name in POINT_NAMES:
            if name not in pts:
                raise CalibrationError("order_dialog.points.%s is missing" % name)
            p = _numbers(pts[name], 2, "order_dialog.points.%s" % name)
            if not _inside(p, dialog["w"], dialog["h"]):
                raise CalibrationError("order_dialog.points.%s %s lies outside the %gx%g order window"
                                       % (name, p, dialog["w"], dialog["h"]))
            points[name] = p
        if math.hypot(points["buy"][0] - points["sell"][0], points["buy"][1] - points["sell"][1]) < 5:
            raise CalibrationError("order_dialog.points.buy and .sell are the same spot")

        region = _numbers(self.toolbox_region, 4, "toolbox_region")
        if region[2] <= 0 or region[3] <= 0:
            raise CalibrationError("toolbox_region width and height must be > 0 (got %s)" % region)
        if not (_inside(region[:2], main["w"], main["h"])
                and _inside([region[0] + region[2], region[1] + region[3]], main["w"], main["h"])):
            raise CalibrationError("toolbox_region %s lies outside the %gx%g main window"
                                   % (region, main["w"], main["h"]))
        focus = _numbers(self.focus_point, 2, "focus_point")
        if not _inside(focus, main["w"], main["h"]):
            raise CalibrationError("focus_point %s lies outside the main window" % focus)
        if self.trade_tab_point is not None:
            tab = _numbers(self.trade_tab_point, 2, "trade_tab_point")
            if not _inside(tab, main["w"], main["h"]):
                raise CalibrationError("trade_tab_point %s lies outside the main window" % tab)


# --------------------------------------------------------------------------- load / save


def load_calibration(path: PathLike) -> Calibration:
    """Read and validate ``calibration.json``. Raises :class:`CalibrationError`."""
    p = Path(path).expanduser()
    if not p.exists():
        raise CalibrationError("calibration missing: %s not found -- run `tvbridge calibrate` "
                               "(on a DEMO account) to create it" % p)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise CalibrationError("cannot read calibration file %s: %s" % (p, e))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CalibrationError("invalid calibration file %s: not valid JSON (%s) -- run "
                               "`tvbridge calibrate` again" % (p, e))
    try:
        return Calibration.from_dict(data)
    except CalibrationError as e:
        raise CalibrationError("invalid calibration file %s: %s -- run `tvbridge calibrate` again" % (p, e))


def save_calibration(path: PathLike, calib: Calibration) -> None:
    """Validate and write ``calib`` atomically with permissions 600."""
    calib.validate()
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(calib.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".calibration-", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(str(p), 0o600)


# --------------------------------------------------------------------------- wizard

_DIALOG_POINT_PROMPTS = {
    "symbol": "the Symbol box at the top of the New Order window",
    "volume": "the Volume field",
    "sl": "the Stop Loss field",
    "tp": "the Take Profit field",
    "sell": "the 'Sell by Market' button (hover only -- do NOT click)",
    "buy": "the 'Buy by Market' button (hover only -- do NOT click)",
}


class _Wizard:
    def __init__(self, driver: Driver, cfg: Any, path: PathLike,
                 input_fn: Callable[[str], str], print_fn: Callable[..., Any]) -> None:
        self.driver = driver
        self.cfg = cfg
        self.gui = cfg.executor.gui
        self.path = Path(path).expanduser()
        self.input_fn = input_fn
        self.out = print_fn
        self.stamp = clock.utcnow().strftime("%Y%m%d_%H%M%S")
        self.shots = Path(cfg.shots_dir) / "calibration"

    # ---- interaction helpers

    def ask(self, prompt: str) -> str:
        try:
            answer = self.input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            raise CalibrationError("calibration aborted")
        answer = (answer or "").strip()
        if answer.lower() in ("q", "quit", "abort"):
            raise CalibrationError("calibration aborted by user")
        return answer

    def hover(self, what: str, ask: bool = True) -> Tuple[float, float]:
        if ask:
            self.ask("Press Enter, then within %d s hover the mouse over %s: " % (HOVER_SECONDS, what))
        for i in range(HOVER_SECONDS, 0, -1):
            self.out("  %d..." % i)
            self.driver.sleep(1.0)
        x, y = self.driver.mouse_location()
        self.out("  mouse at (%.0f, %.0f)" % (x, y))
        return float(x), float(y)

    def windows(self) -> List[Window]:
        return list(self.driver.list_windows(list(self.gui.owner_names)))

    def window_by_id(self, wid: int) -> Optional[Window]:
        for w in self.windows():
            if w.wid == wid:
                return w
        return None

    def point_in(self, wid: int, label: str, what: str, ask: bool = True,
                 max_dy: Optional[float] = None) -> List[float]:
        """Hover-capture a point inside window ``wid``; returns ``[dx, dy]`` relative to it."""
        for attempt in range(1, MAX_TRIES + 1):
            x, y = self.hover(what, ask=ask or attempt > 1)
            win = self.window_by_id(wid)
            if win is None:
                raise CalibrationError("the %s disappeared while calibrating; start again" % label)
            dx, dy = x - win.x, y - win.y
            if not (0 <= dx <= win.w and 0 <= dy <= win.h):
                self.out("  That spot is outside the %s. Try again (%d of %d)." % (label, attempt, MAX_TRIES))
                continue
            if max_dy is not None and dy > max_dy:
                self.out("  That spot is %.0f pt below the top of the %s; it must be in the title bar "
                         "(top %.0f pt). Try again (%d of %d)." % (dy, label, max_dy, attempt, MAX_TRIES))
                continue
            return [round(dx, 1), round(dy, 1)]
        raise CalibrationError("could not record %s inside the %s after %d tries" % (what, label, MAX_TRIES))

    def title_matches(self, win: Window, needles: List[str]) -> bool:
        t = _norm(win.title)
        return any(_norm(n) and _norm(n) in t for n in needles)

    def shot_path(self, tag: str) -> str:
        return str(self.shots / ("%s_%s.png" % (self.stamp, tag)))

    def ocr_window(self, win: Window, tag: str,
                   region: Optional[Tuple[float, float, float, float]] = None) -> List[Any]:
        png = self.shot_path(tag)
        try:
            scale = self.driver.capture(win, png)
            items = self.driver.ocr(png, win, scale, region)
        except RuntimeError as e:
            raise CalibrationError("screenshot/OCR failed: %s" % e)
        return [i for i in items if float(i.conf) >= float(self.gui.ocr_min_confidence)]

    # ---- steps

    def intro(self) -> None:
        self.out("tvbridge calibration")
        self.out("=" * 20)
        self.out("Use a DEMO account in MetaTrader 5 for this.")
        self.out("This wizard never clicks or types in MetaTrader 5: you open and close the order")
        self.out("window yourself and only HOVER the mouse when asked.")
        self.out("Do NOT click Buy or Sell at any point during calibration.")
        self.out("Type q and press Enter at any prompt to abort.")
        self.ask("Press Enter to start: ")

    def check_permissions(self) -> None:
        perms = self.driver.permissions()
        self.out("Permissions: Screen Recording %s, Accessibility %s"
                 % ("OK" if perms.get("screen_recording") else "MISSING",
                    "OK" if perms.get("accessibility") else "MISSING"))
        if not perms.get("screen_recording"):
            raise CalibrationError(
                "Screen Recording permission is missing for this Python binary; it is needed to read "
                "window titles and the screen. Run `tvbridge doctor --prompt`, grant it in System "
                "Settings > Privacy & Security > Screen Recording, then run `tvbridge calibrate` again.")
        if not perms.get("accessibility"):
            self.out("WARNING: Accessibility permission is missing. Calibration works without it, but "
                     "tvbridge cannot click or type in rehearsal/live mode until you grant it.")

    def find_main(self) -> Window:
        """The main window, chosen like the executor does (account filter first, never a guess)."""
        wins = self.windows()
        if not wins:
            raise CalibrationError("no MetaTrader 5 window found (looked for owners %s). Start MT5, log "
                                   "into a DEMO account and try again." % ", ".join(self.gui.owner_names))
        needle = _norm(self.gui.main_title_contains)
        cands = [w for w in wins if w.w >= MIN_MAIN_W and w.h >= MIN_MAIN_H
                 and (not needle or needle in _norm(w.title))]
        if not cands:
            sizes = ", ".join("%r %gx%g" % (w.title, w.w, w.h) for w in wins)
            raise CalibrationError("no MetaTrader 5 main window of at least %gx%g%s (found: %s)"
                                   % (MIN_MAIN_W, MIN_MAIN_H,
                                      " with %r in the title" % self.gui.main_title_contains if needle else "",
                                      sizes))
        acct = self.cfg.account
        login = (acct.account_login or "").strip()
        server = (acct.server_name or "").strip()
        matches = [w for w in cands
                   if (not login or re.search(r"(?<!\d)" + re.escape(login) + r"(?!\d)", w.title or ""))
                   and (not server or server.lower() in (w.title or "").lower())]
        if (login or server) and not matches:
            self.out("WARNING: no MetaTrader 5 window title contains the configured account_login/server_name "
                     "(%s / %s). tvbridge will refuse to trade (WRONG_ACCOUNT) until they match the window "
                     "you use; while you rehearse on a demo, set them to the demo's values."
                     % (login or "-", server or "-"))
        pool = matches or cands
        if len(pool) > 1:
            raise CalibrationError(
                "several windows could be the MetaTrader 5 main window (%s). Close the others (MetaEditor, "
                "MT4, a second terminal) or set account.account_login, then run `tvbridge calibrate` again."
                % ", ".join("%r %gx%g" % (w.title, w.w, w.h) for w in pool))
        main = pool[0]
        self.out("Main window: %r (%s), %gx%g at (%g, %g)" % (main.title, main.owner, main.w, main.h, main.x, main.y))
        for value, what in ((acct.account_login, "account login"), (acct.server_name, "server name")):
            if value and value in (main.title or ""):
                self.out("NOTE: the window title contains your configured %s (%s). Calibration never "
                         "clicks, but do your rehearsals on a DEMO account." % (what, value))
        strays = [w for w in wins if w.pid == main.pid and w.wid != main.wid
                  and w.w >= MIN_DIALOG_SIDE and w.h >= MIN_DIALOG_SIDE]
        if strays:
            self.out("WARNING: MT5 has other windows open (%s). Close them now. Before every order "
                     "tvbridge presses Escape on extra MT5 windows and refuses to trade if any stay open."
                     % ", ".join(repr(w.title) for w in strays))
        return main

    def others(self, main: Window) -> List[Window]:
        return [w for w in self.windows() if w.pid == main.pid and w.wid != main.wid
                and w.w >= MIN_DIALOG_SIDE and w.h >= MIN_DIALOG_SIDE]

    def wait_for_dialog(self, main: Window, before: set) -> Optional[Window]:
        needles = list(self.gui.order_dialog_title_contains)
        polls = max(1, int(round(DIALOG_WAIT_S / POLL_S)))
        for _ in range(polls):
            new = [w for w in self.others(main) if w.wid not in before]
            if new:
                titled = [w for w in new if self.title_matches(w, needles)]
                return max(titled or new, key=lambda w: w.area)
            self.driver.sleep(POLL_S)
        titled = [w for w in self.others(main) if self.title_matches(w, needles)]
        return max(titled, key=lambda w: w.area) if titled else None

    def open_dialog(self, main: Window) -> Window:
        before = {w.wid for w in self.others(main)}
        for attempt in range(1, MAX_TRIES + 1):
            self.ask("In MetaTrader 5 press F9 to open the New Order window (do not change anything in "
                     "it), then come back here and press Enter: ")
            dialog = self.wait_for_dialog(main, before)
            if dialog is not None:
                break
            self.out("  No new MetaTrader 5 window appeared. Try again (%d of %d)." % (attempt, MAX_TRIES))
        else:
            raise CalibrationError("the New Order window was not detected; make sure F9 opens it in MT5")
        self.out("Order window: %r, %gx%g" % (dialog.title, dialog.w, dialog.h))
        if not self.title_matches(dialog, list(self.gui.order_dialog_title_contains)):
            self.out("WARNING: the order window title %r contains none of executor.gui."
                     "order_dialog_title_contains %s. tvbridge will not recognise this window until you "
                     "add a word from its title to that setting." % (dialog.title, self.gui.order_dialog_title_contains))
        return dialog

    def record_dialog_points(self, dialog: Window) -> Dict[str, List[float]]:
        points = {}  # type: Dict[str, List[float]]
        for name in POINT_NAMES:
            points[name] = self.point_in(dialog.wid, "New Order window", _DIALOG_POINT_PROMPTS[name])
        names = list(points)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if math.hypot(points[a][0] - points[b][0], points[a][1] - points[b][1]) < 3:
                    raise CalibrationError("the %s and %s points are the same spot -- did the mouse move? "
                                           "Run `tvbridge calibrate` again." % (a, b))
        now = self.window_by_id(dialog.wid)
        if now is None:
            raise CalibrationError("the New Order window closed during calibration; start again")
        if abs(now.w - dialog.w) > 1 or abs(now.h - dialog.h) > 1:
            raise CalibrationError("the New Order window changed size during calibration; start again")
        return points

    def check_buttons(self, dialog: Window, points: Dict[str, List[float]]) -> None:
        """OCR the order window: the Buy/Sell points must sit on the matching labels (the
        same guard the executor applies before every click)."""
        win = self.window_by_id(dialog.wid) or dialog
        items = self.ocr_window(win, "order_window")
        for side in ("buy", "sell"):
            pt = (win.x + points[side][0], win.y + points[side][1])
            got = parse.nearest_label(items, pt, ["buy", "sell"], BUTTON_LABEL_MAX_DIST)
            if got != side:
                raise CalibrationError(
                    "the %s point is not on the %s button: OCR found %s within %.0f pt of it. Run "
                    "`tvbridge calibrate` again and hover over the middle of the '%s by Market' button."
                    % (side.upper(), side.upper(), repr(got) if got else "no Buy/Sell label",
                       BUTTON_LABEL_MAX_DIST, side.capitalize()))
        self.out("  OK: OCR confirms the Buy and Sell points are on the Buy and Sell buttons.")
        self.check_fields(win, items, points)
        joined = " ".join(i.text for i in items).lower()
        missing = [t for t in self.gui.require_dialog_text if t and t.lower() not in joined]
        if missing:
            self.out("WARNING: the order window does not show %s (executor.gui.require_dialog_text); "
                     "orders will fail verification until it does." % missing)

    def check_fields(self, win: Window, items: List[Any], points: Dict[str, List[float]]) -> None:
        """Each of the Volume / Stop Loss / Take Profit points must sit right of, and level with,
        its own label, nearer to it than to any other field label (the executor reads every
        value back next to its label, so swapped points could otherwise go unnoticed)."""
        labels = []  # type: List[Tuple[str, str, float, Any]]   # (key, name, right edge of the label, item)
        for it in items:
            text = " ".join((it.text or "").split())
            for key, name, pat in FIELD_LABELS:
                for m in pat.finditer(text):
                    right = float(it.x) + float(it.w) * (m.end() / float(max(1, len(text))))
                    labels.append((key, name, right, it))
        for key, name, _pat in FIELD_LABELS:
            px, py = win.x + points[key][0], win.y + points[key][1]
            near = []  # type: List[Tuple[float, str, str]]
            for k2, name2, right, it in labels:
                if right <= px + 2.0 and abs(it.cy - py) <= max(0.9 * float(it.h), 10.0):
                    near.append((px - right, k2, name2))
            if not near:
                raise CalibrationError(
                    "the %s point has no '%s' label to its left on the same line (OCR read no field label "
                    "there). Run `tvbridge calibrate` again and hover over the middle of the %s field."
                    % (name, name, name))
            _d, got, got_name = min(near)
            if got != key:
                raise CalibrationError(
                    "the %s point is next to the '%s' label, not '%s': the field points are swapped or "
                    "misplaced. Run `tvbridge calibrate` again." % (name, got_name, name))
        self.out("  OK: OCR confirms the Volume, Stop Loss and Take Profit points sit next to their labels.")

    def close_dialog(self, dialog: Window) -> None:
        for attempt in range(1, MAX_TRIES + 1):
            self.ask("Now close the New Order window yourself (press Escape in MetaTrader 5), then press "
                     "Enter here: ")
            polls = max(1, int(round(DIALOG_WAIT_S / POLL_S)))
            for _ in range(polls):
                if self.window_by_id(dialog.wid) is None:
                    return
                self.driver.sleep(POLL_S)
            self.out("  The New Order window is still open (%d of %d)." % (attempt, MAX_TRIES))
        raise CalibrationError("the New Order window is still open; close it and run calibration again")

    def record_main_points(self, main: Window) -> Tuple[List[float], List[float], Optional[List[float]]]:
        label = "MetaTrader 5 main window"
        focus = self.point_in(main.wid, label, "an empty part of the MetaTrader 5 window TITLE BAR (a safe "
                              "spot that only focuses the window)", max_dy=FOCUS_MAX_DY)
        for attempt in range(1, MAX_TRIES + 1):
            tl = self.point_in(main.wid, label, "the TOP-LEFT corner of the Toolbox 'Trade' list (just above "
                               "the column headers)")
            br = self.point_in(main.wid, label, "the BOTTOM-RIGHT corner of the Toolbox 'Trade' list (below "
                               "the 'Balance:' line)")
            region = [min(tl[0], br[0]), min(tl[1], br[1]), round(abs(br[0] - tl[0]), 1),
                      round(abs(br[1] - tl[1]), 1)]
            if region[2] >= 100 and region[3] >= 30:
                break
            self.out("  That area is only %gx%g pt; it must cover the whole Trade list. Try again (%d of %d)."
                     % (region[2], region[3], attempt, MAX_TRIES))
        else:
            raise CalibrationError("the Toolbox area is too small; run `tvbridge calibrate` again")
        trade_tab = None  # type: Optional[List[float]]
        answer = self.ask("Optional: to record the Toolbox 'Trade' tab label (tvbridge clicks it if the "
                          "account line cannot be read), type y and press Enter, then hover over it within "
                          "%d s. Press Enter alone to skip: " % HOVER_SECONDS)
        if answer.lower() in ("y", "yes"):
            trade_tab = self.point_in(main.wid, label, "the 'Trade' tab label", ask=False)
        return focus, region, trade_tab

    def check_main(self, main: Window) -> Window:
        now = self.window_by_id(main.wid)
        if now is None:
            raise CalibrationError("the MetaTrader 5 main window disappeared; start again")
        if abs(now.w - main.w) > 1 or abs(now.h - main.h) > 1:
            raise CalibrationError("the MetaTrader 5 main window changed size during calibration; start again")
        return now

    def verify_toolbox(self, main: Window, region: List[float], trade_tab: Optional[List[float]]) -> None:
        self.out("Reading the Toolbox ...")
        try:
            items = self.ocr_window(main, "toolbox", tuple(region))
        except CalibrationError as e:
            self.out("FAILED to read the Toolbox: %s" % e)
            return
        account = parse.parse_account_line(items)
        if account is None:
            self.out("FAILED: could not read 'Balance:' and 'Equity:' in the Toolbox area. Make sure the "
                     "Trade tab is selected and the area includes the Balance line. tvbridge will not "
                     "trade (ACCOUNT_UNREADABLE) until `tvbridge read-account` works; re-run "
                     "`tvbridge calibrate` if needed.")
        else:
            self.out("  Balance %.2f, Equity %.2f%s" % (
                account["balance"], account["equity"],
                ", Free margin %.2f" % account["free_margin"] if "free_margin" in account else ""))
            positions = parse.parse_position_rows(items, self.cfg.known_mt5_symbols())
            self.out("  Open positions seen: %d" % len(positions))
            for pos, _anchor in positions:
                self.out("    %s %s %g" % (pos.symbol, pos.side, pos.lots))
        if trade_tab is not None:
            box = (max(0.0, trade_tab[0] - 60), max(0.0, trade_tab[1] - 20), 120.0, 40.0)
            try:
                tab_items = self.ocr_window(main, "trade_tab", box)
            except CalibrationError as e:
                self.out("WARNING: could not check the Trade tab label: %s" % e)
                return
            pt = (main.x + trade_tab[0], main.y + trade_tab[1])
            if parse.nearest_label(tab_items, pt, ["Trade"], 40.0) is None:
                self.out("WARNING: OCR did not find a 'Trade' label at the recorded Trade tab point.")
            else:
                self.out("  OK: the Trade tab label was found at the recorded point.")

    def run(self) -> Calibration:
        self.intro()
        self.check_permissions()
        main = self.find_main()

        dialog = self.open_dialog(main)
        points = self.record_dialog_points(dialog)
        self.check_buttons(dialog, points)
        self.close_dialog(dialog)

        main = self.check_main(main)
        focus, region, trade_tab = self.record_main_points(main)
        main = self.check_main(main)

        calib = Calibration(
            version=CALIBRATION_VERSION,
            created_at=clock.iso(clock.utcnow()),
            main_window={"title": main.title, "w": main.w, "h": main.h},
            order_dialog={"title": dialog.title, "w": dialog.w, "h": dialog.h, "points": points},
            toolbox_region=region,
            focus_point=focus,
            trade_tab_point=trade_tab,
        )
        calib.validate()
        self.verify_toolbox(main, region, trade_tab)
        save_calibration(self.path, calib)
        self.out("Saved calibration to %s" % self.path)
        self.out("Next: `tvbridge rehearse ...` on the DEMO account to test the full order path.")
        return calib


def run_wizard(driver: Driver, cfg: Any, path: PathLike, input_fn: Callable[[str], str] = input,
               print_fn: Callable[..., Any] = print) -> Calibration:
    """Interactive calibration (see the module docstring). Saves to ``path`` and returns it.

    Raises :class:`CalibrationError` if a step cannot be completed (nothing is saved then).
    """
    return _Wizard(driver, cfg, path, input_fn, print_fn).run()
