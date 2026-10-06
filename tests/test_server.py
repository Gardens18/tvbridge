"""Tests for tvbridge.server.WebhookServer: a real listener on 127.0.0.1 (port 0)."""

import http.client
import json
import logging
import socket
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tvbridge import __version__, clock
from tvbridge import server as server_mod
from tvbridge.config import config_from_dict
from tvbridge.notify import NullNotifier
from tvbridge.server import WebhookServer
from tvbridge.store import Store

UTC = timezone.utc
SECRET = "srv-test-secret-0123456789"
T0 = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)
TV_IP = "52.89.214.238"
TV_IP2 = "34.212.75.30"
MAX_RESPONSE_S = 0.5   # "respond within 1 s": every answer here must be well under that


def make_cfg(**server):
    s = {"secret": SECRET, "port": 0, "host": "127.0.0.1"}
    s.update(server)
    return config_from_dict({"server": s}, home="/nonexistent/tvbridge-test-home")


def alert(**overrides):
    d = {"secret": SECRET, "time": clock.iso(T0), "symbol": "OANDA:EURUSD", "price": 1.0855,
         "action": "buy", "sl": 1.0825, "tp": 1.0915}
    for k, v in overrides.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return d


_quiet = logging.NullHandler()


def setUpModule():
    # Expected warnings (blocked IPs, rejected payloads) would otherwise go to stderr via
    # logging's last-resort handler. assertLogs still captures them where tests check logs.
    logging.getLogger("tvbridge").addHandler(_quiet)


def tearDownModule():
    logging.getLogger("tvbridge").removeHandler(_quiet)


class FailingStore(Store):
    """A store whose signal insert fails like a full/locked database."""

    def insert_signal(self, sig, status="queued"):
        raise sqlite3.OperationalError("database or disk is full")


