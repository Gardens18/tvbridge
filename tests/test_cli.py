"""CLI tests: every command runs against a temp TVBRIDGE_HOME.

GUI commands use the simulated MetaTrader from tests/fakes.py (``cli._make_driver`` is
patched), never the real desktop. ``run`` is exercised in a subprocess with HOME and
TVBRIDGE_HOME pointing at temp folders, in paper mode, and stopped with SIGTERM.
"""

import http.client
import io
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from tests.fakes import FakeMt5Driver, make_calibration
from tvbridge import cli, clock
from tvbridge.config import SECRET_PLACEHOLDER, deep_merge, load_config
from tvbridge.engine import Engine
from tvbridge.executors.paper import PaperExecutor
from tvbridge.gui.calibration import save_calibration
from tvbridge.notify import NullNotifier
from tvbridge.store import Store

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 6, 7, 0, 0, tzinfo=timezone.utc)   # Tue 10:00 server time (UTC+3)

_QUIET = logging.NullHandler()


def setUpModule() -> None:
    logging.getLogger("tvbridge").addHandler(_QUIET)


def tearDownModule() -> None:
    logging.getLogger("tvbridge").removeHandler(_QUIET)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class CliTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tvb-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Path(self.tmp) / "data"            # does not exist before `init`
        env = mock.patch.dict(os.environ, {"TVBRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.now = T0
        clock.set_clock(lambda: self.now)
        self.addCleanup(clock.set_clock, None)
        self.engine = None
        self.store = None

    def tearDown(self) -> None:
        if self.engine is not None:
            self.engine.stop(timeout=5)
        if self.store is not None:
            self.store.close()

    def cli(self, *argv, answer=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if answer is None:
                code = cli.main(list(argv))
            else:
                with mock.patch("builtins.input", return_value=answer):
                    code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    def init_home(self, **overrides):
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        overrides = deep_merge({"notify": {"macos": False}}, overrides)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.config_path.write_text(json.dumps(deep_merge(data, overrides), indent=2), encoding="utf-8")
        return load_config()

    def start_engine(self, **overrides):
        """An in-process paper engine on a free port, configured from config.json."""
        cfg = self.init_home(**deep_merge({"server": {"port": free_port()},
                                           "executor": {"gui": {"account_poll_s": 3600}}}, overrides))
        self.store = Store(cfg.db_path)
        self.engine = Engine(cfg, self.store, PaperExecutor(cfg, self.store), NullNotifier())
        self.engine.tick_s = 0.02
        self.engine.start()
        return cfg

    def open_store(self, cfg):
        if self.store is None:
            self.store = Store(cfg.db_path)
        return self.store


class InitTests(CliTestBase):
    def test_init_creates_private_home_and_config_with_random_secret(self):
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(str(self.home)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.config_path)).st_mode), 0o600)
        cfg = load_config()
        self.assertGreaterEqual(len(cfg.server.secret), 32)
        self.assertNotEqual(cfg.server.secret, SECRET_PLACEHOLDER)
        self.assertNotIn(cfg.server.secret, out + err)            # never printed
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("_comment", data)                           # example comments kept
        self.assertEqual(cfg.executor.mode, "paper")
        self.assertIn(str(self.config_path), out)

    def test_init_is_idempotent_and_never_overwrites(self):
        self.cli("init")
        secret = load_config().server.secret
        code, out, _ = self.cli("init")
        self.assertEqual(code, 0)
        self.assertIn("left unchanged", out)
        self.assertEqual(load_config().server.secret, secret)

    def test_init_replaces_a_hand_copied_placeholder(self):
        self.home.mkdir(parents=True)
        shutil.copy(str(ROOT / "config.example.json"), str(self.config_path))
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(load_config().server.secret, SECRET_PLACEHOLDER)

    def test_init_prints_webhook_url_when_domain_is_set(self):
        self.init_home(ngrok={"domain": "me.ngrok-free.app"})
        code, out, _ = self.cli("init")
        self.assertIn("https://me.ngrok-free.app/webhook", out)

    def test_command_without_config_is_a_config_error(self):
        code, out, err = self.cli("status")
        self.assertEqual(code, 2)
        self.assertIn("tvbridge init", err)


