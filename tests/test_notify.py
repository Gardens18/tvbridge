import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tvbridge import notify
from tvbridge.config import NotifyCfg
from tvbridge.notify import Notifier, NullNotifier, Sent, applescript_quote, normalize_level


class LevelTests(unittest.TestCase):
    def test_normalize_level(self):
        self.assertEqual(normalize_level("WARN"), "warn")
        self.assertEqual(normalize_level("warning"), "warn")
        self.assertEqual(normalize_level("error"), "critical")
        self.assertEqual(normalize_level("bogus"), "info")
        self.assertEqual(normalize_level(None), "info")

    def test_null_notifier_records_everything_by_default(self):
        n = NullNotifier()
        n.send("t", "debug msg", "debug")
        n.send("t", "info msg")
        self.assertEqual(n.sent, [Sent("t", "debug msg", "debug"), Sent("t", "info msg", "info")])
        title, message, level = n.sent[0]
        self.assertEqual((title, message, level), ("t", "debug msg", "debug"))
        self.assertEqual(n.messages("info"), ["info msg"])

    def test_min_level_filtering(self):
        n = NullNotifier(NotifyCfg(macos=False, min_level="warn"))
        n.send("a", "dbg", "debug")
        n.send("a", "inf", "info")
        n.send("a", "wrn", "warn")
        n.send("a", "wrn2", "warning")
        n.send("a", "crit", "critical")
        self.assertEqual([s.message for s in n.sent], ["wrn", "wrn2", "crit"])
        self.assertEqual([s.level for s in n.sent], ["warn", "warn", "critical"])

    def test_min_level_info_drops_debug(self):
        n = NullNotifier(NotifyCfg(min_level="info"))
        n.send("a", "dbg", "debug")
        n.send("a", "inf", "info")
        self.assertEqual(n.messages(), ["inf"])

    def test_min_level_critical(self):
        n = NullNotifier(NotifyCfg(min_level="critical"))
        for lv in ("debug", "info", "warn"):
            n.send("a", lv, lv)
        n.send("a", "KILL", "critical")
        self.assertEqual(n.messages(), ["KILL"])
        self.assertTrue(n.enabled_for("critical"))
        self.assertFalse(n.enabled_for("warn"))

    def test_null_notifier_is_a_notifier(self):
        self.assertIsInstance(NullNotifier(), Notifier)

    def test_sent_access_styles(self):
        s = Sent("T", "M", "warn")
        self.assertEqual(s, ("T", "M", "warn"))
        self.assertEqual((s.title, s[1], s["level"], s.get("message"), s.get("nope", 1)), ("T", "M", "warn", "M", 1))
        with self.assertRaises(KeyError):
            s["nope"]

    def test_accepts_full_config_or_none(self):
        from pathlib import Path
        from tvbridge.config import config_from_dict
        cfg = config_from_dict({"server": {"secret": "x" * 20}, "notify": {"min_level": "warn", "macos": False}},
                               Path("/nonexistent"))
        n = NullNotifier(cfg)  # type: ignore[arg-type]
        self.assertEqual(n.min_level, "warn")
        self.assertEqual(Notifier(None).min_level, "info")


class MacOsChannelTests(unittest.TestCase):
    def test_applescript_quote(self):
        self.assertEqual(applescript_quote('say "hi" \\ bye'), 'say \\"hi\\" \\\\ bye')
        self.assertEqual(applescript_quote("a\nb"), "a b")

    def test_osascript_invocation_and_escaping(self):
        n = Notifier(NotifyCfg(macos=True, min_level="info"))
        with mock.patch.object(notify.subprocess, "run") as run:
            n.send('Ti"tle', 'Filled "EURUSD.h" at 1.0850 \\ ok', "info")
            n.flush()
        run.assert_called_once()
        args = run.call_args[0][0]
        self.assertEqual(args[0:2], ["osascript", "-e"])
        self.assertEqual(args[2], 'display notification "Filled \\"EURUSD.h\\" at 1.0850 \\\\ ok" '
                                  'with title "Ti\\"tle"')
        self.assertEqual(run.call_args[1].get("timeout"), 5.0)

    def test_macos_disabled_or_below_level(self):
        with mock.patch.object(notify.subprocess, "run") as run:
            n = Notifier(NotifyCfg(macos=False))
            n.send("t", "m", "critical")
            n.flush()
            n2 = Notifier(NotifyCfg(macos=True, min_level="warn"))
            n2.send("t", "m", "info")
            n2.flush()
        run.assert_not_called()

    def test_osascript_failures_never_raise(self):
        n = Notifier(NotifyCfg(macos=True))
        for exc in (FileNotFoundError("osascript"), subprocess.TimeoutExpired("osascript", 5), RuntimeError("x")):
            with mock.patch.object(notify.subprocess, "run", side_effect=exc):
                with self.assertLogs("tvbridge.notify", level="WARNING") as cm:
                    n.send("t", "m", "critical")
                    n.flush()
                self.assertIn(type(exc).__name__, "\n".join(cm.output))


