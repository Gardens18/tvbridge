"""Windows desktop driver: Win32 windows and input (ctypes), GDI screenshots, Windows.Media.Ocr.

Same :class:`~tvbridge.gui.driver.Driver` contract as ``MacDriver``. The process is made
per-monitor DPI aware, so every coordinate is a physical screen pixel and ``scale`` is 1.0.
It must run in the interactive desktop session that shows MetaTrader (not in a service or
SSH session): input and screenshots only exist there.

``winrt`` (OCR) is imported lazily, so this module imports without it.
"""

import ctypes
import logging
import os
import struct
import time
import zlib
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

from . import keys
from .driver import Driver, OcrItem, Window

log = logging.getLogger("tvbridge.gui.windriver")

#: Windows virtual-key codes for the key names in ``keys.KEYCODES``.
VK = {
    "period": 0xBE, "comma": 0xBC, "minus": 0xBD, "equal": 0xBB, "slash": 0xBF, "semicolon": 0xBA,
    "space": 0x20, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "delete": 0x08, "forwarddelete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}  # type: Dict[str, int]
VK.update({chr(c): 0x41 + c - ord("a") for c in range(ord("a"), ord("z") + 1)})
VK.update({str(d): 0x30 + d for d in range(10)})
VK.update({"f%d" % n: 0x6F + n for n in range(1, 13)})
MOD_VK = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12}  # type: Dict[str, int]
_EXTENDED = {"forwarddelete", "home", "end", "pageup", "pagedown", "left", "up", "right", "down"}

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: Windows.Media.Ocr refuses images larger than this on either edge.
OCR_MAX_PX = 2600
TILE_PX = 1200
TILE_STRIDE = 600
EDGE_PX = 3


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def write_png(path: str, width: int, height: int, bgra: bytes) -> None:
    """Write top-down 32-bit BGRA pixels as an 8-bit RGB PNG (standard library only)."""
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 1)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def pixels_look_blank(bgra: bytes, width: int, height: int, grid: int = 32) -> bool:
    """True when a sampled grid of pixels is one flat colour (a refused or empty capture)."""
    if width <= 0 or height <= 0:
        return True
    first = None
    for gy in range(grid):
        y = min(height - 1, (gy * height) // grid)
        for gx in range(grid):
            x = min(width - 1, (gx * width) // grid)
            i = (y * width + x) * 4
            px = bgra[i:i + 3]
            if first is None:
                first = px
            elif px != first:
                return False
    return True


def group_words(words: List[Tuple[str, float, float, float, float]]) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """Join the words of one OCR line into phrases, splitting at gaps wider than ~1.2 text heights.

    Windows returns a whole screen row as one line ("Symbol Bid Ask"); table cells and
    separate labels must stay separate items, as they are in the macOS Vision results.
    """
    out = []  # type: List[Tuple[str, Tuple[float, float, float, float]]]
    cur = []  # type: List[Tuple[str, float, float, float, float]]

    def flush() -> None:
        if not cur:
            return
        x0 = min(w[1] for w in cur)
        y0 = min(w[2] for w in cur)
        x1 = max(w[1] + w[3] for w in cur)
        y1 = max(w[2] + w[4] for w in cur)
        out.append((" ".join(w[0] for w in cur), (x0, y0, x1 - x0, y1 - y0)))

    for w in sorted(words, key=lambda w: w[1]):
        if cur:
            prev = cur[-1]
            gap = w[1] - (prev[1] + prev[3])
            height = max(prev[4], w[4], 1.0)
            if gap > 1.2 * height:
                flush()
                cur = []
        cur.append(w)
    flush()
    return out


def drop_fragments(results: List[Any]) -> List[Any]:
    """Remove a result that is only a piece of a longer one on the same row.

    Overlapping tiles can each see a phrase, one of them cut short at its own edge further
    along ("Do you want" next to "Do you want to allow"); the longer reading is the true one.
    """
    keep = []
    for i, (text, conf, (x, y, w, h)) in enumerate(results):
        fragment = False
        for j, (text2, _c2, (x2, y2, w2, h2)) in enumerate(results):
            if i == j or len(text2) <= len(text) or text not in text2:
                continue
            same_row = abs((y + h / 2.0) - (y2 + h2 / 2.0)) <= max(h, h2) * 0.6
            inside = x >= x2 - 4 and x + w <= x2 + w2 + 4
            if same_row and inside:
                fragment = True
                break
        if not fragment:
            keep.append((text, conf, (x, y, w, h)))
    return keep


def _tile_starts(total: int) -> List[int]:
    if total <= TILE_PX:
        return [0]
    starts = list(range(0, total - TILE_PX, TILE_STRIDE))
    starts.append(total - TILE_PX)
    return sorted(set(starts))


class WinDriver(Driver):
    """Real Windows implementation (SendInput, BitBlt, Windows.Media.Ocr)."""

    MOVE_SETTLE_S = 0.05
    PRESS_S = 0.02
    TYPE_INTERVAL_S = 0.02

    _target_pid = None  # type: Optional[int]
    #: (x, y, w, h) the MT5 main window is moved to on every activate, from the environment variable
    #: TVBRIDGE_WIN_GEOMETRY ("0,0,1024,728"). Remote-desktop and console sessions have different
    #: screen sizes; a fixed, small window keeps the calibration valid in all of them.
    main_geometry = None  # type: Optional[Tuple[int, int, int, int]]

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("WinDriver only runs on Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:  # already set, or an older Windows
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass
        try:
            self.dwmapi = ctypes.WinDLL("dwmapi")  # type: Any
        except OSError:
            self.dwmapi = None
        u, g, k = self.user32, self.gdi32, self.kernel32
        self._enum_proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self._enum_proc_t, wintypes.LPARAM]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k.VirtualAllocEx.restype = ctypes.c_void_p
        k.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        k.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                         ctypes.c_void_p]
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.c_void_p]
        u.EnumChildWindows.argtypes = [wintypes.HWND, self._enum_proc_t, wintypes.LPARAM]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                          wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self._owner_cache = {}  # type: Dict[int, str]
        geo = os.environ.get("TVBRIDGE_WIN_GEOMETRY", "").strip()
        if geo:
            try:
                x, y, w, h = (int(v) for v in geo.split(","))
                self.main_geometry = (x, y, w, h)
            except ValueError:
                log.warning("ignoring TVBRIDGE_WIN_GEOMETRY=%r (want x,y,w,h)", geo)
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindow.restype = wintypes.HWND

    def _apply_geometry(self, pid: int) -> None:
        """Move the un-owned main window of ``pid`` to :attr:`main_geometry` (restoring it if maximised)."""
        if self.main_geometry is None:
            return
        x, y, w, h = self.main_geometry
        for win in self.list_windows([]):
            if win.pid != int(pid) or self.user32.GetWindow(win.wid, 4):   # GW_OWNER: dialogs are owned
                continue
            fx, fy, fw, fh = self._bounds(win.wid)
            if (round(fx), round(fy), round(fw), round(fh)) == (x, y, w, h):
                return
            if self.user32.IsZoomed(win.wid):
                self.user32.ShowWindow(win.wid, 9)                          # SW_RESTORE
                time.sleep(0.2)
            # SetWindowPos works on the outer rectangle, which includes invisible resize borders
            r = wintypes.RECT()
            self.user32.GetWindowRect(win.wid, ctypes.byref(r))
            fx, fy, fw, fh = self._bounds(win.wid)
            bl, bt = int(round(fx - r.left)), int(round(fy - r.top))
            br, bb = int(round(r.right - (fx + fw))), int(round(r.bottom - (fy + fh)))
            self.user32.SetWindowPos(win.wid, None, x - bl, y - bt, w + bl + br, h + bt + bb, 0x0004 | 0x0010)
            time.sleep(0.3)
            return

    # ------------------------------------------------------------------ windows

    def _owner_name(self, pid: int) -> str:
        name = self._owner_cache.get(pid)
        if name is not None:
            return name
        name = ""
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value)
            finally:
                self.kernel32.CloseHandle(h)
        self._owner_cache[pid] = name
        return name

    def _bounds(self, hwnd: int) -> Tuple[float, float, float, float]:
        r = wintypes.RECT()
        ok = False
        if self.dwmapi is not None:   # visible frame, without the invisible resize borders
            ok = self.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                                                   ctypes.byref(r), ctypes.sizeof(r)) == 0
        if not ok:
            self.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return float(r.left), float(r.top), float(r.right - r.left), float(r.bottom - r.top)

    def _cloaked(self, hwnd: int) -> bool:
        if self.dwmapi is None:
            return False
        v = wintypes.DWORD(0)
        if self.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v),
                                             ctypes.sizeof(v)) != 0:
            return False
        return bool(v.value)

    def list_windows(self, owner_names: List[str]) -> List[Window]:
        """Visible, non-minimised top-level windows, front to back; owner = executable name."""
        needles = [n.lower() for n in (owner_names or []) if n]
        hwnds = []  # type: List[int]

        def cb(hwnd: int, _lparam: int) -> bool:
            hwnds.append(hwnd)
            return True

        self.user32.EnumWindows(self._enum_proc_t(cb), 0)
        out = []  # type: List[Window]
        for hwnd in hwnds:
            try:
                if not self.user32.IsWindowVisible(hwnd) or self.user32.IsIconic(hwnd) or self._cloaked(hwnd):
                    continue
                pid = wintypes.DWORD(0)
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                owner = self._owner_name(int(pid.value))
                if needles and not any(n in owner.lower() for n in needles):
                    continue
                x, y, w, h = self._bounds(hwnd)
                if w <= 0 or h <= 0:
                    continue
                n = self.user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                self.user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append(Window(wid=int(hwnd), pid=int(pid.value), owner=owner, title=buf.value,
                                  x=x, y=y, w=w, h=h))
            except (TypeError, ValueError, OSError) as e:
                log.debug("skipping window %r: %s", hwnd, e)
        return out

    def _front_pid(self) -> Optional[int]:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None

    def activate(self, pid: int) -> None:
        """Bring the frontmost window of ``pid`` to the foreground and remember it as the target."""
        self._target_pid = int(pid)
        self._apply_geometry(int(pid))
        if self._front_pid() == int(pid):
            return
        wins = [w for w in self.list_windows([]) if w.pid == int(pid)]
        if not wins:
            raise RuntimeError("NOT_FRONT: process %d has no visible window to activate" % pid)
        hwnd = wins[0].wid
        deadline = time.monotonic() + 3.0
        while True:
            # Windows only lets the process that received the last input steal the foreground:
            # a tap of Alt counts as that input.
            self._send_key(MOD_VK["alt"], True, False)
            self._send_key(MOD_VK["alt"], False, False)
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            if self._front_pid() == int(pid):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("NOT_FRONT: could not bring process %d to the foreground" % pid)

    def _require_front(self) -> None:
        if self._target_pid is not None and self._front_pid() != self._target_pid:
            raise RuntimeError("NOT_FRONT: MT5 is not the frontmost app; refusing to send input to another app")

    # ------------------------------------------------------------------ screenshots & OCR

    def _grab(self, x: int, y: int, w: int, h: int) -> bytes:
        u, g = self.user32, self.gdi32
        screen = u.GetDC(None)
        if not screen:
            raise RuntimeError("cannot open the screen (no interactive desktop in this session?)")
        mem = g.CreateCompatibleDC(screen)
        bmp = g.CreateCompatibleBitmap(screen, w, h)
        old = g.SelectObject(mem, bmp)
        try:
            if not g.BitBlt(mem, 0, 0, w, h, screen, x, y, SRCCOPY | CAPTUREBLT):
                raise RuntimeError("BitBlt failed (error %d)" % ctypes.get_last_error())
            hdr = _BITMAPINFOHEADER()
            hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            hdr.biWidth, hdr.biHeight = w, -h      # negative height = top-down rows
            hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0
            buf = ctypes.create_string_buffer(w * h * 4)
            g.SelectObject(mem, old)
            if g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(hdr), 0) != h:
                raise RuntimeError("GetDIBits failed (error %d)" % ctypes.get_last_error())
            return buf.raw
        finally:
            g.SelectObject(mem, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
            u.ReleaseDC(None, screen)

    def capture(self, win: Window, out_path: str) -> float:
        """Copy the screen area under ``win`` to a PNG; returns 1.0 (pixels per point)."""
        if win.w <= 0 or win.h <= 0:
            raise RuntimeError("cannot capture window %d: it has no size (%sx%s)" % (win.wid, win.w, win.h))
        out_path = str(out_path)
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.remove(out_path)
        except FileNotFoundError:
            pass
        w, h = int(round(win.w)), int(round(win.h))
        bgra = self._grab(int(round(win.x)), int(round(win.y)), w, h)
        if pixels_look_blank(bgra, w, h):
            raise RuntimeError("capture of window %d is blank: the desktop session is not being drawn "
                               "(locked, or disconnected without a console session)" % win.wid)
        write_png(out_path, w, h, bgra)
        return 1.0

    def ocr(self, png_path: str, win: Window, scale: float,
            region: Optional[Tuple[float, float, float, float]] = None) -> List[OcrItem]:
        if scale is None or scale <= 0:
            raise ValueError("scale must be > 0 (got %r)" % (scale,))
        region_px = None
        if region is not None:
            dx, dy, rw, rh = (float(v) for v in region)
            region_px = (dx * scale, dy * scale, rw * scale, rh * scale)
        # Native MT5 exposes its fields, buttons, lists and the account line as control text: exact,
        # where pixel OCR of the same 9 pt numbers is not. Pixels are only read when a window has no
        # readable controls at all.
        items = self.control_items(win, png_path, region)
        if items:
            if region is None:
                return items
            dx, dy, rw, rh = (float(v) for v in region)
            x0, y0 = win.x + dx, win.y + dy
            return [i for i in items if x0 <= i.cx <= x0 + rw and y0 <= i.cy <= y0 + rh]
        results, (ox, oy) = recognize_with_origin(png_path, region_px)
        return [OcrItem(text=text, conf=float(conf), x=win.x + (ox + x) / scale, y=win.y + (oy + y) / scale,
                        w=w / scale, h=h / scale) for text, conf, (x, y, w, h) in results]

    # ------------------------------------------------------------------ control text

    def _class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(160)
        self.user32.GetClassNameW(hwnd, buf, 160)
        return buf.value

    def _msg(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> Optional[int]:
        res = ctypes.c_size_t(0)
        ok = self.user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, 0x0002, 1000, ctypes.byref(res))
        return int(res.value) if ok else None

    def _text(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        res = ctypes.c_size_t(0)
        ok = self.user32.SendMessageTimeoutW(hwnd, 0x000D, 1024, ctypes.cast(buf, ctypes.c_void_p).value,
                                             0x0002, 1000, ctypes.byref(res))
        return buf.value if ok else ""

    def _rect(self, hwnd: int) -> Tuple[float, float, float, float]:
        r = wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return float(r.left), float(r.top), float(r.right - r.left), float(r.bottom - r.top)

    def _children(self, hwnd: int) -> List[int]:
        out = []  # type: List[int]

        def cb(h: int, _lparam: int) -> bool:
            out.append(h)
            return True

        self.user32.EnumChildWindows(hwnd, self._enum_proc_t(cb), 0)
        return out

    def list_view_items(self, hwnd: int, pid: int, max_rows: int = 200,
                        blank_rows: Optional[List[Tuple[float, float, float, float]]] = None
                        ) -> Tuple[List[OcrItem], float]:
        """Header and cell texts of a ``SysListView32`` in another process, with their screen boxes.

        Returns (items, y just below the last visible row or the header). MT5 draws its rows
        itself and gives no cell text; the screen box of every such visible row is appended to
        ``blank_rows`` so the caller can read it from pixels.
        """
        k = self.kernel32
        items = []  # type: List[OcrItem]
        lx, ly, lw, lh = self._rect(hwnd)
        pt = wintypes.POINT(0, 0)
        self.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        cx0, cy0 = float(pt.x), float(pt.y)
        bottom = ly
        header = self._msg(hwnd, 0x1000 + 31) or 0
        rows = min(max_rows, self._msg(hwnd, 0x1000 + 4) or 0)
        cols = (self._msg(header, 0x1200) or 0) if header else 0
        if cols <= 0:
            return items, bottom
        proc = k.OpenProcess(0x0008 | 0x0010 | 0x0020 | 0x0400, False, pid)
        if not proc:
            return items, bottom
        mem = k.VirtualAllocEx(proc, None, 8192, 0x3000, 0x04)
        try:
            if not mem:
                return items, bottom
            text_at, rect_at, cch = mem + 512, mem + 4096, 1024

            def write(addr: int, data: bytes) -> None:
                k.WriteProcessMemory(proc, ctypes.c_void_p(addr), data, len(data), None)

            def read(addr: int, n: int) -> bytes:
                buf = ctypes.create_string_buffer(n)
                k.ReadProcessMemory(proc, ctypes.c_void_p(addr), buf, n, None)
                return buf.raw

            def read_text() -> str:
                raw = read(text_at, cch * 2).decode("utf-16-le", "replace")
                return raw.split("\x00", 1)[0].strip()

            def read_rect() -> Tuple[int, int, int, int]:
                return struct.unpack("<iiii", read(rect_at, 16))

            hx, hy, _hw, hh = self._rect(header)
            bottom = hy + hh
            for c in range(cols):
                hd = bytearray(72)
                struct.pack_into("<I", hd, 0, 0x0002)          # HDI_TEXT
                struct.pack_into("<Q", hd, 8, text_at)
                struct.pack_into("<i", hd, 24, cch)
                write(text_at, b"\x00\x00")
                write(mem, bytes(hd))
                if not self._msg(header, 0x1200 + 11, c, mem):
                    continue
                text = read_text()
                write(rect_at, b"\x00" * 16)
                if text and self._msg(header, 0x1200 + 7, c, rect_at):
                    l, t, r, b = read_rect()
                    if r > l:
                        items.append(OcrItem(text, 1.0, hx + l, hy + t, float(r - l), float(b - t)))
            for row in range(rows):
                row_seen = False
                for c in range(cols):
                    lv = bytearray(88)
                    struct.pack_into("<i", lv, 8, c)
                    struct.pack_into("<Q", lv, 24, text_at)
                    struct.pack_into("<i", lv, 32, cch)
                    write(text_at, b"\x00\x00")
                    write(mem, bytes(lv))
                    self._msg(hwnd, 0x1000 + 115, row, mem)
                    text = read_text()
                    if not text:
                        continue
                    write(rect_at, struct.pack("<iiii", 0, c, 0, 0))   # LVIR_BOUNDS of sub-item c
                    if not self._msg(hwnd, 0x1000 + 56, row, rect_at):
                        continue
                    l, t, r, b = read_rect()
                    x, y = cx0 + l, cy0 + t
                    if r <= l or y + (b - t) <= ly or y >= ly + lh:
                        continue                                   # scrolled out of view
                    items.append(OcrItem(text, 1.0, x, y, float(r - l), float(b - t)))
                    bottom = max(bottom, y + (b - t))
                    row_seen = True
                if not row_seen:
                    write(rect_at, struct.pack("<iiii", 0, 0, 0, 0))   # LVIR_BOUNDS of the whole row
                    if self._msg(hwnd, 0x1000 + 14, row, rect_at):
                        l, t, r, b = read_rect()
                        x, y = cx0 + l, cy0 + t
                        if r > l and b > t and y >= bottom - 1 and y + (b - t) <= ly + lh + 1:
                            if blank_rows is not None:
                                blank_rows.append((max(x, lx), y, min(float(r - l), lw), float(b - t)))
                            bottom = max(bottom, y + (b - t))
                        elif y >= ly + lh:
                            break                                  # below the visible part
        finally:
            if mem:
                k.VirtualFreeEx(proc, ctypes.c_void_p(mem), 0, 0x8000)
            k.CloseHandle(proc)
        return items, bottom

    def control_items(self, win: Window, png_path: Optional[str] = None,
                      region: Optional[Tuple[float, float, float, float]] = None) -> List[OcrItem]:
        """Texts of the visible controls inside ``win`` as OCR-style items (global pixels).

        With ``png_path`` (a capture of ``win``), list rows that expose no text are read from
        the image, one row strip at a time.
        """
        items = []  # type: List[OcrItem]
        blank_rows = []  # type: List[Tuple[float, float, float, float]]
        if win.title:
            items.append(OcrItem(win.title, 1.0, win.x + 10, win.y + 6, min(win.w - 20, 8.0 * len(win.title)), 16.0))
        account_text = ""
        trade_list_bottom = None  # type: Optional[Tuple[float, float, float]]
        for h in self._children(win.wid):
            cls = self._class(h)
            visible = bool(self.user32.IsWindowVisible(h))
            if cls.startswith("AfxWnd"):
                t = self._text(h)
                if t.lower().startswith("balance:"):
                    account_text = " ".join(t.split())
                    continue
            if not visible:
                continue
            x, y, w, h_px = self._rect(h)
            if w <= 0 or h_px <= 0:
                continue
            if cls == "SysListView32":
                cells, bottom = self.list_view_items(h, win.pid, blank_rows=blank_rows)
                items.extend(cells)
                if any(c.text.strip().lower() == "ticket" for c in cells):
                    trade_list_bottom = (x, bottom, w)
                continue
            if cls in ("Static", "Edit", "Button", "msctls_statusbar32"):
                t = " ".join(self._text(h).split())
                if t:
                    items.append(OcrItem(t, 1.0, x, y, w, h_px))
        if region is not None:
            dx, dy, rgw, rgh = (float(v) for v in region)
            blank_rows = [r for r in blank_rows
                          if win.x + dx <= r[0] + r[2] / 2.0 <= win.x + dx + rgw
                          and win.y + dy <= r[1] + r[3] / 2.0 <= win.y + dy + rgh]
        account_placed = False
        exe = tesseract_exe()
        if png_path and exe and blank_rows:
            for (rx, ry, rw, rh) in blank_rows[:MAX_PIXEL_ROWS]:
                try:
                    res, (ox, oy) = _recognize_tesseract(exe, png_path, (rx - win.x, ry - win.y, rw, rh), psm=7)
                except Exception as e:  # one unreadable row must not hide the others
                    log.debug("row OCR failed at y=%s: %s", ry, e)
                    continue
                if account_text and not account_placed and any("balance" in t.lower() for t, _c, _b in res):
                    # the account summary row: its exact text is known, so the pixels are not trusted
                    items.append(OcrItem(account_text, 1.0, rx + 24, ry + 1, max(50.0, min(rw - 48, 1000.0)),
                                         max(8.0, rh - 2)))
                    account_placed = True
                    continue
                for text, conf, (x, y, w, h) in res:
                    items.append(OcrItem(text, float(conf), win.x + ox + x, win.y + oy + y, w, h))
        if account_text and not account_placed and trade_list_bottom is not None:
            lx, bottom, lw = trade_list_bottom
            items.append(OcrItem(account_text, 1.0, lx + 24, bottom + 2, max(50.0, min(lw - 48, 1000.0)), 18.0))
        return items

    # ------------------------------------------------------------------ mouse & keyboard

    def _send(self, inp: _INPUT) -> None:
        if self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
            raise RuntimeError("SendInput failed (error %d): input is blocked in this session"
                               % ctypes.get_last_error())

    def _send_mouse(self, flags: int) -> None:
        inp = _INPUT(type=INPUT_MOUSE)
        inp.u.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
        self._send(inp)

    def _send_key(self, vk: int, down: bool, extended: bool) -> None:
        flags = 0 if down else KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = _KEYBDINPUT(vk, self.user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
        self._send(inp)

    def click(self, x: float, y: float, count: int = 1) -> None:
        count = max(1, int(count))
        self._require_front()
        if not self.user32.SetCursorPos(int(round(x)), int(round(y))):
            raise RuntimeError("SetCursorPos failed (error %d)" % ctypes.get_last_error())
        time.sleep(self.MOVE_SETTLE_S)
        for i in range(1, count + 1):
            self._send_mouse(MOUSEEVENTF_LEFTDOWN)
            time.sleep(self.PRESS_S)
            self._send_mouse(MOUSEEVENTF_LEFTUP)
            if i < count:
                time.sleep(self.PRESS_S)

    def key(self, name: str, mods: Tuple[str, ...] = ()) -> None:
        keys.keycode(name)   # validates the name (and aliases) exactly like the macOS driver
        low = name if name == " " else name.strip().lower()
        low = keys.KEY_ALIASES.get(low, low)
        vk = VK[low]
        mods_n = keys.normalize_mods(mods)
        for m in mods_n:
            if m not in MOD_VK:
                raise ValueError("modifier %r does not exist on Windows" % (m,))
        self._require_front()
        held = []  # type: List[str]
        try:
            for m in mods_n:
                self._send_key(MOD_VK[m], True, False)
                held.append(m)
            self._send_key(vk, True, low in _EXTENDED)
            time.sleep(self.PRESS_S)
            self._send_key(vk, False, low in _EXTENDED)
        finally:
            for m in reversed(held):
                self._send_key(MOD_VK[m], False, False)

    def type_text(self, text: str) -> None:
        for name, mods in keys.text_to_keys(text):
            self.key(name, mods)
            time.sleep(self.TYPE_INTERVAL_S)

    def mouse_location(self) -> Tuple[float, float]:
        pt = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return float(pt.x), float(pt.y)

    def sleep(self, s: float) -> None:
        if s and s > 0:
            time.sleep(s)

    def close_window(self, wid: int) -> None:
        """Ask a window to close (WM_CLOSE): a result page closes, an order form is cancelled."""
        self.user32.PostMessageW(wintypes.HWND(int(wid)), 0x0010, 0, 0)

    # ------------------------------------------------------------------ permissions

    def permissions(self) -> Dict[str, bool]:
        """Windows has no per-app grants: both are true when an interactive desktop is reachable."""
        ok = False
        try:
            dc = self.user32.GetDC(None)
            if dc:
                self.user32.ReleaseDC(None, dc)
                ok = bool(self.user32.GetForegroundWindow()) or bool(self.list_windows([]))
        except Exception:
            ok = False
        return {"accessibility": ok, "screen_recording": ok}

    def request_permissions(self) -> None:
        return None


# --------------------------------------------------------------------------- OCR (Windows.Media.Ocr)

OcrResult = Tuple[str, float, Tuple[float, float, float, float]]


def _clamp_region(region_px: Tuple[float, float, float, float], img_w: int, img_h: int) -> Tuple[int, int, int, int]:
    x, y, w, h = region_px
    x0 = max(0, min(img_w, int(x)))
    y0 = max(0, min(img_h, int(y)))
    x1 = max(x0, min(img_w, int(round(x + w))))
    y1 = max(y0, min(img_h, int(round(y + h))))
    return x0, y0, x1 - x0, y1 - y0


#: Tesseract reads MT5's small numbers far more reliably than Windows.Media.Ocr; used when installed.
TESSERACT_PATHS = (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                   r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
TESSERACT_TIMEOUT_S = 30.0
#: Upscaled images are capped at this many pixels on the long edge (speed).
TESS_MAX_EDGE = 4200
#: At most this many text-less list rows are read from pixels per capture.
MAX_PIXEL_ROWS = 40


def tesseract_exe() -> Optional[str]:
    env = os.environ.get("TVBRIDGE_TESSERACT")
    for path in ((env,) if env else ()) + TESSERACT_PATHS:
        if path and os.path.exists(path):
            return path
    return None


def parse_tesseract_tsv(tsv: str, up: float) -> List[OcrResult]:
    """Word rows of ``tesseract ... tsv`` -> phrases (see :func:`group_words`) in un-scaled pixels."""
    lines = {}  # type: Dict[Tuple[int, int, int], List[Tuple[str, float, float, float, float, float]]]
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5":
            continue
        text = f[11].strip()
        try:
            conf = float(f[10])
            x, y, w, h = (float(v) / up for v in f[6:10])
        except ValueError:
            continue
        if not text or conf < 0:
            continue
        lines.setdefault((int(f[2]), int(f[3]), int(f[4])), []).append((text, x, y, w, h, conf))
    out = []  # type: List[OcrResult]
    for words in lines.values():
        confs = {(w[1], w[2]): w[5] for w in words}
        for text, (bx, by, bw, bh) in group_words([w[:5] for w in words]):
            inside = [c for (wx, wy), c in confs.items() if bx - 1 <= wx <= bx + bw + 1 and by - 1 <= wy <= by + bh + 1]
            conf = (sum(inside) / len(inside) / 100.0) if inside else 0.0
            out.append((text, conf, (bx, by, bw, bh)))
    out.sort(key=lambda r: (r[2][1], r[2][0]))
    return out


def _recognize_tesseract(exe: str, png_path: str, region_px: Optional[Tuple[float, float, float, float]],
                         psm: int = 11) -> Tuple[List[OcrResult], Tuple[int, int]]:
    import subprocess
    import tempfile

    from PIL import Image

    with Image.open(png_path) as src:
        img = src.convert("L")
    x0, y0, cw, ch = (0, 0, img.width, img.height) if region_px is None else _clamp_region(region_px, img.width, img.height)
    if cw <= 0 or ch <= 0:
        return [], (x0, y0)
    if (x0, y0, cw, ch) != (0, 0, img.width, img.height):
        img = img.crop((x0, y0, x0 + cw, y0 + ch))
    up = max(1.0, min(3.0, TESS_MAX_EDGE / float(max(cw, ch))))
    img = img.resize((int(round(cw * up)), int(round(ch * up))), Image.LANCZOS)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="tvb_ocr_")
    os.close(fd)
    try:
        img.save(tmp)
        proc = subprocess.run([exe, tmp, "stdout", "--psm", str(int(psm)), "-l", "eng", "tsv"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=TESSERACT_TIMEOUT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError("tesseract failed (exit %d): %s"
                           % (proc.returncode, (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]))
    return parse_tesseract_tsv(proc.stdout.decode("utf-8", "replace"), up), (x0, y0)


def recognize_with_origin(png_path: str, region_px: Optional[Tuple[float, float, float, float]] = None
                          ) -> Tuple[List[OcrResult], Tuple[int, int]]:
    """OCR ``png_path`` (optionally a crop). Boxes are pixels relative to the crop origin, which is returned."""
    exe = tesseract_exe()
    if exe:
        return _recognize_tesseract(exe, png_path, region_px)
    import asyncio

    return asyncio.run(_recognize(png_path, region_px))


async def _recognize(png_path: str, region_px: Optional[Tuple[float, float, float, float]]
                     ) -> Tuple[List[OcrResult], Tuple[int, int]]:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import (BitmapAlphaMode, BitmapBounds, BitmapDecoder,
                                                BitmapInterpolationMode, BitmapPixelFormat, BitmapTransform,
                                                ColorManagementMode, ExifOrientationMode)
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile

    engine = OcrEngine.try_create_from_language(Language("en-US")) or OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR has no English recognizer installed")
    file = await StorageFile.get_file_from_path_async(os.path.abspath(png_path))
    stream = await file.open_async(FileAccessMode.READ)
    try:
        decoder = await BitmapDecoder.create_async(stream)
        img_w, img_h = int(decoder.pixel_width), int(decoder.pixel_height)
        x0, y0, cw, ch = (0, 0, img_w, img_h) if region_px is None else _clamp_region(region_px, img_w, img_h)
        if cw <= 0 or ch <= 0:
            return [], (x0, y0)
        up = 2.0   # UI text is small: Windows OCR reads it far better at 2x
        out = []  # type: List[OcrResult]
        for ty in _tile_starts(ch):
            for tx in _tile_starts(cw):
                tw, th = min(TILE_PX, cw - tx), min(TILE_PX, ch - ty)
                t = BitmapTransform()
                t.scaled_width = int(round(img_w * up))
                t.scaled_height = int(round(img_h * up))
                t.interpolation_mode = BitmapInterpolationMode.CUBIC
                t.bounds = BitmapBounds(int(round((x0 + tx) * up)), int(round((y0 + ty) * up)),
                                        int(round(tw * up)), int(round(th * up)))
                bmp = await decoder.get_software_bitmap_transformed_async(
                    BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMULTIPLIED, t,
                    ExifOrientationMode.IGNORE_EXIF_ORIENTATION, ColorManagementMode.DO_NOT_COLOR_MANAGE)
                result = await engine.recognize_async(bmp)
                for line in result.lines:
                    words = []
                    for wd in line.words:
                        r = wd.bounding_rect
                        words.append((wd.text, r.x / up, r.y / up, r.width / up, r.height / up))
                    for text, (bx, by, bw, bh) in group_words(words):
                        if ((tx > 0 and bx <= EDGE_PX) or (ty > 0 and by <= EDGE_PX)
                                or (tx + tw < cw and bx + bw >= tw - EDGE_PX)
                                or (ty + th < ch and by + bh >= th - EDGE_PX)):
                            continue   # cut by an inner tile edge: the overlapping tile has it whole
                        out.append((text, 1.0, (bx + tx, by + ty, bw, bh)))
        seen, unique = set(), []
        for r in out:
            key = (r[0], round(r[2][0] / 6), round(r[2][1] / 6))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        unique = drop_fragments(unique)
        unique.sort(key=lambda r: (r[2][1], r[2][0]))
        return unique, (x0, y0)
    finally:
        stream.close()