class ServerTestCase(unittest.TestCase):
    server_cfg = {}   # type: dict
    store_cls = Store

    def setUp(self):
        clock.set_clock(lambda: T0)
        self.addCleanup(clock.set_clock, None)
        self.cfg = make_cfg(**self.server_cfg)
        self.store = self.store_cls(":memory:")
        self.addCleanup(self.store.close)
        self.notifier = NullNotifier()
        self.received = []
        self.received_lock = threading.Lock()
        self.on_signal_error = None
        self.mono = [1000.0]
        self.srv = self.start_server(self.cfg)

    def start_server(self, cfg):
        srv = WebhookServer(cfg, self.store, self.on_signal, self.notifier)
        srv._monotonic = lambda: self.mono[0]
        srv.poll_interval = 0.01
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def on_signal(self, sig):
        with self.received_lock:
            self.received.append(sig)
        if self.on_signal_error is not None:
            raise self.on_signal_error

    # ---- HTTP helpers

    def request(self, method, path, body=None, headers=None):
        """One request on a fresh connection -> (status, parsed JSON or None, raw bytes)."""
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            t0 = time.monotonic()
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            elapsed = time.monotonic() - t0
        finally:
            conn.close()
        self.assertLess(elapsed, MAX_RESPONSE_S, "%s %s took %.3f s" % (method, path, elapsed))
        if raw:
            self.assertEqual(resp.getheader("Content-Type"), "application/json")
        return resp.status, (json.loads(raw.decode("utf-8")) if raw else None), raw

    def post(self, body, path="/webhook", headers=None):
        return self.request("POST", path, body, headers)

    def raw_post(self, header_list, body=None, path="/webhook"):
        """POST with full control over headers (duplicates, no Content-Length, ...)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            t0 = time.monotonic()
            conn.putrequest("POST", path)
            for k, v in header_list:
                conn.putheader(k, v)
            conn.endheaders(body)
            resp = conn.getresponse()
            raw = resp.read()
            elapsed = time.monotonic() - t0
        finally:
            conn.close()
        self.assertLess(elapsed, MAX_RESPONSE_S)
        return resp.status, (json.loads(raw.decode("utf-8")) if raw else None), raw

    def xff_post(self, xff, body=None):
        return self.post(body if body is not None else alert(), headers={"X-Forwarded-For": xff})

    # ---- assertions

    def events(self, kind=None):
        evs = self.store.recent_events(200)
        return [e for e in evs if kind is None or e["kind"] == kind]

    def assertNoSignalsStored(self):
        self.assertEqual(self.store.recent_signals(), [])
        self.assertEqual(self.received, [])


# --------------------------------------------------------------------------- routing


class HealthAndRoutingTests(ServerTestCase):
    def test_port_assigned(self):
        self.assertGreater(self.srv.port, 0)
        self.assertTrue(self.srv.running)

    def test_health(self):
        status, body, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "version": __version__})

    def test_health_with_query_or_trailing_slash(self):
        self.assertEqual(self.request("GET", "/health?x=1")[0], 200)
        self.assertEqual(self.request("GET", "/health/")[0], 200)

    def test_head_health(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request("HEAD", "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")
        finally:
            conn.close()

    def test_other_gets_404(self):
        for path in ("/", "/webhook", "/webhook/", "/healthz", "/status", "/../etc/passwd", "/health/x"):
            with self.subTest(path=path):
                status, body, _ = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(body, {"ok": False, "error": "NOT_FOUND"})

    def test_other_post_paths_404(self):
        for path in ("/", "/health", "/webhookx", "/Webhook", "/webhook//", "/webhook/x", "/x/webhook"):
            with self.subTest(path=path):
                status, body, _ = self.post(alert(), path=path)
                self.assertEqual(status, 404)
                self.assertEqual(body["error"], "NOT_FOUND")
        self.assertNoSignalsStored()

    def test_other_methods_404(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, _, _ = self.request(method, "/webhook", json.dumps(alert()).encode())
                self.assertEqual(status, 404)
        self.assertNoSignalsStored()

    def test_trailing_slash_accepted(self):
        status, body, _ = self.post(alert(), path="/webhook/")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.received), 1)

    def test_query_string_ignored(self):
        status, _, _ = self.post(alert(), path="/webhook?source=tv")
        self.assertEqual(status, 200)

    def test_never_redirects(self):
        for path in ("/webhook/", "/webhook", "/nope"):
            status, _, _ = self.post(alert(id=path), path=path)
            self.assertNotIn(status, (301, 302, 303, 307, 308))

    def test_custom_path(self):
        srv = self.start_server(make_cfg(path="/tv/hook-abc/"))
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        try:
            for path, want in (("/tv/hook-abc", 200), ("/tv/hook-abc/", 200), ("/webhook", 404)):
                conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
                conn.request("POST", path, body=json.dumps(alert(id=path)).encode())
                resp = conn.getresponse()
                resp.read()
                conn.close()
                self.assertEqual(resp.status, want, path)
        finally:
            conn.close()

    def test_server_header_does_not_leak_python_version(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            self.assertNotIn("Python", resp.getheader("Server", ""))
            self.assertEqual(resp.getheader("Connection"), "close")
        finally:
            conn.close()

    def test_log_message_goes_to_debug_logging(self):
        with self.assertLogs("tvbridge.server", level="DEBUG") as cm:
            self.request("GET", "/health")
        records = [r for r in cm.records if "GET /health" in r.getMessage()]
        self.assertTrue(records, cm.output)
        self.assertTrue(all(r.levelno == logging.DEBUG for r in records))


# --------------------------------------------------------------------------- size checks


class LengthTests(ServerTestCase):
    def test_missing_content_length_411(self):
        status, body, _ = self.raw_post([("Content-Type", "application/json")])
        self.assertEqual(status, 411)
        self.assertFalse(body["ok"])
        self.assertNoSignalsStored()

    def test_chunked_without_length_411(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            data = json.dumps(alert()).encode()
            conn.request("POST", "/webhook", body=iter([data[:10], data[10:]]), encode_chunked=True,
                         headers={"Transfer-Encoding": "chunked"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 411)
        finally:
            conn.close()
        self.assertNoSignalsStored()

    def test_body_too_large_413(self):
        big = json.dumps(alert(comment="x" * 9000)).encode()
        self.assertGreater(len(big), self.cfg.server.max_body_bytes)
        status, body, _ = self.post(big)
        self.assertEqual(status, 413)
        self.assertFalse(body["ok"])
        self.assertNoSignalsStored()

    def test_huge_declared_length_413_without_body(self):
        status, _, _ = self.raw_post([("Content-Length", str(10 ** 9))])
        self.assertEqual(status, 413)

    def test_exactly_max_body_bytes_is_accepted(self):
        d = alert()
        base = len(json.dumps(dict(d, pad="")).encode())
        d["pad"] = "p" * (self.cfg.server.max_body_bytes - base)
        data = json.dumps(d).encode()
        self.assertEqual(len(data), self.cfg.server.max_body_bytes)
        status, _, _ = self.post(data)
        self.assertEqual(status, 200)

    def test_invalid_content_length_400(self):
        for value in ("abc", "-5", "1.5"):
            with self.subTest(value=value):
                status, body, _ = self.raw_post([("Content-Length", value)])
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])

    def test_zero_length_is_bad_json(self):
        status, body, _ = self.raw_post([("Content-Length", "0")])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "BAD_JSON")

    def test_size_checked_before_ip(self):
        status, _, _ = self.raw_post([("X-Forwarded-For", "1.2.3.4")])
        self.assertEqual(status, 411)
        self.assertEqual(self.events("ip_blocked"), [])


class SmallLimitTests(ServerTestCase):
    server_cfg = {"max_body_bytes": 300}

    def test_configured_limit(self):
        self.assertEqual(self.post(alert())[0], 200)
        status, _, _ = self.post(alert(strategy="s" * 300))
        self.assertEqual(status, 413)


# --------------------------------------------------------------------------- source IP


class LocalRequestTests(ServerTestCase):
    def test_local_request_allowed(self):
        status, body, _ = self.post(alert())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


class LocalRequestsDisabledTests(ServerTestCase):
    server_cfg = {"allow_local_requests": False}

    def test_local_request_denied(self):
        status, body, _ = self.post(alert())
        self.assertEqual(status, 403)
        self.assertEqual(body, {"ok": False, "error": "FORBIDDEN"})
        self.assertNoSignalsStored()

    def test_forwarded_request_still_allowed(self):
        status, _, _ = self.xff_post(TV_IP)
        self.assertEqual(status, 200)

    def test_forwarded_request_from_unknown_ip_denied(self):
        status, _, _ = self.xff_post("8.8.8.8")
        self.assertEqual(status, 403)


class AllowlistTests(ServerTestCase):
    def test_allowlisted_ip(self):
        for i, ip in enumerate((TV_IP, TV_IP2, "54.218.53.128", "52.32.178.7")):
            with self.subTest(ip=ip):
                status, body, _ = self.xff_post(ip, alert(id="a%d" % i))
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
        self.assertEqual(len(self.received), 4)

    def test_blocked_ip(self):
        status, body, _ = self.xff_post("1.2.3.4")
        self.assertEqual(status, 403)
        self.assertEqual(body, {"ok": False, "error": "FORBIDDEN"})
        self.assertNoSignalsStored()
        evs = self.events("ip_blocked")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["level"], "warn")
        self.assertEqual(evs[0]["data"]["ip"], "1.2.3.4")

    def test_blocked_events_throttled_per_ip(self):
        for _ in range(4):
            self.assertEqual(self.xff_post("1.2.3.4")[0], 403)
        self.assertEqual(len(self.events("ip_blocked")), 1)
        self.assertEqual(self.xff_post("5.6.7.8")[0], 403)
        self.assertEqual(len(self.events("ip_blocked")), 2)
        self.mono[0] += 61
        self.assertEqual(self.xff_post("1.2.3.4")[0], 403)
        self.assertEqual(len(self.events("ip_blocked")), 3)

    def test_multi_hop_uses_last_value(self):
        # A client can forge the first hops; ngrok appends the real peer last.
        status, _, _ = self.xff_post("%s, 1.2.3.4" % TV_IP)
        self.assertEqual(status, 403)
        self.assertEqual(self.events("ip_blocked")[0]["data"]["ip"], "1.2.3.4")
        status, _, _ = self.xff_post("1.2.3.4, 10.0.0.1,%s" % TV_IP)
        self.assertEqual(status, 200)

    def test_multiple_xff_header_lines_use_last_value(self):
        body = json.dumps(alert()).encode()
        status, _, _ = self.raw_post([("Content-Length", str(len(body))), ("X-Forwarded-For", "1.2.3.4"),
                                      ("X-Forwarded-For", TV_IP)], body)
        self.assertEqual(status, 200)
        body = json.dumps(alert(id="second")).encode()
        status, _, _ = self.raw_post([("Content-Length", str(len(body))), ("X-Forwarded-For", TV_IP),
                                      ("X-Forwarded-For", "1.2.3.4")], body)
        self.assertEqual(status, 403)

    def test_whitespace_and_ipv4_mapped(self):
        self.assertEqual(self.xff_post("  %s  " % TV_IP, alert(id="w"))[0], 200)
        self.assertEqual(self.xff_post("::ffff:%s" % TV_IP, alert(id="m"))[0], 200)

    def test_garbage_or_empty_xff_blocked(self):
        for xff in ("unknown", "", "52.89.214", "%s:443" % TV_IP, "a" * 500):
            with self.subTest(xff=xff):
                status, _, _ = self.xff_post(xff)
                self.assertEqual(status, 403)
        self.assertNoSignalsStored()

    def test_xff_loopback_is_not_local(self):
        # A loopback address in XFF came through the tunnel: allowlist rules apply.
        self.assertEqual(self.xff_post("127.0.0.1")[0], 403)

    def test_ip_check_before_secret_and_json(self):
        status, _, _ = self.xff_post("1.2.3.4", b"not json")
        self.assertEqual(status, 403)
        status, _, _ = self.xff_post("1.2.3.4", alert(secret="wrong-wrong-wrong-wrong"))
        self.assertEqual(status, 403)
        self.assertEqual(self.events("bad_secret"), [])
        self.assertEqual(self.notifier.sent, [])


class AllowlistDisabledTests(ServerTestCase):
    server_cfg = {"enforce_ip_allowlist": False}

    def test_any_forwarded_ip_allowed(self):
        self.assertEqual(self.xff_post("1.2.3.4")[0], 200)
        self.assertEqual(self.events("ip_blocked"), [])


class CheckSourceUnitTests(ServerTestCase):
    """_check_source with fake handlers, for peers a loopback test socket cannot produce."""

    def fake(self, peer, headers=()):
        msg = http.client.HTTPMessage()
        for k, v in headers:
            msg[k] = v
        return mock.Mock(client_address=(peer, 50000), headers=msg)

    def test_direct_non_loopback_peer_needs_allowlist(self):
        ip, denial = self.srv._check_source(self.fake("10.0.0.5"))
        self.assertEqual(ip, "10.0.0.5")
        self.assertIsNotNone(denial)
        self.assertEqual(self.events("ip_blocked")[0]["data"]["ip"], "10.0.0.5")
        ip, denial = self.srv._check_source(self.fake(TV_IP))
        self.assertIsNone(denial)

    def test_direct_peer_without_enforcement(self):
        self.cfg.server.enforce_ip_allowlist = False
        self.assertIsNone(self.srv._check_source(self.fake("10.0.0.5"))[1])

    def test_ipv6_loopback_is_local(self):
        self.assertIsNone(self.srv._check_source(self.fake("::1"))[1])
        self.assertIsNone(self.srv._check_source(self.fake("::ffff:127.0.0.1"))[1])
        self.assertIsNone(self.srv._check_source(self.fake("127.0.0.2"))[1])
        self.cfg.server.allow_local_requests = False
        self.assertIsNotNone(self.srv._check_source(self.fake("::1"))[1])

    def test_throttle_memory_is_bounded(self):
        for i in range(3 * server_mod.MAX_THROTTLE_KEYS):
            ip = "10.%d.%d.%d" % (i // 65536, (i // 256) % 256, i % 256)
            self.assertIsNotNone(self.srv._check_source(self.fake(ip))[1])
        self.assertLessEqual(len(self.srv._throttle), server_mod.MAX_THROTTLE_KEYS)

    def test_xff_from_a_direct_peer_is_ignored(self):
        # a LAN client cannot claim a TradingView address with X-Forwarded-For
        ip, denial = self.srv._check_source(self.fake("192.168.1.50", [("X-Forwarded-For", TV_IP)]))
        self.assertEqual(ip, "192.168.1.50")
        self.assertIsNotNone(denial)
        self.assertIsNone(self.srv._check_source(self.fake(TV_IP, [("X-Forwarded-For", "1.2.3.4")]))[1])

    def test_xff_present_ignores_local_rule(self):
        self.cfg.server.allow_local_requests = False
        self.assertIsNone(self.srv._check_source(self.fake("127.0.0.1", [("X-Forwarded-For", TV_IP)]))[1])


# --------------------------------------------------------------------------- rate limit


class RateLimitTests(ServerTestCase):
    server_cfg = {"rate_limit_per_min": 3}

    def test_rate_limit_429(self):
        for i in range(3):
            self.assertEqual(self.post(alert(id="r%d" % i))[0], 200)
        status, body, _ = self.post(alert(id="r3"))
        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "RATE_LIMITED")
        self.assertEqual(len(self.received), 3)
        self.assertIsNone(self.store.get_signal_row("buy:r3"))
        self.assertEqual(len(self.events("rate_limited")), 1)

    def test_sliding_window(self):
        for i in range(2):
            self.assertEqual(self.post(alert(id="a%d" % i))[0], 200)
        self.mono[0] += 30
        self.assertEqual(self.post(alert(id="a2"))[0], 200)
        self.assertEqual(self.post(alert(id="a3"))[0], 429)
        self.mono[0] += 30   # the first two are now 60 s old
        self.assertEqual(self.post(alert(id="a4"))[0], 200)
        self.assertEqual(self.post(alert(id="a5"))[0], 200)
        self.assertEqual(self.post(alert(id="a6"))[0], 429)
        self.mono[0] += 30   # a2 expires
        self.assertEqual(self.post(alert(id="a7"))[0], 200)

    def test_authenticated_rejections_count_unauthenticated_do_not(self):
        self.assertEqual(self.post(b"garbage")[0], 400)
        self.assertEqual(self.post(alert(secret="wrong-wrong-wrong-wrong"))[0], 401)
        self.assertEqual(self.post(alert(action="hold"))[0], 400)       # authenticated: counts
        self.assertEqual(self.post(alert(id="a"))[0], 200)
        self.assertEqual(self.post(alert(id="b"))[0], 200)
        self.assertEqual(self.post(alert(id="c"))[0], 429)

    def test_wrong_secret_flood_cannot_starve_real_alerts(self):
        for _ in range(10):
            self.assertIn(self.xff_post(TV_IP, alert(secret="wrong-wrong-wrong-wrong"))[0], (401, 429))
        # once the unauthenticated budget is used up, no more events are written for junk
        self.assertEqual(len(self.events("bad_secret")), 3)
        self.assertEqual(self.xff_post(TV_IP, alert(id="real"))[0], 200)
        self.assertEqual(self.received[-1].id, "buy:real")
        self.assertTrue(any(n.title == "tvbridge: webhook rate limit" for n in self.notifier.sent))

    def test_authenticated_exits_are_never_rate_limited(self):
        for i in range(3):
            self.assertEqual(self.post(alert(id="e%d" % i))[0], 200)
        self.assertEqual(self.post(alert(id="e3"))[0], 429)
        status, body, _ = self.post(alert(action="close", id="c1"))
        self.assertEqual((status, body["id"]), (200, "close:c1"))
        status, body, _ = self.post({"secret": SECRET, "time": clock.iso(T0), "action": "close_all", "id": "all"})
        self.assertEqual(status, 200)

    def test_requests_refused_earlier_do_not_count(self):
        for _ in range(5):
            self.assertEqual(self.xff_post("1.2.3.4")[0], 403)
            self.assertEqual(self.post(b"x" * 10000)[0], 413)
            self.assertEqual(self.request("GET", "/health")[0], 200)
        for i in range(3):
            self.assertEqual(self.post(alert(id="ok%d" % i))[0], 200)

    def test_rate_limited_events_are_not_spammed(self):
        for i in range(3):
            self.post(alert(id="s%d" % i))
        for _ in range(5):
            self.assertEqual(self.post(alert(id="z"))[0], 429)
        self.assertEqual(len(self.events("rate_limited")), 1)


# --------------------------------------------------------------------------- auth & payload


class SecretTests(ServerTestCase):
    def test_bad_secret_401(self):
        status, body, raw = self.post(alert(secret="wrong-wrong-wrong-wrong"))
        self.assertEqual(status, 401)
        self.assertEqual(body, {"ok": False, "error": "UNAUTHORIZED"})
        self.assertNoSignalsStored()
        evs = self.events("bad_secret")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["level"], "warn")
        self.assertNotIn("wrong-wrong", json.dumps(evs))

    def test_missing_secret_401(self):
        status, _, _ = self.post(alert(secret=None))
        self.assertEqual(status, 401)
        self.assertIn("missing", self.events("bad_secret")[0]["message"])

    def test_passphrase_accepted(self):
        status, _, _ = self.post(alert(secret=None, passphrase=SECRET))
        self.assertEqual(status, 200)

    def test_notify_at_most_once_per_10_minutes(self):
        for _ in range(3):
            self.assertEqual(self.post(alert(secret="nope-nope-nope-nope"))[0], 401)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0].level, "warn")
        self.mono[0] += 599
        self.post(alert(secret="nope-nope-nope-nope"))
        self.assertEqual(len(self.notifier.sent), 1)
        self.mono[0] += 2
        self.post(alert(secret="nope-nope-nope-nope"))
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(len(self.events("bad_secret")), 5)

    def test_secret_checked_before_schema(self):
        status, _, _ = self.post({"secret": "bad-bad-bad-bad-bad", "action": "hold"})
        self.assertEqual(status, 401)
        self.assertEqual(self.events("signal_rejected"), [])

    def test_json_checked_before_secret(self):
        status, body, _ = self.post(b"[1,2,3]")
        self.assertEqual((status, body["error"]), (400, "BAD_JSON"))
        self.assertEqual(self.events("bad_secret"), [])

    def test_works_without_notifier(self):
        srv = WebhookServer(self.cfg, self.store, self.on_signal)
        srv.poll_interval = 0.01
        srv.start()
        self.addCleanup(srv.stop)
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        try:
            conn.request("POST", "/webhook", body=json.dumps(alert(secret="x" * 20)).encode())
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 401)
        finally:
            conn.close()


class PayloadErrorTests(ServerTestCase):
    def assertRejected(self, body, code):
        status, resp, raw = self.post(body)
        self.assertEqual(status, 400, raw)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], code)
        return resp

    def test_bad_json(self):
        for body in (b"not json", b'{"secret":"x","order":}', b"\xff\xfe", b"[]", b'"str"', b"null"):
            with self.subTest(body=body):
                self.assertRejected(body, "BAD_JSON")
        self.assertNoSignalsStored()
        self.assertTrue(self.events("signal_rejected"))

    def test_bom_body_accepted(self):
        status, _, _ = self.post(b"\xef\xbb\xbf" + json.dumps(alert()).encode())
        self.assertEqual(status, 200)

    def test_schema_codes(self):
        cases = [
            (alert(action="hold"), "BAD_ACTION"),
            (alert(action=None), "BAD_ACTION"),
            (alert(symbol=None), "NO_SYMBOL"),
            (alert(symbol="BTCUSD"), "SYMBOL_NOT_ALLOWED"),
            (alert(sl="abc"), "BAD_NUMBER"),
            (alert(price="1,0855"), "BAD_NUMBER"),
            (alert(time=None), "NO_TIME"),
            (alert(time="{{timenow}}"), "NO_TIME"),
            (alert(order="{broken"), "BAD_JSON"),
        ]
        for body, code in cases:
            with self.subTest(code=code, body=body):
                resp = self.assertRejected(body, code)
                self.assertIn("message", resp)
        self.assertNoSignalsStored()
        evs = self.events("signal_rejected")
        self.assertEqual(len(evs), len(cases))
        self.assertEqual({e["data"]["code"] for e in evs}, {c for _, c in cases})
        for e in evs:
            self.assertNotIn("secret", e["data"].get("payload", {}))

    def test_stale_and_future(self):
        resp = self.assertRejected(alert(time=clock.iso(T0 - timedelta(seconds=121))), "STALE")
        self.assertIn("121", resp["message"])
        self.assertRejected(alert(time=clock.iso(T0 + timedelta(seconds=31))), "FUTURE")
        self.assertEqual(self.post(alert(time=clock.iso(T0 - timedelta(seconds=119)), id="ok1"))[0], 200)
        self.assertEqual(self.post(alert(time=clock.iso(T0 + timedelta(seconds=29)), id="ok2"))[0], 200)
        codes = [e["data"]["code"] for e in self.events("signal_rejected")]
        self.assertEqual(sorted(codes), ["FUTURE", "STALE"])
        self.assertEqual(len(self.received), 2)

    def test_freshness_uses_frozen_clock(self):
        clock.set_clock(lambda: T0 + timedelta(minutes=10))
        self.assertRejected(alert(), "STALE")

    def test_secret_never_echoed_or_logged(self):
        with self.assertLogs("tvbridge", level="DEBUG") as cm:
            responses = [
                self.post(alert(sl=SECRET))[2],                      # BAD_NUMBER echoes the value
                self.post(alert(symbol=SECRET))[2],                  # SYMBOL_NOT_ALLOWED
                self.post(alert(time=SECRET))[2],                    # NO_TIME
                self.post(alert(action=SECRET))[2],                  # BAD_ACTION
                self.post(alert(comment=SECRET[:20], id="c"))[2],    # accepted
                self.post(alert(secret=SECRET + "x"))[2],            # 401
            ]
        for raw in responses:
            self.assertNotIn(SECRET.encode(), raw)
            self.assertNotIn(SECRET[:20].encode(), raw)
        for line in cm.output:
            self.assertNotIn(SECRET, line)
        dumped = json.dumps(self.store.recent_events(100))
        self.assertNotIn(SECRET, dumped)
        for row in self.store.recent_signals(10):
            self.assertNotIn(SECRET, json.dumps(row))
            self.assertNotIn("secret", row["payload"]["raw"])


# --------------------------------------------------------------------------- exits fail open


class ExitIngressTests(ServerTestCase):
    def close(self, **kw):
        d = {"secret": SECRET, "time": clock.iso(T0), "symbol": "EURUSD", "action": "close"}
        d.update(kw)
        return d

    def critical(self):
        return [n for n in self.notifier.sent if n.level == "critical"]

    def test_future_dated_close_is_accepted(self):
        status, body, _ = self.post(self.close(time=clock.iso(T0 + timedelta(seconds=40)), id="f"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.received[-1].action, "close")
        self.assertTrue(self.events("late_exit_accepted"))
        # a future-dated entry is still refused
        self.assertEqual(self.post(alert(time=clock.iso(T0 + timedelta(seconds=40))))[0], 400)

    def test_late_close_accepted_when_nothing_newer_is_open(self):
        self.store.add_position("buy:x", "EURUSD.h", "buy", 0.5, 1.08, 1.07, None, 100.0,
                                T0 - timedelta(minutes=30), ticket="1")
        status, _, _ = self.post(self.close(time=clock.iso(T0 - timedelta(minutes=5)), id="late"))
        self.assertEqual(status, 200)
        self.assertEqual(self.received[-1].id, "close:late")

    def test_late_close_refused_when_a_newer_position_exists(self):
        self.store.add_position("buy:y", "EURUSD.h", "buy", 0.5, 1.08, 1.07, None, 100.0,
                                T0 - timedelta(minutes=1), ticket="2")
        status, body, _ = self.post(self.close(time=clock.iso(T0 - timedelta(minutes=5)), id="late2"))
        self.assertEqual((status, body["error"]), (400, "STALE"))
        self.assertIn("opened after", body["message"])
        self.assertEqual(len(self.critical()), 1)
        self.assertIn("EURUSD", self.critical()[0].message)
        self.assertEqual(self.received, [])

    def test_very_late_close_refused_with_notification(self):
        status, body, _ = self.post(self.close(time=clock.iso(T0 - timedelta(minutes=20))))
        self.assertEqual(status, 400)
        self.assertEqual(self.critical()[0].title, "tvbridge: EXIT ALERT REFUSED")

    def test_stale_entry_is_still_refused_silently(self):
        self.assertEqual(self.post(alert(time=clock.iso(T0 - timedelta(seconds=200))))[0], 400)
        self.assertEqual(self.critical(), [])

    def test_close_for_symbol_without_spec_but_with_open_position(self):
        status, _, _ = self.post(self.close(symbol="BTCUSD", id="b1"))
        self.assertEqual(status, 400)
        self.assertEqual(len(self.critical()), 1)        # refused exit -> critical notification
        self.store.add_position(None, "BTCUSD.h", "buy", 0.1, 60000.0, 59000.0, None, 100.0, T0, ticket="9")
        status, body, _ = self.post(self.close(symbol="BTCUSD", id="b2"))
        self.assertEqual(status, 200, body)

    def test_partial_exit_refused_with_notification(self):
        d = {"secret": SECRET, "time": clock.iso(T0), "symbol": "EURUSD", "price": 1.0855, "action": "sell",
             "market_position": "long", "sl": 1.09}
        status, body, _ = self.post(d)
        self.assertEqual((status, body["error"]), (400, "BAD_ACTION"))
        self.assertIn("PARTIAL_EXIT", body["message"])
        self.assertEqual(len(self.critical()), 1)


class IdReuseTests(ServerTestCase):
    def test_same_id_on_another_symbol_later_is_not_a_duplicate_for_exits(self):
        c1 = {"secret": SECRET, "time": clock.iso(T0), "symbol": "EURUSD", "action": "close", "id": "Long"}
        self.assertEqual(self.post(c1)[1]["id"], "close:Long")
        clock.set_clock(lambda: T0 + timedelta(hours=5))
        c2 = dict(c1, symbol="GBPUSD", time=clock.iso(T0 + timedelta(hours=5)))
        status, body, _ = self.post(c2)
        self.assertEqual(status, 200)
        self.assertNotEqual(body.get("duplicate"), True)
        self.assertTrue(body["id"].startswith("close:Long:GBPUSD:"), body)
        self.assertEqual([s.symbol for s in self.received], ["EURUSD.h", "GBPUSD.h"])
        self.assertTrue(self.events("id_reused"))
        # TradingView retrying that second alert is still a duplicate
        self.assertEqual(self.post(c2)[1], {"ok": True, "duplicate": True})

    def test_entry_reusing_an_id_on_the_same_symbol_is_refused(self):
        self.assertEqual(self.post(alert(id="Long"))[0], 200)
        clock.set_clock(lambda: T0 + timedelta(hours=1))
        status, body, _ = self.post(alert(id="Long", time=clock.iso(T0 + timedelta(hours=1))))
        self.assertEqual((status, body["error"]), (400, "ID_REUSED"))
        self.assertEqual(len(self.received), 1)
        self.assertTrue(any(n.title == "tvbridge: alert id reused" for n in self.notifier.sent))

    def test_entry_reusing_an_id_on_another_symbol_is_processed(self):
        self.assertEqual(self.post(alert(id="L"))[0], 200)
        status, body, _ = self.post(alert(id="L", symbol="GBPUSD", price=1.30, sl=1.29, tp=1.32))
        self.assertEqual((status, body["id"]), (200, "buy:L:GBPUSD"))


# --------------------------------------------------------------------------- queueing


class QueueTests(ServerTestCase):
    def test_accepted_signal_stored_then_handed_over(self):
        seen_in_store = []

        def check(sig):
            seen_in_store.append(self.store.get_signal_row(sig.id))
            self.on_signal(sig)

        self.srv.on_signal = check
        status, body, _ = self.post(alert(id="q1"))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "id": "buy:q1"})
        self.assertEqual(len(self.received), 1)
        sig = self.received[0]
        self.assertEqual((sig.id, sig.action, sig.symbol, sig.sl), ("buy:q1", "buy", "EURUSD.h", 1.0825))
        self.assertEqual(sig.received_at, T0)
        self.assertEqual(seen_in_store[0]["status"], "queued")
        self.assertEqual(self.store.load_signal("buy:q1"), sig)
        self.assertEqual(len(self.events("signal_received")), 1)

    def test_duplicate_returns_200_duplicate(self):
        self.assertEqual(self.post(alert())[1]["ok"], True)
        status, body, _ = self.post(alert())
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "duplicate": True})
        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.store.recent_signals()), 1)
        self.assertEqual(len(self.events("signal_duplicate")), 1)

    def test_duplicate_with_provided_id(self):
        self.assertEqual(self.post(alert(id="X", price=1.0))[1]["id"], "buy:X")
        self.assertEqual(self.post(alert(id="X", price=2.0))[1], {"ok": True, "duplicate": True})
        # Same id with another action is a different signal.
        self.assertEqual(self.post(alert(id="X", action="close"))[1]["id"], "close:X")
        self.assertEqual([s.id for s in self.received], ["buy:X", "close:X"])

    def test_on_signal_called_once_per_unique_signal(self):
        bodies = [alert(), alert(), alert(time=clock.iso(T0 - timedelta(seconds=5))), alert(id="u"),
                  alert(id="u"), alert(action="close"), alert(action="close"),
                  {"secret": SECRET, "time": clock.iso(T0), "action": "close_all"}]
        for b in bodies:
            self.assertEqual(self.post(b)[0], 200)
        ids = [s.id for s in self.received]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)

    def test_concurrent_duplicates_handed_over_once(self):
        results = []
        lock = threading.Lock()
        data = json.dumps(alert(id="race")).encode()

        def worker():
            conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
            try:
                conn.request("POST", "/webhook", body=data)
                resp = conn.getresponse()
                body = json.loads(resp.read())
            finally:
                conn.close()
            with lock:
                results.append((resp.status, body))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(s == 200 for s, _ in results))
        self.assertEqual(sum(1 for _, b in results if b.get("id") == "buy:race"), 1)
        self.assertEqual(sum(1 for _, b in results if b.get("duplicate")), 7)
        self.assertEqual(len(self.received), 1)

    def test_on_signal_exception_still_200(self):
        self.on_signal_error = RuntimeError("engine queue exploded")
        with self.assertLogs("tvbridge.server", level="ERROR"):
            status, body, _ = self.post(alert(id="boom"))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "id": "buy:boom"})
        self.assertEqual(self.store.get_signal_row("buy:boom")["status"], "queued")

    def test_close_all_and_strategy_order(self):
        body = {"secret": SECRET, "time": clock.iso(T0), "symbol": "EURUSD", "price": 1.0855,
                "order": json.dumps({"action": "sell", "sl": 1.0885, "strategy": "pine"})}
        status, resp, _ = self.post(body)
        self.assertEqual(status, 200)
        sig = self.received[-1]
        self.assertEqual((sig.action, sig.sl, sig.strategy), ("sell", 1.0885, "pine"))
        status, resp, _ = self.post({"secret": SECRET, "time": clock.iso(T0), "action": "flatten"})
        self.assertEqual(status, 200)
        self.assertEqual((self.received[-1].action, self.received[-1].symbol), ("close_all", ""))

    def test_response_time_well_under_one_second(self):
        # Every helper call asserts < MAX_RESPONSE_S; here many in a row, mixed outcomes.
        t0 = time.monotonic()
        for i in range(10):
            self.post(alert(id="t%d" % i))
            self.post(alert(id="t%d" % i))
            self.post(b"{bad")
            self.request("GET", "/health")
        self.assertLess(time.monotonic() - t0, 40 * MAX_RESPONSE_S)


class StoreFailureTests(ServerTestCase):
    store_cls = FailingStore

    def test_store_error_503(self):
        with self.assertLogs("tvbridge.server", level="ERROR"):
            status, body, _ = self.post(alert())
        self.assertEqual(status, 503)
        self.assertEqual(body, {"ok": False, "error": "STORE_ERROR"})
        self.assertEqual(self.received, [])
        self.assertEqual([s.level for s in self.notifier.sent], ["critical"])

    def test_store_error_notification_throttled(self):
        with self.assertLogs("tvbridge.server", level="ERROR"):
            for _ in range(3):
                self.assertEqual(self.post(alert())[0], 503)
        self.assertEqual(len(self.notifier.sent), 1)


# --------------------------------------------------------------------------- connection handling


class ConnectionTests(ServerTestCase):
    def read_response(self, sock):
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return data
            data += chunk

    def test_expect_100_continue(self):
        body = json.dumps(alert(id="expect")).encode()
        with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) as s:
            s.sendall(b"POST /webhook HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                      b"Content-Length: %d\r\nExpect: 100-continue\r\n\r\n" % len(body))
            interim = b""
            while b"\r\n\r\n" not in interim:
                chunk = s.recv(1024)
                self.assertTrue(chunk)
                interim += chunk
            self.assertTrue(interim.startswith(b"HTTP/1.1 100"), interim)
            rest = interim.split(b"\r\n\r\n", 1)[1]
            s.sendall(body)
            resp = rest + self.read_response(s)
        self.assertTrue(resp.startswith(b"HTTP/1.1 200"), resp)
        self.assertIn(b'"id":"buy:expect"', resp)

    def test_expect_100_refused_without_waiting_for_body(self):
        with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) as s:
            t0 = time.monotonic()
            s.sendall(b"POST /webhook HTTP/1.1\r\nHost: localhost\r\nX-Forwarded-For: 1.2.3.4\r\n"
                      b"Content-Length: 100\r\nExpect: 100-continue\r\n\r\n")
            resp = self.read_response(s)
            self.assertLess(time.monotonic() - t0, MAX_RESPONSE_S)
        self.assertTrue(resp.startswith(b"HTTP/1.1 403"), resp)

    def test_idle_connection_does_not_block_others(self):
        with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5):
            self.assertEqual(self.request("GET", "/health")[0], 200)
            self.assertEqual(self.post(alert())[0], 200)

    def test_truncated_body_gets_no_answer_and_is_not_queued(self):
        with mock.patch.object(server_mod._Handler, "timeout", 0.2):
            with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) as s:
                s.sendall(b"POST /webhook HTTP/1.1\r\nHost: localhost\r\nContent-Length: 500\r\n\r\n{\"a\":")
                resp = self.read_response(s)
        self.assertEqual(resp, b"")
        self.assertNoSignalsStored()

    def test_malformed_request_line(self):
        for line in (b"GARBAGE", b"POST /webhook HTTP/9.x", b"POST"):
            with self.subTest(line=line):
                with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) as s:
                    s.sendall(line + b"\r\n\r\n")
                    resp = self.read_response(s)
                self.assertIn(b'"error":"HTTP_400"', resp)
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertNoSignalsStored()

    def test_stop_is_idempotent_and_closes_port(self):
        port = self.srv.port
        self.srv.stop()
        self.srv.stop()
        self.assertFalse(self.srv.running)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_start_twice_is_noop(self):
        port = self.srv.port
        self.srv.start()
        self.assertEqual(self.srv.port, port)


if __name__ == "__main__":
    unittest.main()