class _Capture(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class NtfyChannelTests(unittest.TestCase):
    def setUp(self):
        _Capture.received = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/tvbridge-topic" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_ntfy_post_with_priority(self):
        n = Notifier(NotifyCfg(macos=False, ntfy_url=self.url, min_level="debug"))
        n.send("Kill switch", "equity below floor", "critical")
        n.flush()
        n.send("Rejected", "SL_MISSING", "warn")
        n.flush()
        n.send("Filled", "EURUSD.h buy 0.50", "info")
        n.flush()
        n.send("Tit→le", "unicode ✓ body", "debug")
        n.flush()
        self.assertEqual(len(_Capture.received), 4)
        prios = [r[1].get("Priority") for r in _Capture.received]
        self.assertEqual(prios[:3], ["5", "4", "3"])
        path, headers, body = _Capture.received[0]
        self.assertEqual(path, "/tvbridge-topic")
        self.assertEqual(headers.get("Title"), "Kill switch")
        self.assertEqual(body, b"equity below floor")
        self.assertEqual(_Capture.received[3][2].decode("utf-8"), "unicode ✓ body")


class NeverRaisesTests(unittest.TestCase):
    def test_bad_ntfy_urls_never_raise(self):
        for url in ("not a url", "http://127.0.0.1:1/unreachable", "ftp://x", "http://[bad"):
            n = Notifier(NotifyCfg(macos=False, ntfy_url=url, min_level="debug"))
            with self.assertLogs("tvbridge.notify", level="WARNING"):
                n.send("t", "m", "critical")
                n.flush()

    def test_send_returns_quickly(self):
        # fire-and-forget: send() itself does not wait for the channel
        started = threading.Event()
        release = threading.Event()

        def slow(*a, **k):
            started.set()
            release.wait(5)

        n = Notifier(NotifyCfg(macos=True))
        with mock.patch.object(notify.subprocess, "run", side_effect=slow):
            n.send("t", "m")
            self.assertTrue(started.wait(2))
            release.set()
            n.flush()

    def test_non_string_inputs(self):
        n = NullNotifier()
        n.send(123, {"a": 1}, "info")  # type: ignore[arg-type]
        self.assertEqual(n.sent[0].title, "123")


class TelegramChannelTests(unittest.TestCase):
    TOKEN = "123456:SECRET-TOKEN-abc"

    def test_telegram_request(self):
        calls = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            calls.append((req, timeout))
            return Resp()

        n = Notifier(NotifyCfg(macos=False, telegram_bot_token=self.TOKEN, telegram_chat_id="42"))
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=fake_urlopen):
            n.send("Filled", "EURUSD.h", "info")
            n.flush()
            n.send("Long", "x" * 10000, "info")
            n.flush()
        self.assertEqual(len(calls), 2)
        req, timeout = calls[0]
        self.assertEqual(req.full_url, "https://api.telegram.org/bot%s/sendMessage" % self.TOKEN)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data.decode()), {"chat_id": "42", "text": "Filled\nEURUSD.h"})
        self.assertEqual(timeout, 5.0)
        long_text = json.loads(calls[1][0].data.decode())["text"]
        self.assertLessEqual(len(long_text), notify.MAX_REMOTE_CHARS)
        self.assertTrue(long_text.endswith("..."))

    def test_telegram_requires_both_settings(self):
        with mock.patch.object(notify.urllib.request, "urlopen") as op:
            n = Notifier(NotifyCfg(macos=False, telegram_bot_token=self.TOKEN, telegram_chat_id=""))
            n.send("t", "m", "critical")
            n.flush()
        op.assert_not_called()

    def test_telegram_failure_does_not_log_token(self):
        n = Notifier(NotifyCfg(macos=False, telegram_bot_token=self.TOKEN, telegram_chat_id="42"))
        err = OSError("failed for https://api.telegram.org/bot%s/sendMessage" % self.TOKEN)
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=err):
            with self.assertLogs("tvbridge.notify", level="WARNING") as cm:
                n.send("t", "m", "critical")
                n.flush()
        self.assertNotIn(self.TOKEN, "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
