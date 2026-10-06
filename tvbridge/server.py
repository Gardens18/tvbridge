"""Webhook listener for TradingView alerts.

A :class:`http.server.ThreadingHTTPServer` on a daemon thread, bound to
``server.host:server.port`` (127.0.0.1:8787; ngrok forwards the public URL to it).

``POST <server.path>`` (also with one trailing "/") runs these checks in order and
answers within a fraction of a second; it never redirects and never echoes the secret:

1. ``Content-Length`` missing -> 411, larger than ``max_body_bytes`` -> 413.
2. Client IP = last ``X-Forwarded-For`` value (ngrok appends the real peer) -- honoured only
   when the socket peer is loopback, where the ngrok agent connects from -- or else the
   socket peer. Loopback without XFF is a local request (``send-test``): 403 unless
   ``allow_local_requests`` (best effort: a local process can also send an XFF header).
   Otherwise, with ``enforce_ip_allowlist``, an IP not in ``tradingview_ips`` -> 403 and
   event ``ip_blocked`` (at most one per IP per minute).
3. The body is read. Not one JSON object -> 400 ``BAD_JSON``; wrong/missing secret -> 401
   (event ``bad_secret``; notification at most every 10 min). These unauthenticated
   requests have their own sliding 60 s budget (``rate_limit_per_min``); once it is used up
   they get 429 without an event, so junk can never use up the budget of real alerts.
4. Authenticated entries (and alerts whose action cannot be told) count against the sliding
   60 s window of ``rate_limit_per_min`` -> 429. Authenticated close / close_all alerts are
   never rate-limited, and neither are mirror-mode ``sync`` alerts (they may carry an exit).
5. Payload refused by :func:`tvbridge.signals.parse_payload` -> 400 with its code.
6. Too old / dated in the future -> 400 ``STALE`` / ``FUTURE`` -- except exits, which fail
   open: a future-dated close is accepted, and a stale one up to ``EXIT_MAX_AGE_S`` (15 min)
   when no ledger position on that symbol/side was opened after the alert fired. Every
   refused authenticated exit sends a critical notification (per symbol, at most every
   10 min): the position stays open until someone closes it. A ``sync`` is treated like an
   exit for lateness (future-dated or up to 15 min old is accepted); the engine acts only on
   the newest sync per symbol and never opens a position on a stale one.
7. Stored in the queue first: a duplicate id is a TradingView retry (same symbol and fire
   time) -> 200 ``{"ok":true,"duplicate":true}``; an explicit ``id`` reused by another firing
   is re-keyed for exits (processed, warn) and for an entry on another symbol, and refused
   for an entry on the same symbol (400 ``ID_REUSED``). Database error -> 503 (TradingView
   retries 5xx).
8. ``on_signal(sig)`` (exceptions are logged, the answer is still 200) -> 200
   ``{"ok":true,"id":...}``.

``GET /health`` -> 200 ``{"ok":true,"version":...}``. Everything else -> 404.
"""

import collections
import dataclasses
import ipaddress
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__, clock
from .config import Config
from .models import Signal
from .signals import (SignalError, check_freshness, check_secret, decode_body, parse_payload, peek_action,
                      strip_secrets)

log = logging.getLogger("tvbridge.server")

HEALTH_PATH = "/health"
RATE_WINDOW_S = 60.0
NOTIFY_INTERVAL_S = 600.0      # bad-secret / store-error notifications: at most every 10 minutes
BLOCKED_EVENT_INTERVAL_S = 60.0  # ip_blocked events: at most one per source IP per minute
MAX_THROTTLE_KEYS = 1024       # bound on remembered throttle keys (distinct blocked IPs)
SOCKET_TIMEOUT_S = 5.0         # per-connection read timeout (slow or idle clients)
DRAIN_LIMIT_BYTES = 64 * 1024  # most we read and discard from a refused request's body
DRAIN_BUDGET_S = 0.5           # ... and for at most this long (after the response was sent)
EXIT_MAX_AGE_S = 900.0         # a late close/close_all is still executed up to this age (see step 6)
EXIT_ACTIONS = ("close", "close_all")
#: a mirror-mode sync may carry an exit: never rate-limited, accepted late like an exit
#: (the engine decides what may still be opened)
SYNC_ACTION = "sync"


