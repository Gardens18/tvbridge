"""Fire-and-forget notifications: macOS Notification Center, ntfy, Telegram.

``Notifier.send`` never raises and never blocks on the network: each channel runs in
its own daemon thread with a 5 s timeout. Messages below ``min_level`` are dropped.
"""

import json
import logging
import subprocess
import threading
import urllib.request
from collections import namedtuple
from typing import Callable, List, Optional

from .config import NotifyCfg

log = logging.getLogger("tvbridge.notify")

LEVELS = {"debug": 10, "info": 20, "warn": 30, "critical": 40}
_ALIASES = {"warning": "warn", "error": "critical", "crit": "critical", "fatal": "critical"}
NTFY_PRIORITY = {"critical": "5", "warn": "4", "info": "3", "debug": "2"}
TIMEOUT_S = 5.0

MAX_REMOTE_CHARS = 3500   # ntfy / Telegram message bodies are truncated to this length


class Sent(namedtuple("_Sent", ["title", "message", "level"])):
    """One recorded notification (see :class:`NullNotifier`).

    Usable as a tuple (``title, message, level = s``), by attribute (``s.level``) or by
    key (``s["level"]``).
    """

    __slots__ = ()

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            if key in self._fields:
                return getattr(self, key)
            raise KeyError(key)
        return super().__getitem__(key)

    def get(self, key: str, default=None):
        return getattr(self, key) if key in self._fields else default


def _truncate(s: str, limit: int = MAX_REMOTE_CHARS) -> str:
    return s if len(s) <= limit else s[: limit - 3] + "..."


def normalize_level(level: Optional[str]) -> str:
    """Map a level name (incl. aliases like "warning"/"error") to debug|info|warn|critical."""
    lv = str(level or "info").strip().lower()
    lv = _ALIASES.get(lv, lv)
    return lv if lv in LEVELS else "info"


def applescript_quote(s: str) -> str:
    """Escape for use inside an AppleScript double-quoted string literal."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return s.replace("\r", " ").replace("\n", " ")


class Notifier:
    """Sends notifications on every configured channel."""

    def __init__(self, cfg: Optional[NotifyCfg] = None):
        # Accept a full Config by mistake (use its .notify) or None (defaults).
        if cfg is not None and not hasattr(cfg, "min_level") and hasattr(cfg, "notify"):
            cfg = getattr(cfg, "notify")
        if cfg is None:
            cfg = NotifyCfg()
        self.cfg = cfg
        self.min_level = normalize_level(getattr(cfg, "min_level", "info"))
        self._threads = []  # type: List[threading.Thread]
        self._tlock = threading.Lock()

    def enabled_for(self, level: str) -> bool:
        return LEVELS[normalize_level(level)] >= LEVELS[self.min_level]

    def send(self, title: str, message: str, level: str = "info") -> None:
        """Send a notification. Never raises; drops messages below ``min_level``."""
        try:
            lv = normalize_level(level)
            if LEVELS[lv] < LEVELS[self.min_level]:
                return
            self._dispatch(str(title), str(message), lv)
        except Exception:  # pragma: no cover - defensive: notifications must never break trading
            log.exception("notification dispatch failed")

    def flush(self, timeout: float = TIMEOUT_S + 1.0) -> None:
        """Wait (up to ``timeout`` per thread) for pending channel threads, e.g. before exit."""
        with self._tlock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout)
        with self._tlock:
            self._threads = [t for t in self._threads if t.is_alive()]

    # ------------------------------------------------------------------ channels

    def _dispatch(self, title: str, message: str, level: str) -> None:
        cfg = self.cfg
        if getattr(cfg, "macos", False):
            self._spawn("macos", self._send_macos, title, message, level)
        if getattr(cfg, "ntfy_url", ""):
            self._spawn("ntfy", self._send_ntfy, title, message, level)
        if getattr(cfg, "telegram_bot_token", "") and getattr(cfg, "telegram_chat_id", ""):
            self._spawn("telegram", self._send_telegram, title, message, level)

    def _spawn(self, channel: str, fn: Callable[[str, str, str], None], title: str, message: str,
               level: str) -> None:
        def run() -> None:
            try:
                fn(title, message, level)
            except Exception as e:  # never propagate; never include secrets in the log line
                log.warning("notification channel %s failed: %s", channel, type(e).__name__)

        t = threading.Thread(target=run, name="tvbridge-notify-%s" % channel, daemon=True)
        with self._tlock:
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)
        t.start()

    def _send_macos(self, title: str, message: str, level: str) -> None:
        script = 'display notification "%s" with title "%s"' % (applescript_quote(message), applescript_quote(title))
        subprocess.run(["osascript", "-e", script], timeout=TIMEOUT_S, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _send_ntfy(self, title: str, message: str, level: str) -> None:
        # HTTP header values must be latin-1; replace anything else.
        safe_title = title.encode("latin-1", "replace").decode("latin-1")
        req = urllib.request.Request(
            self.cfg.ntfy_url,
            data=_truncate(message).encode("utf-8"),
            method="POST",
            headers={"Title": safe_title, "Priority": NTFY_PRIORITY.get(level, "3")},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read()

    def _send_telegram(self, title: str, message: str, level: str) -> None:
        url = "https://api.telegram.org/bot%s/sendMessage" % self.cfg.telegram_bot_token
        body = json.dumps({"chat_id": self.cfg.telegram_chat_id, "text": _truncate("%s\n%s" % (title, message))})
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read()


class NullNotifier(Notifier):
    """Records notifications in ``.sent`` (list of ``Sent(title, message, level)``) instead of sending.

    Honors ``min_level`` when a cfg is given; without one, everything is recorded.
    """

    def __init__(self, cfg: Optional[NotifyCfg] = None):
        super().__init__(cfg if cfg is not None else NotifyCfg(macos=False, min_level="debug"))
        self.sent = []  # type: List[Sent]
        self._lock = threading.Lock()

    def _dispatch(self, title: str, message: str, level: str) -> None:
        with self._lock:
            self.sent.append(Sent(title, message, level))

    def messages(self, level: Optional[str] = None) -> List[str]:
        """Recorded message texts, optionally filtered by level."""
        with self._lock:
            return [s.message for s in self.sent if level is None or s.level == normalize_level(level)]