class StateCommandTests(CliTestBase):
    def test_status_reads_the_database_without_an_engine(self):
        cfg = self.init_home()
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("NOT RUNNING", out)
        self.assertIn("mode:        paper", out)
        self.assertIn("no snapshot yet", out)
        self.assertIn("NO_DAY_REFERENCE", out)
        store = self.open_store(cfg)
        store.set_day_state(date(2026, 10, 6), 50000.0, 50000.0, "startup")
        store.add_position("buy:x", "EURUSD.h", "buy", 0.5, 1.1, 1.098, None, 120.0, T0, ticket="P1")
        store.set_kv("halted", "UNCERTAIN_EXECUTION: test")
        code, out, _ = self.cli("status")
        self.assertIn("HALTED: UNCERTAIN_EXECUTION: test", out)
        self.assertIn("kill 48,150.00", out)
        self.assertIn("1 open in the ledger", out)
        code, out, _ = self.cli("status", "--json")
        self.assertEqual(code, 0)
        st = json.loads(out)
        self.assertFalse(st["engine"]["running"])
        self.assertEqual(st["halted"], "UNCERTAIN_EXECUTION: test")
        self.assertEqual(len(st["open_positions"]), 1)
        self.assertAlmostEqual(st["floors"]["entry_floor"], 48500.0)

    def test_status_with_running_engine(self):
        self.start_engine()
        code, out, _ = self.cli("status", "--json")
        st = json.loads(out)
        self.assertTrue(st["engine"]["running"])
        self.assertEqual(st["snapshot"]["balance"], 50000.0)
        self.assertEqual(st["day_reference"]["source"], "startup")
        code, out, _ = self.cli("status")
        self.assertIn("engine:      running", out)

    def test_pause_and_resume(self):
        cfg = self.init_home()
        code, out, _ = self.cli("pause", "--reason", "NFP")
        self.assertEqual(code, 0)
        store = self.open_store(cfg)
        self.assertEqual(store.get_kv("paused"), "1")
        self.assertEqual(store.get_kv("pause_reason"), "NFP")
        store.set_kv("halted", "UNCERTAIN_EXECUTION: test")
        code, out, _ = self.cli("resume")
        self.assertEqual(code, 0)
        self.assertIn("Cleared halt: UNCERTAIN_EXECUTION: test", out)
        for key in ("paused", "pause_reason", "halted"):
            self.assertIsNone(store.get_kv(key))
        self.assertEqual(store.get_kv("resume_ack"), clock.iso(T0))   # the engine closes invisible rows
        code, out, _ = self.cli("events", "--limit", "5")
        self.assertIn("pause", out)
        self.assertIn("resume", out)

    def test_resume_refuses_a_kill_halt_below_the_kill_floor(self):
        from tvbridge.models import AccountSnapshot
        cfg = self.init_home()
        store = self.open_store(cfg)
        store.set_day_state(date(2026, 10, 6), 50000.0, 50000.0, "startup")
        store.set_kv("halted", "KILL: equity 48100.00 <= kill floor 48150.00")
        code, out, err = self.cli("resume")                    # no snapshot at all
        self.assertEqual(code, 1)
        self.assertIn("no account snapshot", err)
        store.add_snapshot(AccountSnapshot(ts=T0, balance=50000.0, equity=48100.0, source="paper"))
        code, out, err = self.cli("resume")
        self.assertEqual(code, 1)
        self.assertIn("kill floor 48,150.00", err)
        self.assertTrue(store.get_kv("halted").startswith("KILL"))
        store.add_snapshot(AccountSnapshot(ts=T0, balance=50000.0, equity=48600.0, source="paper"))
        self.assertEqual(self.cli("resume")[0], 0)
        self.assertIsNone(store.get_kv("halted"))
        store.set_kv("halted", "KILL: again")
        store.add_snapshot(AccountSnapshot(ts=T0, balance=50000.0, equity=48000.0, source="paper"))
        self.assertEqual(self.cli("resume", "--force")[0], 0)
        self.assertIsNone(store.get_kv("halted"))

    def test_set_reference_lowering_needs_confirmation(self):
        from tvbridge.models import AccountSnapshot
        cfg = self.init_home()
        store = self.open_store(cfg)
        store.set_day_state(date(2026, 10, 6), 50250.0, 50250.0, "rollover")
        store.add_snapshot(AccountSnapshot(ts=T0, balance=50250.0, equity=50250.0, source="paper"))
        code, out, err = self.cli("set-reference", "5025.00")              # a dropped digit
        self.assertEqual(code, 1)
        self.assertIn("LOWERS", out)
        self.assertIn("refusing", err)
        self.assertEqual(store.get_day_state(date(2026, 10, 6))["reference"], 50250.0)
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.assertEqual(self.cli("set-reference", "50100", answer="no")[0], 1)
            self.assertEqual(self.cli("set-reference", "50100", answer="50100")[0], 0)
        self.assertEqual(store.get_day_state(date(2026, 10, 6))["reference"], 50100.0)
        self.assertEqual(self.cli("set-reference", "50300")[0], 0)          # raising: no question
        self.assertEqual(self.cli("set-reference", "60000")[0], 1)          # > 10 % above the account
        self.assertEqual(self.cli("set-reference", "5025", "--force")[0], 0)
        self.assertEqual(store.get_day_state(date(2026, 10, 6))["reference"], 5025.0)

    def test_set_reference(self):
        cfg = self.init_home()
        code, out, err = self.cli("set-reference", "51000")
        self.assertEqual(code, 0, err)
        store = self.open_store(cfg)
        ds = store.get_day_state(date(2026, 10, 6))            # today's server date (frozen clock)
        self.assertEqual((ds["reference"], ds["source"]), (51000.0, "manual"))
        self.assertIn("kill floor 49,110.00", out)
        self.cli("set-reference", "50500", "--date", "2026-10-07")
        self.assertEqual(store.get_day_state(date(2026, 10, 7))["reference"], 50500.0)
        self.assertEqual(self.cli("set-reference", "-5")[0], 2)
        self.assertEqual(self.cli("set-reference", "abc")[0], 2)
        self.assertEqual(self.cli("set-reference", "50000", "--date", "07/10/2026")[0], 2)

    def test_flatten_requires_confirmation(self):
        cfg = self.init_home()
        store = self.open_store(cfg)
        code, out, _ = self.cli("flatten", answer="no")
        self.assertEqual(code, 1)
        self.assertIsNone(store.get_kv("command"))
        code, out, _ = self.cli("flatten", answer="FLATTEN")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(store.get_kv("command"))["cmd"], "flatten")
        store.set_kv("command", None)
        code, out, _ = self.cli("flatten", "--yes")
        self.assertEqual(code, 0)
        cmd = json.loads(store.get_kv("command"))
        self.assertEqual(cmd["cmd"], "flatten")
        self.assertEqual(cmd["ts"], clock.iso(T0))
        self.assertIn("does not seem to be running", out)

    def test_flatten_is_executed_by_a_running_engine(self):
        cfg = self.start_engine()
        code, out, err = self.cli("send-test", "--action", "buy", "--symbol", "EURUSD", "--price", "1.1",
                                  "--sl", "1.098")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(len(self.store.open_ledger_positions()), 1)
        code, out, _ = self.cli("flatten", "--yes")
        self.assertEqual(code, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.store.open_ledger_positions():
            time.sleep(0.02)
        self.assertEqual(self.store.open_ledger_positions(), [])
        self.assertIsNone(self.store.get_kv("command"))

    def test_ngrok_config(self):
        self.init_home(ngrok={"authtoken": "2abcDEF_token_123", "domain": "me.ngrok-free.app"})
        code, out, err = self.cli("ngrok-config")
        self.assertEqual(code, 0, err)
        yml = self.home / "ngrok.yml"
        self.assertEqual(yml.read_text(encoding="utf-8"),
                         'version: "3"\nagent:\n  authtoken: "2abcDEF_token_123"\n')
        self.assertEqual(stat.S_IMODE(os.stat(str(yml)).st_mode), 0o600)
        self.assertEqual(out.strip().splitlines()[-1], "https://me.ngrok-free.app/webhook")

    def test_ngrok_config_without_token_fails(self):
        self.init_home()
        code, out, err = self.cli("ngrok-config")
        self.assertEqual(code, 1)
        self.assertIn("authtoken", err)
        self.assertFalse((self.home / "ngrok.yml").exists())


class SendTestTests(CliTestBase):
    def test_send_test_reaches_the_running_engine(self):
        self.start_engine()
        code, out, err = self.cli("send-test", "--action", "buy", "--symbol", "EURUSD", "--price", "1.10000",
                                  "--sl", "1.09800", "--tp", "1.10500")
        self.assertEqual(code, 0, out + err)
        self.assertIn("HTTP 200", out)
        self.assertNotIn(self.engine.cfg.server.secret, out + err)
        self.assertTrue(self.engine.wait_idle(10))
        led = self.store.open_ledger_positions()
        self.assertEqual(len(led), 1)
        self.assertEqual((led[0]["symbol"], led[0]["side"]), ("EURUSD.h", "buy"))
        # close_all needs no symbol; --url posts to the given address
        url = "http://127.0.0.1:%d/webhook" % self.engine.server.port
        code, out, _ = self.cli("send-test", "--action", "close_all", "--url", url)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.engine.wait_idle(10))
        self.assertEqual(self.store.open_ledger_positions(), [])

    def test_send_test_reports_http_errors(self):
        self.start_engine()
        code, out, _ = self.cli("send-test", "--action", "buy", "--symbol", "NOPE")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 400", out)
        self.assertIn("SYMBOL_NOT_ALLOWED", out)

    def test_send_test_without_engine(self):
        self.init_home(server={"port": free_port()})
        code, out, err = self.cli("send-test", "--action", "buy", "--symbol", "EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("is the engine running", err)

    def test_send_test_requires_symbol_for_entries(self):
        self.init_home()
        self.assertEqual(self.cli("send-test", "--action", "buy")[0], 2)

    def test_live_entry_needs_typed_confirmation(self):
        port = free_port()
        self.init_home(executor={"mode": "live"}, server={"port": port})
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, out, err = self.cli("send-test", "--action", "buy", "--symbol", "EURUSD")
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        # --url does not skip it, not even pointing at the local listener
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, out, err = self.cli("send-test", "--action", "buy", "--symbol", "EURUSD", "--url",
                                      "http://127.0.0.1:%d/webhook" % port)
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            code, out, err = self.cli("send-test", "--action", "buy", "--symbol", "EURUSD", "--url",
                                      "http://127.0.0.1:%d/webhook" % port, answer="no")
        self.assertEqual(code, 1)
        self.assertIn("not sent", out)


class GuiCommandTests(CliTestBase):
    """read-account / rehearse / doctor against the simulated MT5 (never the real desktop)."""

    def setUp(self) -> None:
        super().setUp()
        self.driver = FakeMt5Driver()
        patcher = mock.patch.object(cli, "_make_driver", return_value=self.driver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_account(self):
        cfg = self.init_home()
        save_calibration(cfg.calibration_path, make_calibration())
        code, out, err = self.cli("read-account")
        self.assertEqual(code, 0, err)
        self.assertIn("balance:      50,000.00", out)
        self.assertIn("positions:    0", out)

    def test_read_account_without_calibration(self):
        self.init_home()
        code, out, err = self.cli("read-account")
        self.assertEqual(code, 1)
        self.assertIn("tvbridge calibrate", err)

    def test_rehearse_never_clicks_buy_or_sell(self):
        cfg = self.init_home(executor={"mode": "live"})     # rehearse ignores the mode
        save_calibration(cfg.calibration_path, make_calibration())
        code, out, err = self.cli("rehearse", "--symbol", "EURUSD", "--side", "buy", "--sl", "1.08045",
                                  "--tp", "1.08945", "--lots", "0.5", "--price", "1.08345")
        self.assertEqual(code, 0, out + err)
        self.assertIn("result:   rehearsed", out)
        self.assertEqual(self.driver.order_button_clicks, [])
        self.assertEqual(self.driver.orders_sent, [])
        self.assertIn("0.50", self.driver.typed())

    def test_rehearse_rejects_wrong_side_stop(self):
        self.init_home()
        code, out, err = self.cli("rehearse", "--symbol", "EURUSD", "--side", "buy", "--sl", "1.09",
                                  "--price", "1.08")
        self.assertEqual(code, 2)
        self.assertEqual(self.driver.actions, [])

    def test_doctor_with_running_engine(self):
        self.start_engine()
        save_calibration(self.engine.cfg.calibration_path, make_calibration())
        self.engine._write_heartbeat()
        code, out, err = self.cli("doctor")
        self.assertEqual(code, 0, out + err)
        for needle in ("[OK  ] config", "[OK  ] secret", "[INFO] python", "[OK  ] accessibility",
                       "[OK  ] MT5 windows", "[OK  ] calibration", "[OK  ] listener", "[OK  ] heartbeat",
                       "[WARN] ngrok", "All checks passed"):
            self.assertIn(needle, out)

    def test_doctor_fails_without_engine_and_permissions_in_live_mode(self):
        self.driver.perms = {"accessibility": False, "screen_recording": False}
        self.init_home(executor={"mode": "live"}, server={"port": free_port()})
        code, out, err = self.cli("doctor", "--prompt")
        self.assertEqual(code, 1)
        self.assertIn(("request_permissions",), self.driver.actions)
        self.assertIn("[FAIL] accessibility", out)
        self.assertIn("[FAIL] calibration", out)
        self.assertIn("[FAIL] listener", out)
        self.assertIn("[FAIL] heartbeat", out)


class RunStartupFailureTests(unittest.TestCase):
    """A start that fails after the config loaded writes the error to the heartbeat and notifies once."""

    def test_failed_start_is_reported_and_throttled(self):
        tmp = tempfile.mkdtemp(prefix="tvb-runfail-")
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "data")
        env = dict(os.environ, HOME=tmp, TVBRIDGE_HOME=home, PYTHONUNBUFFERED="1")
        py = sys.executable
        subprocess.run([py, "-m", "tvbridge", "init"], cwd=str(ROOT), env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        cfg_path = Path(home) / "config.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        # live mode without account_login / calibration: make_executor refuses (no GUI is touched)
        data = deep_merge(data, {"server": {"port": free_port()}, "notify": {"macos": False},
                                 "executor": {"mode": "live"}})
        cfg_path.write_text(json.dumps(data), encoding="utf-8")
        first = subprocess.run([py, "-m", "tvbridge", "run"], cwd=str(ROOT), env=env, timeout=60,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(first.returncode, 1)
        self.assertIn("engine failed to start", first.stderr)
        hb = json.loads((Path(home) / "heartbeat.json").read_text(encoding="utf-8"))
        self.assertTrue(hb["stopped"])
        self.assertIn("ACCOUNT_LOGIN_REQUIRED", hb["error"])
        marker = Path(home) / "startup_failure.json"
        stamp = json.loads(marker.read_text(encoding="utf-8"))["ts"]
        second = subprocess.run([py, "-m", "tvbridge", "run"], cwd=str(ROOT), env=env, timeout=60,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["ts"], stamp)   # not re-notified
        status = subprocess.run([py, "-m", "tvbridge", "status"], cwd=str(ROOT), env=env, timeout=60,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertIn("last start FAILED", status.stdout)


class RunSubprocessTests(unittest.TestCase):
    """`python -m tvbridge run` in paper mode: serves, writes logs and heartbeat, stops on SIGTERM."""

    def test_run_serves_and_stops_cleanly_on_sigterm(self):
        tmp = tempfile.mkdtemp(prefix="tvb-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, HOME=tmp, TVBRIDGE_HOME=os.path.join(tmp, "data"), PYTHONUNBUFFERED="1")
        py = sys.executable
        subprocess.run([py, "-m", "tvbridge", "init"], cwd=str(ROOT), env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        cfg_path = Path(env["TVBRIDGE_HOME"]) / "config.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        port = free_port()
        data = deep_merge(data, {"server": {"port": port}, "notify": {"macos": False}})
        cfg_path.write_text(json.dumps(data), encoding="utf-8")

        proc = subprocess.Popen([py, "-m", "tvbridge", "run"], cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 30
            healthy = False
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    conn.request("GET", "/health")
                    healthy = conn.getresponse().status == 200
                    conn.close()
                except OSError:
                    pass
                if healthy:
                    break
                time.sleep(0.1)
            self.assertTrue(healthy, "engine never answered /health")
            # a second engine for the same home refuses to start
            second = subprocess.run([py, "-m", "tvbridge", "run"], cwd=str(ROOT), env=env, timeout=60,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(second.returncode, 1)
            self.assertIn("already running", second.stderr)
            caffeinate = subprocess.run(["/usr/bin/pgrep", "-f", "caffeinate -dimsu -w %d" % proc.pid],
                                        stdout=subprocess.PIPE, universal_newlines=True)
            self.assertTrue(caffeinate.stdout.strip(), "caffeinate child not running")
            proc.send_signal(signal.SIGTERM)
            _out, err = proc.communicate(timeout=40)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        time.sleep(0.3)
        leftover = subprocess.run(["/usr/bin/pgrep", "-f", "caffeinate -dimsu -w %d" % proc.pid],
                                  stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(leftover.stdout.strip(), "", "caffeinate was not terminated")
        hb = json.loads((Path(env["TVBRIDGE_HOME"]) / "heartbeat.json").read_text(encoding="utf-8"))
        self.assertTrue(hb["stopped"])
        log_file = Path(tmp) / "Library" / "Logs" / "tvbridge" / "tvbridge.log"
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("engine running", text)
        self.assertIn("received SIGTERM", text)
        self.assertNotIn(data["server"]["secret"], text)


if __name__ == "__main__":
    unittest.main()