def _parse_ip(value: str) -> Optional[Any]:
    """``ipaddress`` object for a header/peer value (IPv4-mapped IPv6 unwrapped), else None."""
    v = (value or "").strip()
    if v.startswith("[") and "]" in v:          # "[::1]" or "[::1]:1234"
        v = v[1:v.index("]")]
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that carries a reference to its :class:`WebhookServer`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Tuple[str, int], app: "WebhookServer"):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.app = app
        super().__init__(addr, _Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.exception("unhandled error serving %s", client_address[0] if client_address else "?")


class _Handler(BaseHTTPRequestHandler):
    """Request handler; all logic lives in :class:`WebhookServer`."""

    server_version = "tvbridge"
    sys_version = ""
    # HTTP/1.1 so "Expect: 100-continue" can be answered; every response closes the connection.
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S
    error_content_type = "application/json"
    error_message_format = '{"ok":false,"error":"HTTP_%(code)d"}'

    def handle_expect_100(self) -> bool:
        # Defer: 100 Continue is sent only after the cheap checks pass (see _read_body).
        return True

    def do_GET(self) -> None:
        self.server.app._handle_get(self)

    def do_HEAD(self) -> None:
        self.server.app._handle_get(self, head=True)

    def do_POST(self) -> None:
        self.server.app._handle_post(self)

    def _not_found(self) -> None:
        self.server.app._handle_other(self)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_found

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)


