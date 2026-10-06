"""Desktop driver: window discovery, screenshots, OCR, mouse and keyboard.

Everything that touches the real desktop goes through the :class:`Driver` interface so the
MT5 executor and the calibration wizard can be tested with a fake driver.

Coordinate system: **global screen points, top-left origin** (the same space as
``CGWindowListCopyWindowInfo`` bounds and ``CGEvent`` locations). Screenshots are in pixels;
``scale`` (pixels per point) converts between the two.

pyobjc is imported lazily inside :class:`MacDriver` methods, so this module (and
``Window``/``OcrItem``) imports fine without pyobjc.
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import keys

log = logging.getLogger("tvbridge.gui.driver")

SCREENCAPTURE = "/usr/sbin/screencapture"


@dataclass
class Window:
    """An on-screen window. ``x``/``y``/``w``/``h`` are global points, top-left origin."""

    wid: int
    pid: int
    owner: str
    title: str
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return float(self.w) * float(self.h)

    def contains(self, x: float, y: float) -> bool:
        """True if the global point (x, y) lies inside the window bounds (edges included)."""
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


@dataclass
class OcrItem:
    """One OCR observation. Box in global screen points, top-left origin."""

    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


class Driver:
    """Abstract desktop driver. Every method raises ``NotImplementedError``."""

    def list_windows(self, owner_names: List[str]) -> List[Window]:
        """On-screen, layer-0 windows whose owner name contains any needle (case-insensitive)."""
        raise NotImplementedError

    def activate(self, pid: int) -> None:
        """Bring the application with this pid to the front."""
        raise NotImplementedError

    def capture(self, win: Window, out_path: str) -> float:
        """Write a PNG of that window only; return its scale (pixels per point)."""
        raise NotImplementedError

    def ocr(self, png_path: str, win: Window, scale: float,
            region: Optional[Tuple[float, float, float, float]] = None) -> List[OcrItem]:
        """OCR a capture of ``win``. ``region`` = (dx, dy, w, h) in window-relative points.

        Returned coordinates are global screen points.
        """
        raise NotImplementedError

    def click(self, x: float, y: float, count: int = 1) -> None:
        """Left-click at a global point (``count=2`` for a double click)."""
        raise NotImplementedError

    def key(self, name: str, mods: Tuple[str, ...] = ()) -> None:
        """Press and release a key from ``keys.KEYCODES`` with modifiers (shift, ctrl, alt, cmd)."""
        raise NotImplementedError

    def type_text(self, text: str) -> None:
        """Type text key by key (``keys.char_to_key``)."""
        raise NotImplementedError

    def mouse_location(self) -> Tuple[float, float]:
        """Current mouse position in global points."""
        raise NotImplementedError

    def sleep(self, s: float) -> None:
        raise NotImplementedError

    def permissions(self) -> Dict[str, bool]:
        """``{"accessibility": bool, "screen_recording": bool}``."""
        raise NotImplementedError

    def request_permissions(self) -> None:
        """Trigger the macOS permission prompts."""
        raise NotImplementedError


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def default_driver() -> Driver:
    """The real driver for this operating system (Windows or macOS)."""
    if os.name == "nt":
        from .windriver import WinDriver

        return WinDriver()
    return MacDriver()


class MacDriver(Driver):
    """Real macOS implementation (Quartz events, ``screencapture``, Vision OCR).

    Needs Accessibility permission to post mouse/keyboard events and Screen Recording
    permission to read other applications' window titles and pixels. Both are granted to
    the *Python binary* that runs tvbridge.
    """

    #: Pause after posting a mouse move before pressing the button (lets Wine see the hover).
    MOVE_SETTLE_S = 0.05
    #: Pause between button/key down and up, and between successive clicks.
    PRESS_S = 0.02
    #: Pause between characters in :meth:`type_text`.
    TYPE_INTERVAL_S = 0.02
    #: Timeout for ``screencapture``.
    CAPTURE_TIMEOUT_S = 10.0

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _quartz() -> Any:
        import Quartz  # noqa: WPS433 (lazy import by design)
        return Quartz

    # ------------------------------------------------------------------ windows

    def list_windows(self, owner_names: List[str]) -> List[Window]:
        """On-screen, layer-0 windows whose owner name contains any needle (case-insensitive).

        An empty ``owner_names`` list matches every owner. Window titles of other
        applications are only available with Screen Recording permission (otherwise "").
        """
        Q = self._quartz()
        opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        infos = Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []
        needles = [n.lower() for n in (owner_names or []) if n]
        out = []  # type: List[Window]
        for info in infos:
            try:
                if int(info.get(Q.kCGWindowLayer, 0)) != 0:
                    continue
                owner = str(info.get(Q.kCGWindowOwnerName) or "")
                if needles and not any(n in owner.lower() for n in needles):
                    continue
                alpha = info.get(Q.kCGWindowAlpha)
                if alpha is not None and float(alpha) <= 0.0:
                    continue
                b = info.get(Q.kCGWindowBounds) or {}
                out.append(Window(
                    wid=int(info.get(Q.kCGWindowNumber)),
                    pid=int(info.get(Q.kCGWindowOwnerPID)),
                    owner=owner,
                    title=str(info.get(Q.kCGWindowName) or ""),
                    x=float(b.get("X", 0.0)),
                    y=float(b.get("Y", 0.0)),
                    w=float(b.get("Width", 0.0)),
                    h=float(b.get("Height", 0.0)),
                ))
            except (TypeError, ValueError) as e:  # malformed entry: skip it, never crash discovery
                log.debug("skipping window entry %r: %s", info, e)
        return out

    _target_pid = None  # type: Optional[int]

    def _front_pid(self) -> Optional[int]:
        """Pid owning the frontmost normal window (window-server order; needs no run loop)."""
        Q = self._quartz()
        opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        for w in Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer") == 0:
                return int(w.get("kCGWindowOwnerPID") or 0)
        return None

    def _top_window_id(self, pid: int) -> int:
        """The app's topmost real window (Wine also owns tiny helper windows: skip those)."""
        Q = self._quartz()
        opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        for w in Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer") == 0 and int(w.get("kCGWindowOwnerPID") or 0) == int(pid):
                b = w.get("kCGWindowBounds") or {}
                if float(b.get("Width", 0)) >= 100 and float(b.get("Height", 0)) >= 100:
                    return int(w.get("kCGWindowNumber") or 0)
        return 0

    def activate(self, pid: int) -> None:
        """Bring the app to the front and verify it. Raises RuntimeError if it does not come.

        ``NSRunningApplication.activate`` is ignored by recent macOS when the caller is a
        background process, so the window server's front-process call is used first.
        Remembers the pid: ``key``/``click`` then refuse to act unless it is still frontmost
        (a key sent to another app could do anything there).
        """
        import ctypes
        import ctypes.util

        pid = int(pid)
        self._target_pid = pid
        for attempt in range(3):
            if self._front_pid() == pid:
                return
            try:
                sl = ctypes.cdll.LoadLibrary("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
                hi = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))

                class _PSN(ctypes.Structure):
                    _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]

                psn = _PSN()
                if hi.GetProcessForPID(ctypes.c_int(pid), ctypes.byref(psn)) == 0:
                    # first the app's top window; then "all windows" (the form that works when
                    # only the main window exists)
                    wid, mode = (self._top_window_id(pid), 0x200) if attempt == 0 else (0, 0x100)
                    sl._SLPSSetFrontProcessWithOptions(ctypes.byref(psn), ctypes.c_uint32(wid), ctypes.c_uint32(mode))
            except Exception as e:  # fall through to the public API
                log.debug("SkyLight activation failed: %s", e)
            if attempt:
                from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
                app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is not None:
                    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            for _ in range(10):
                time.sleep(0.1)
                if self._front_pid() == pid:
                    return
        raise RuntimeError("NOT_FRONT: could not bring the MT5 window (pid %d) to the front" % pid)

    def _require_front(self) -> None:
        if self._target_pid is not None and self._front_pid() != self._target_pid:
            raise RuntimeError("NOT_FRONT: MT5 is not the frontmost app; refusing to send input to another app")

    # ------------------------------------------------------------------ screenshots & OCR

    def capture(self, win: Window, out_path: str) -> float:
        """Capture ``win`` with ``screencapture -x -o -l <wid>`` and return pixels per point.

        Raises ``RuntimeError`` if the capture fails or the image is empty, or if it is
        blank/black, which is what macOS produces when Screen Recording permission is
        missing for this Python binary.
        """
        if win.w <= 0 or win.h <= 0:
            raise RuntimeError("cannot capture window %d: it has no size (%sx%s)" % (win.wid, win.w, win.h))
        out_path = str(out_path)
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.remove(out_path)  # never mistake a stale file for a fresh capture
        except FileNotFoundError:
            pass
        cmd = [SCREENCAPTURE, "-x", "-o", "-l", str(int(win.wid)), out_path]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=self.CAPTURE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError("screencapture timed out after %.0f s for window %d"
                               % (self.CAPTURE_TIMEOUT_S, win.wid))
        except OSError as e:
            raise RuntimeError("cannot run %s: %s" % (SCREENCAPTURE, e))
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("screencapture failed for window %d (exit %d): %s"
                               % (win.wid, proc.returncode, err or "no message"))
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("screencapture produced no image for window %d (window closed, or "
                               "Screen Recording permission missing?)" % win.wid)

        try:
            width_px, blank = self._inspect_png(out_path)
        except RuntimeError:
            _remove_quietly(out_path)
            raise
        if width_px <= 0 or blank:
            _remove_quietly(out_path)   # a refused capture must not pile up blank files
        if width_px <= 0:
            raise RuntimeError("screencapture produced an empty image for window %d" % win.wid)
        if blank:
            raise RuntimeError(
                "screen capture of window %d is blank/black: grant Screen Recording permission to "
                "this Python binary (System Settings > Privacy & Security > Screen Recording) and "
                "make sure the screen is not locked" % win.wid)
        height_px = self._png_height(out_path)
        if height_px > 0 and abs(width_px / float(height_px) - win.w / float(win.h)) > 0.03:
            # Wine can return its whole main window for a dialog's window id: the picture then
            # does not match the window's shape and every coordinate would be wrong. Capture
            # the window's rectangle on screen instead (the dialog is on top while we use it).
            _remove_quietly(out_path)
            rect = "%d,%d,%d,%d" % (round(win.x), round(win.y), round(win.w), round(win.h))
            proc = subprocess.run([SCREENCAPTURE, "-x", "-R", rect, out_path], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=self.CAPTURE_TIMEOUT_S)
            if proc.returncode != 0 or not os.path.exists(out_path):
                raise RuntimeError("screencapture -R failed for window %d" % win.wid)
            width_px, blank = self._inspect_png(out_path)
            if width_px <= 0 or blank:
                _remove_quietly(out_path)
                raise RuntimeError("screen capture of window %d is blank" % win.wid)
        return float(width_px) / float(win.w)

    @classmethod
    def _png_height(cls, path: str) -> int:
        from Foundation import NSURL

        Q = cls._quartz()
        src = Q.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
        img = Q.CGImageSourceCreateImageAtIndex(src, 0, None) if src is not None else None
        return int(Q.CGImageGetHeight(img)) if img is not None else 0

    @classmethod
    def _inspect_png(cls, path: str) -> Tuple[int, bool]:
        """(pixel width, looks_blank) of a PNG via a cheap 16x16 downsample."""
        from Foundation import NSURL

        Q = cls._quartz()
        src = Q.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
        if src is None or Q.CGImageSourceGetCount(src) < 1:
            raise RuntimeError("cannot read captured image %s" % path)
        img = Q.CGImageSourceCreateImageAtIndex(src, 0, None)
        if img is None:
            raise RuntimeError("cannot decode captured image %s" % path)
        width = int(Q.CGImageGetWidth(img))
        if width <= 0 or int(Q.CGImageGetHeight(img)) <= 0:
            return 0, True
        return width, image_looks_blank(img)

    def ocr(self, png_path: str, win: Window, scale: float,
            region: Optional[Tuple[float, float, float, float]] = None) -> List[OcrItem]:
        """Vision OCR of a window capture, converted to global screen points.

        ``region`` = (dx, dy, w, h) in window-relative points: the image is cropped first and
        results are offset back, so coordinates are always global.
        """
        from . import ocr as ocr_mod

        if scale is None or scale <= 0:
            raise ValueError("scale must be > 0 (got %r)" % (scale,))
        region_px = None
        if region is not None:
            dx, dy, rw, rh = (float(v) for v in region)
            region_px = (dx * scale, dy * scale, rw * scale, rh * scale)
        # Small text at 1x is read noticeably better by Vision when upscaled.
        upscale = 2.0 if scale < 1.5 else 1.0
        results, (ox, oy) = ocr_mod.recognize_with_origin(png_path, region_px, upscale=upscale)
        items = []  # type: List[OcrItem]
        for text, conf, (x, y, w, h) in results:
            items.append(OcrItem(
                text=text,
                conf=float(conf),
                x=win.x + (ox + x) / scale,
                y=win.y + (oy + y) / scale,
                w=w / scale,
                h=h / scale,
            ))
        return items

    # ------------------------------------------------------------------ mouse & keyboard

    def _post_mouse(self, etype: int, x: float, y: float, click_state: Optional[int] = None) -> None:
        Q = self._quartz()
        ev = Q.CGEventCreateMouseEvent(None, etype, (float(x), float(y)), Q.kCGMouseButtonLeft)
        if ev is None:
            raise RuntimeError("CGEventCreateMouseEvent failed")
        if click_state is not None:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, int(click_state))
        Q.CGEventSetFlags(ev, 0)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def click(self, x: float, y: float, count: int = 1) -> None:
        """Move to (x, y), then ``count`` down/up pairs with click state 1..count."""
        Q = self._quartz()
        count = max(1, int(count))
        self._require_front()
        self._post_mouse(Q.kCGEventMouseMoved, x, y)
        time.sleep(self.MOVE_SETTLE_S)
        for i in range(1, count + 1):
            self._post_mouse(Q.kCGEventLeftMouseDown, x, y, click_state=i)
            time.sleep(self.PRESS_S)
            self._post_mouse(Q.kCGEventLeftMouseUp, x, y, click_state=i)
            if i < count:
                time.sleep(self.PRESS_S)

    def _post_key(self, code: int, down: bool, flags: int) -> None:
        Q = self._quartz()
        ev = Q.CGEventCreateKeyboardEvent(None, int(code), bool(down))
        if ev is None:
            raise RuntimeError("CGEventCreateKeyboardEvent failed")
        Q.CGEventSetFlags(ev, int(flags))   # explicit, so held hardware modifiers never leak in
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def key(self, name: str, mods: Tuple[str, ...] = ()) -> None:
        """Press ``name`` with modifiers. Modifier keys are pressed first and always released."""
        code = keys.keycode(name)
        self._require_front()
        mods_n = keys.normalize_mods(mods)
        flags = keys.modifier_mask(mods_n)
        held = []  # type: List[str]
        cur = 0
        try:
            for m in mods_n:
                cur |= keys.MODIFIER_FLAGS[m]
                self._post_key(keys.MODIFIER_KEYCODES[m], True, cur)
                held.append(m)
            self._post_key(code, True, flags)
            time.sleep(self.PRESS_S)
            self._post_key(code, False, flags)
        finally:
            for m in reversed(held):
                cur &= ~keys.MODIFIER_FLAGS[m]
                self._post_key(keys.MODIFIER_KEYCODES[m], False, cur)

    def type_text(self, text: str) -> None:
        """Type ``text`` key by key. Validates every character before typing any."""
        strokes = keys.text_to_keys(text)
        for name, mods in strokes:
            self.key(name, mods)
            time.sleep(self.TYPE_INTERVAL_S)

    def mouse_location(self) -> Tuple[float, float]:
        Q = self._quartz()
        ev = Q.CGEventCreate(None)
        loc = Q.CGEventGetLocation(ev)
        return float(loc.x), float(loc.y)

    def sleep(self, s: float) -> None:
        if s and s > 0:
            time.sleep(s)

    # ------------------------------------------------------------------ permissions

    def permissions(self) -> Dict[str, bool]:
        out = {"accessibility": False, "screen_recording": False}
        try:
            import ApplicationServices
            out["accessibility"] = bool(ApplicationServices.AXIsProcessTrusted())
        except Exception as e:  # pragma: no cover - depends on the host
            log.warning("cannot query Accessibility permission: %s", e)
        try:
            Q = self._quartz()
            out["screen_recording"] = bool(Q.CGPreflightScreenCaptureAccess())
        except Exception as e:  # pragma: no cover - depends on the host
            log.warning("cannot query Screen Recording permission: %s", e)
        return out

    def request_permissions(self) -> None:
        import ApplicationServices

        ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: True})
        self._quartz().CGRequestScreenCaptureAccess()


def image_looks_blank(img: Any, grid: int = 32) -> bool:
    """True if a CGImage is fully transparent, all (near-)black, or a single flat colour.

    That is what ``screencapture`` yields for another app's window when Screen Recording
    permission is missing. The image is downsampled (area-averaged, so sparse text still
    shows up) into a ``grid``x``grid`` RGBA bitmap, which keeps the check cheap even for
    large windows. A real MT5 window (title bar, toolbars, text) is never uniform.
    """
    import Quartz as Q

    buf = bytearray(grid * grid * 4)
    cs = Q.CGColorSpaceCreateDeviceRGB()
    ctx = Q.CGBitmapContextCreate(buf, grid, grid, 8, grid * 4, cs, Q.kCGImageAlphaPremultipliedLast)
    if ctx is None:  # pragma: no cover - only on exotic failures
        return False
    Q.CGContextSetInterpolationQuality(ctx, Q.kCGInterpolationHigh)
    Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, grid, grid), img)
    data = bytes(buf)
    alphas = data[3::4]
    if max(alphas) == 0:
        return True
    rgb = [max(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 4)]
    if max(rgb) <= 10:
        return True
    for ch in range(3):
        vals = data[ch::4]
        if max(vals) - min(vals) > 3:
            return False
    return True