class WebhookServer:
    """HTTP ingress: authenticates, validates and queues TradingView alerts.

    ``on_signal`` (normally ``Engine.submit_signal``) is called exactly once per new
    signal id, after the signal is stored with status "queued". It may be called from
    several handler threads concurrently and must return quickly.
    """

    def __init__(self, cfg: Config, store: Any, on_signal: Callable[[Signal], None], notifier: Any = None):
        self.cfg = cfg
        self.store = store
        self.on_signal = on_signal
        self.notifier = notifier
        self.port = int(cfg.server.port)
        self._httpd = None  # type: Optional[_Server]
        self._thread = None  # type: Optional[threading.Thread]
        self._lock = threading.Lock()
        self._rate = collections.deque()  # type: Deque[float]          # authenticated requests
        self._rate_unauth = collections.deque()  # type: Deque[float]   # bad JSON / bad secret
        self._throttle = {}  # type: Dict[str, float]  # key -> monotonic time last let through
        #: monotonic time source for rate limiting/notification throttling (tests may replace it).
        self._monotonic = time.monotonic  # type: Callable[[], float]
        #: serve_forever poll interval; bounds how long stop() waits (tests lower it).
        self.poll_interval = 0.2
        self._allowed_ips = set()
        for ip in cfg.server.tradingview_ips:
            parsed = _parse_ip(ip)
            if parsed is not None:
                self._allowed_ips.add(parsed)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Bind and serve on a daemon thread. Port 0 picks a free port (see ``self.port``)."""
        if self._httpd is not None:
            return
        host = self.cfg.server.host
        httpd = _Server((host, int(self.cfg.server.port)), self)
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": self.poll_interval},
            name="tvbridge-webhook", daemon=True,
        )
        self._thread.start()
        log.info("webhook listening on http://%s:%d%s", host, self.port, self.cfg.server.path)

    def stop(self) -> None:
        """Stop serving and close the socket. Safe to call more than once."""
        httpd, thread = self._httpd, self._thread
        self._httpd, self._thread = None, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        log.info("webhook listener stopped")

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # ------------------------------------------------------------------ routing

    def _webhook_path(self, path: str) -> bool:
        base = self.cfg.server.path.rstrip("/") or "/"
        if base == "/":
            return path == "/"
        return path == base or path == base + "/"

    @staticmethod
    def _path_of(h: _Handler) -> str:
        try:
            return urlsplit(h.path).path or "/"
        except ValueError:
            return ""

    def _handle_get(self, h: _Handler, head: bool = False) -> None:
        if self._path_of(h) in (HEALTH_PATH, HEALTH_PATH + "/"):
            self._respond(h, 200, {"ok": True, "version": __version__}, head=head)
        else:
            self._respond(h, 404, {"ok": False, "error": "NOT_FOUND"}, head=head)

    def _handle_other(self, h: _Handler) -> None:
        self._refuse(h, 404, {"ok": False, "error": "NOT_FOUND"}, self._content_length(h))

    def _handle_post(self, h: _Handler) -> None:
        try:
            self._process_post(h)
        except Exception:
            log.exception("unexpected error handling webhook")
            if not getattr(h, "_tvb_responded", False):
                self._respond(h, 500, {"ok": False, "error": "INTERNAL"})

    # ------------------------------------------------------------------ the webhook

    def _process_post(self, h: _Handler) -> None:
        scfg = self.cfg.server
        if not self._webhook_path(self._path_of(h)):
            self._refuse(h, 404, {"ok": False, "error": "NOT_FOUND"}, self._content_length(h))
            return

        # 1. size
        if h.headers.get("Content-Length") is None:
            self._refuse(h, 411, {"ok": False, "error": "LENGTH_REQUIRED"}, None)
            return
        length = self._content_length(h)
        if length is None:
            self._refuse(h, 400, {"ok": False, "error": "BAD_LENGTH"}, None)
            return
        if length > int(scfg.max_body_bytes):
            self._refuse(h, 413, {"ok": False, "error": "TOO_LARGE"}, length)
            return

        # 2. source IP
        client_ip, denial = self._check_source(h)
        if denial is not None:
            self._refuse(h, 403, {"ok": False, "error": "FORBIDDEN"}, length)
            return

        # 3. body, JSON and secret (unauthenticated requests use their own budget)
        body = self._read_body(h, length)
        if body is None:
            return  # client went away or timed out; nothing sensible to answer
        received_at = clock.utcnow()
        try:
            d = decode_body(body)
        except SignalError as e:
            if not self._admit_unauth(client_ip):
                self._respond(h, 429, {"ok": False, "error": "RATE_LIMITED"})
                return
            self._reject_payload(h, e.code, e.message, client_ip, None)
            return
        if not check_secret(d, self.cfg):
            if not self._admit_unauth(client_ip):
                self._respond(h, 429, {"ok": False, "error": "RATE_LIMITED"})
                return
            self._bad_secret(h, d, client_ip)
            return

        # 3b. copy the authenticated alert to the other engines (one per MT5 account)
        if scfg.forward_to and not h.headers.get("X-Tvbridge-Forwarded"):
            threading.Thread(target=self._forward, args=(bytes(body), list(scfg.forward_to)), daemon=True).start()

        # 4. rate limit (authenticated entries only; exits are never refused for volume)
        guess = peek_action(d)
        exit_like = guess in EXIT_ACTIONS or guess in ("partial_exit", SYNC_ACTION)
        if not exit_like and not self._admit():
            self._rate_limited(client_ip, "authenticated")
            self._refuse(h, 429, {"ok": False, "error": "RATE_LIMITED"}, 0)
            return

        # 5. schema
        try:
            sig = parse_payload(d, self.cfg, received_at, exit_symbol_ok=self._ledger_has_symbol)
        except SignalError as e:
            self._reject_payload(h, e.code, e.message, client_ip, d)
            if exit_like:
                self._exit_refused(d, None, e.code, e.message)
            return

        # 6. freshness (entries fail closed, exits fail open where it is safe)
        code = check_freshness(sig, received_at, self.cfg)
        if code is not None:
            age = (received_at - sig.fired_at).total_seconds()
            msg = ("alert fired %.0f s ago (max %s s)" % (age, scfg.max_signal_age_s) if code == "STALE"
                   else "alert dated %.0f s in the future (max %s s)" % (-age, scfg.max_future_skew_s))
            if sig.action in EXIT_ACTIONS or sig.action == SYNC_ACTION:
                accept, why = self._late_exit_ok(sig, code, age)
                if not accept:
                    self._reject_payload(h, code, msg + "; " + why, client_ip, d, sig=sig)
                    self._exit_refused(d, sig, code, msg + "; " + why)
                    return
                log.warning("accepting %s %s despite %s (%s): %s", sig.action, sig.symbol or "*", code, msg, why)
                self._event("warn", "late_exit_accepted", "%s %s accepted despite %s: %s (%s)" % (
                    sig.action, sig.symbol or "*", code, msg, why), {"id": sig.id, "ip": client_ip})
            else:
                self._reject_payload(h, code, msg, client_ip, d, sig=sig)
                return

        # 7. queue (before answering, so an accepted alert is never lost)
        try:
            sig, outcome = self._store_signal(sig)
        except Exception as e:
            log.error("cannot store signal %s: %s: %s", sig.id, type(e).__name__, e)
            self._notify_throttled("store_error", "tvbridge: database error",
                                   "Could not queue an alert (%s). TradingView may retry; check disk "
                                   "space and the engine log." % type(e).__name__, "critical")
            self._respond(h, 503, {"ok": False, "error": "STORE_ERROR"})
            return
        if outcome == "duplicate":
            log.info("duplicate signal %s ignored", sig.id)
            self._event("info", "signal_duplicate", "duplicate %s %s ignored" % (sig.action, sig.symbol or "*"),
                        {"id": sig.id, "ip": client_ip})
            self._respond(h, 200, {"ok": True, "duplicate": True})
            return
        if outcome == "id_reused":
            msg = ("alert id %r was already used by another firing; this %s %s was refused. Give every alert "
                   "firing a unique id, or leave 'id' out" % (sig.raw.get("id"), sig.action, sig.symbol))
            log.warning(msg)
            self._event("warn", "id_reused", msg, {"id": sig.id, "ip": client_ip})
            self._notify_throttled("id_reused:" + (sig.symbol or "*"), "tvbridge: alert id reused", msg, "warn")
            self._respond(h, 400, {"ok": False, "error": "ID_REUSED", "message": msg})
            return
        if outcome == "rekeyed":
            msg = ("alert id %r was already used by another firing; this %s %s is processed as %s. Give every "
                   "alert firing a unique id" % (sig.raw.get("id"), sig.action, sig.symbol or "*", sig.id))
            log.warning(msg)
            self._event("warn", "id_reused", msg, {"id": sig.id, "ip": client_ip})
            self._notify_throttled("id_reused:" + (sig.symbol or "*"), "tvbridge: alert id reused", msg, "warn")

        log.info("accepted signal %s: %s %s", sig.id, sig.action, sig.symbol or "*")
        self._event("info", "signal_received", "%s %s accepted" % (sig.action, sig.symbol or "*"),
                    {"id": sig.id, "action": sig.action, "symbol": sig.symbol, "ip": client_ip})

        # 8. hand over to the engine
        try:
            self.on_signal(sig)
        except Exception:
            log.exception("on_signal failed for %s (signal stays queued)", sig.id)
        self._respond(h, 200, {"ok": True, "id": sig.id})

    def _forward(self, body: bytes, urls: List[str]) -> None:
        """POST the alert body to each other engine; a failed copy is a critical event."""
        import urllib.request

        for url in urls:
            err = ""
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, data=body, headers={
                        "Content-Type": "application/json", "X-Tvbridge-Forwarded": "1"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        resp.read()
                    err = ""
                    break
                except Exception as e:   # HTTPError (4xx from the other engine) included
                    err = "%s: %s" % (type(e).__name__, e)
                    code = getattr(e, "code", None)
                    if code is not None and 400 <= int(code) < 500:
                        break            # the other engine refused it: retrying will not help
                    time.sleep(0.5)
            if err:
                msg = "alert could not be copied to the engine at %s (%s): that account did NOT get it" % (url, err)
                log.error(msg)
                self._event("critical", "forward_failed", msg, {"url": url})
                self._notify_throttled("forward:" + url, "tvbridge: ALERT NOT COPIED", msg, "critical")

    # ------------------------------------------------------------------ queueing helpers

    def _store_signal(self, sig: Signal) -> Tuple[Signal, str]:
        """Insert ``sig``. Returns (signal as stored, "new" | "rekeyed" | "duplicate" | "id_reused")."""
        if self.store.insert_signal(sig):
            return sig, "new"
        row = self.store.get_signal_row(sig.id)
        if row is None or self._same_firing(row, sig) or not sig.raw.get("id") or sig.action == SYNC_ACTION:
            return sig, "duplicate"      # a TradingView retry (or an identical content hash)
        same_symbol = (row.get("symbol") or "").lower() == (sig.symbol or "").lower()
        if sig.action in EXIT_ACTIONS:
            new_id = "%s:%s:%s" % (sig.id, sig.tv_symbol or "*", clock.iso(sig.fired_at))
        elif not same_symbol:
            new_id = "%s:%s" % (sig.id, sig.tv_symbol)
        else:
            return sig, "id_reused"      # an entry must never run twice under one id
        resig = dataclasses.replace(sig, id=new_id)
        if self.store.insert_signal(resig):
            return resig, "rekeyed"
        row = self.store.get_signal_row(new_id)
        if row is None or self._same_firing(row, resig):
            return resig, "duplicate"
        return resig, "id_reused"

    @staticmethod
    def _same_firing(row: Dict[str, Any], sig: Signal) -> bool:
        try:
            fired = clock.from_iso(row.get("fired_at") or "")
        except (TypeError, ValueError):
            return False
        return fired == sig.fired_at and (row.get("symbol") or "").lower() == (sig.symbol or "").lower()

    def _ledger_has_symbol(self, mt5_symbol: str) -> bool:
        """True if an open ledger position is on ``mt5_symbol`` (exits for it are never refused)."""
        try:
            rows = self.store.open_ledger_positions()
        except Exception:
            return False
        return any((r.get("symbol") or "").lower() == (mt5_symbol or "").lower() for r in rows)

    def _late_exit_ok(self, sig: Signal, code: str, age: float) -> Tuple[bool, str]:
        """(accept, why) for an exit that failed the freshness check."""
        if code == "FUTURE":
            return True, "closing early only reduces risk"
        if age > EXIT_MAX_AGE_S:
            return False, "older than the %.0f s limit for late exits" % EXIT_MAX_AGE_S
        if sig.action == SYNC_ACTION:
            # the engine acts only on the newest sync per symbol and never opens on a stale one
            return True, "a sync may carry an exit; the engine decides what may still be opened"
        try:
            rows = self.store.open_ledger_positions()
        except Exception as e:
            return False, "cannot check the ledger (%s)" % type(e).__name__
        for r in rows:
            if sig.action == "close":
                if (r.get("symbol") or "").lower() != (sig.symbol or "").lower():
                    continue
                if sig.side and r.get("side") != sig.side:
                    continue
            try:
                opened = clock.from_iso(r.get("opened_at") or "")
            except (TypeError, ValueError):
                return False, "a ledger position has no valid open time"
            if opened > sig.fired_at:
                return False, "%s %s was opened after the alert fired" % (r.get("side"), r.get("symbol"))
        return True, "no position on it was opened after the alert fired"

    def _exit_refused(self, d: Dict[str, Any], sig: Optional[Signal], code: str, message: str) -> None:
        """Critical notification: an authenticated exit was refused, the position stays open."""
        if sig is not None:
            symbol, side, action = sig.symbol or "*", sig.side, sig.action
        else:
            raw_sym = d.get("symbol") or d.get("ticker") or "*"
            symbol, side, action = str(raw_sym)[:32], d.get("side"), str(d.get("action") or "exit")[:32]
        what = "%s %s%s" % (action, symbol, " side %s" % side if side else "")
        text = ("A %s alert was refused (%s: %s). Its position stays open: close it by hand in MT5 if the "
                "strategy exited." % (what, code, self._scrub_text(message)))
        self._notify_throttled("exit_refused:%s:%s" % (symbol, side or ""), "tvbridge: EXIT ALERT REFUSED", text,
                               "critical")

    # ------------------------------------------------------------------ checks

    @staticmethod
    def _content_length(h: _Handler) -> Optional[int]:
        raw = h.headers.get("Content-Length")
        if raw is None:
            return None
        raw = raw.strip()
        if not raw.isdigit():
            return None
        return int(raw)

    def _check_source(self, h: _Handler) -> Tuple[str, Optional[str]]:
        """(client_ip, denial reason or None) per the allowlist rules.

        ``X-Forwarded-For`` is only believed from a loopback peer (the ngrok agent connects
        from there); a direct client cannot claim a TradingView address with it.
        """
        scfg = self.cfg.server
        peer = h.client_address[0] if h.client_address else ""
        ip = _parse_ip(peer)
        xff_values = h.headers.get_all("X-Forwarded-For")
        if xff_values and ip is not None and ip.is_loopback:
            client = ",".join(xff_values).split(",")[-1].strip()[:64]
            if not scfg.enforce_ip_allowlist:
                return client, None
            cip = _parse_ip(client)
            if cip is not None and cip in self._allowed_ips:
                return client, None
            return client, self._ip_blocked(client, "not in server.tradingview_ips")
        if xff_values:
            log.debug("ignoring X-Forwarded-For from non-loopback peer %s", peer)

        if ip is not None and ip.is_loopback:
            if scfg.allow_local_requests:
                return peer, None
            log.warning("local request refused (server.allow_local_requests is false)")
            self._event("warn", "local_blocked", "local request refused (allow_local_requests=false)",
                        {"ip": peer})
            return peer, "local requests disabled"
        # A direct connection from another host (only possible if host is not loopback).
        if not scfg.enforce_ip_allowlist or (ip is not None and ip in self._allowed_ips):
            return peer, None
        return peer, self._ip_blocked(peer, "direct connection not in server.tradingview_ips")

    def _ip_blocked(self, client: str, why: str) -> str:
        # One event per source IP per minute: a flood against the public URL must not turn
        # into one database write per request (the engine shares the store's lock).
        if self._throttled("ip_blocked:" + client, BLOCKED_EVENT_INTERVAL_S):
            log.debug("blocked webhook request from %r: %s", client, why)
        else:
            log.warning("blocked webhook request from %r: %s", client, why)
            self._event("warn", "ip_blocked", "request from %s blocked: %s" % (client or "?", why),
                        {"ip": client})
        return why

    def _admit(self) -> bool:
        """Sliding-window limit for authenticated requests: True if this one may proceed (and counts it)."""
        return self._admit_bucket(self._rate)

    def _admit_unauth(self, client_ip: str) -> bool:
        """Separate sliding-window budget for requests failing JSON or secret checks."""
        if self._admit_bucket(self._rate_unauth):
            return True
        self._rate_limited(client_ip, "unauthenticated")
        return False

    def _admit_bucket(self, bucket: "Deque[float]") -> bool:
        now = self._monotonic()
        limit = int(self.cfg.server.rate_limit_per_min)
        with self._lock:
            while bucket and now - bucket[0] >= RATE_WINDOW_S:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def _rate_limited(self, client_ip: str, bucket: str = "authenticated") -> None:
        """Log, record an event and notify at most once per window per bucket while refusing."""
        if not self._throttled("rate_limited:" + bucket, RATE_WINDOW_S):
            limit = self.cfg.server.rate_limit_per_min
            log.warning("rate limit exceeded (%d %s requests/min); refusing requests (latest from %s)",
                        limit, bucket, client_ip)
            self._event("warn", "rate_limited", "more than %d %s webhook requests per minute; refusing (429)"
                        % (limit, bucket), {"ip": client_ip, "bucket": bucket})
            self._notify_throttled("rate_limited:" + bucket, "tvbridge: webhook rate limit",
                                   "More than %d %s webhook requests in a minute; refusing them (HTTP 429)%s."
                                   % (limit, bucket, "" if bucket == "authenticated" else
                                      " -- someone may be probing your webhook URL"), "warn")
        else:
            log.debug("rate limited request from %s", client_ip)

    # ------------------------------------------------------------------ outcomes

    def _bad_secret(self, h: _Handler, d: Dict[str, Any], client_ip: str) -> None:
        present = any(d.get(k) not in (None, "") for k in ("secret", "passphrase"))
        why = "wrong secret" if present else "missing secret"
        log.warning("webhook from %s refused: %s", client_ip, why)
        self._event("warn", "bad_secret", "webhook refused: %s" % why, {"ip": client_ip})
        self._notify_throttled("bad_secret", "tvbridge: webhook refused",
                               "An alert with a %s was refused (from %s). Check the alert message, "
                               "or someone is probing your webhook URL." % (why, client_ip), "warn")
        self._respond(h, 401, {"ok": False, "error": "UNAUTHORIZED"})

    def _reject_payload(self, h: _Handler, code: str, message: str, client_ip: str,
                        d: Optional[Dict[str, Any]], sig: Optional[Signal] = None) -> None:
        message = self._scrub_text(message)
        log.warning("signal rejected %s: %s", code, message)
        data = {"code": code, "message": message, "ip": client_ip}  # type: Dict[str, Any]
        if d is not None:
            data["payload"] = self._scrub(strip_secrets(d))
        if sig is not None:
            data["id"] = sig.id
        self._event("warn", "signal_rejected", "%s: %s" % (code, message) if message else code, data)
        body = {"ok": False, "error": code}  # type: Dict[str, Any]
        if message:
            body["message"] = message
        self._respond(h, 400, body)

    # ------------------------------------------------------------------ I/O helpers

    def _read_body(self, h: _Handler, length: int) -> Optional[bytes]:
        if self._expects_continue(h):
            h.send_response_only(100)
            h.end_headers()
        try:
            body = h.rfile.read(length) if length else b""
        except (socket.timeout, OSError) as e:
            log.warning("could not read webhook body: %s", type(e).__name__)
            h.close_connection = True
            return None
        if len(body) < length:
            log.warning("webhook body truncated (%d of %d bytes)", len(body), length)
            h.close_connection = True
            return None
        return body

    @staticmethod
    def _expects_continue(h: _Handler) -> bool:
        return (h.request_version or "") >= "HTTP/1.1" and \
            (h.headers.get("Expect", "") or "").strip().lower() == "100-continue"

    def _refuse(self, h: _Handler, status: int, payload: Dict[str, Any], length: Optional[int]) -> None:
        """Answer without processing the body, then discard what the client still sends.

        The response goes out first (no delay). Afterwards the connection is half-closed
        and the unread input drained ("lingering close"): closing a socket that still has
        unread input makes the kernel send a reset, which can destroy the response before
        the client has read it.
        """
        self._respond(h, status, payload)
        if length == 0 or self._expects_continue(h):
            return  # nothing to discard (an Expect: 100-continue client never sends the body)
        self._linger(h, DRAIN_LIMIT_BYTES if length is None else min(length, DRAIN_LIMIT_BYTES))

    @staticmethod
    def _linger(h: _Handler, limit: int) -> None:
        try:
            h.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        deadline = time.monotonic() + DRAIN_BUDGET_S
        try:
            while limit > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                h.connection.settimeout(remaining)
                chunk = h.rfile.read1(min(limit, 16384))
                if not chunk:
                    break
                limit -= len(chunk)
        except (socket.timeout, OSError, ValueError):
            pass

    def _respond(self, h: _Handler, status: int, payload: Dict[str, Any], head: bool = False) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        h._tvb_responded = True  # type: ignore[attr-defined]
        try:
            h.send_response(status)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Cache-Control", "no-store")
            h.send_header("Connection", "close")
            h.end_headers()
            if not head:
                h.wfile.write(body)
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError) as e:
            log.debug("client went away before the response was sent: %s", type(e).__name__)
        h.close_connection = True

    # ------------------------------------------------------------------ side channels

    def _event(self, level: str, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.store.log_event(level, kind, self._scrub_text(message), data)
        except Exception as e:  # the store logs its own errors; never fail a request on this
            log.error("cannot log event %s: %s", kind, type(e).__name__)

    def _throttled(self, key: str, interval_s: float) -> bool:
        """True if ``key`` was let through less than ``interval_s`` ago; otherwise record
        it as let through now and return False."""
        now = self._monotonic()
        with self._lock:
            last = self._throttle.get(key)
            if last is not None and now - last < interval_s:
                return True
            if len(self._throttle) >= MAX_THROTTLE_KEYS:
                horizon = max(NOTIFY_INTERVAL_S, RATE_WINDOW_S, BLOCKED_EVENT_INTERVAL_S)
                self._throttle = {k: t for k, t in self._throttle.items() if now - t < horizon}
                if len(self._throttle) >= MAX_THROTTLE_KEYS:   # flood of distinct IPs
                    self._throttle = {k: t for k, t in self._throttle.items() if not k.startswith("ip_blocked:")}
            self._throttle[key] = now
            return False

    def _notify_throttled(self, key: str, title: str, message: str, level: str) -> None:
        if self.notifier is None or self._throttled("notify:" + key, NOTIFY_INTERVAL_S):
            return
        try:
            self.notifier.send(title, message, level)
        except Exception:
            log.exception("notifier failed")

    def _scrub_text(self, s: str) -> str:
        secret = self.cfg.server.secret
        s = str(s)
        if secret and secret in s:
            s = s.replace(secret, "***")
        return s

    def _scrub(self, obj: Any, _depth: int = 0) -> Any:
        """Replace any occurrence of the configured secret inside strings with "***"."""
        if _depth > 32:
            return None
        if isinstance(obj, dict):
            return {self._scrub_text(k): self._scrub(v, _depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._scrub(v, _depth + 1) for v in obj]
        if isinstance(obj, str):
            return self._scrub_text(obj)
        return obj
