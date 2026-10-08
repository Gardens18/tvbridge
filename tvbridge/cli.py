"""Command line interface: ``python -m tvbridge <command>`` (SPEC section 13).

Every command reads ``$TVBRIDGE_HOME/config.json`` (default ``~/.tvbridge``). Commands that
change state while the engine runs (pause, resume, flatten, set-reference) write to the
SQLite database, which the engine reads on every decision; ``flatten`` leaves a request in
kv ``command`` that the running engine consumes within a second.

GUI commands (doctor, calibrate, read-account, rehearse) use the real macOS driver; they
never click Buy, Sell or Close (rehearse always runs the executor in rehearsal mode).
"""

import argparse
from . import _fcntl as fcntl
import json
import logging
import logging.handlers
import math
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, clock
from .config import (EXAMPLE_CONFIG_PATH, SECRET_PLACEHOLDER, Config, ConfigError, config_from_dict, default_home,
                     load_config)

log = logging.getLogger("tvbridge.cli")

LOG_FILE_NAME = "tvbridge.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
CAFFEINATE = "/usr/bin/caffeinate"
ENGINE_LOCK_NAME = "engine.lock"
STOP_TIMEOUT_S = 25.0      # launchd's ExitTimeOut is 30 s
ENTRY_ACTIONS = ("buy", "sell", "long", "short")
STARTUP_FAILURE_FILE = "startup_failure.json"
STARTUP_FAILURE_NOTIFY_S = 900.0   # launchd restarts every 10 s: notify a failing start at most every 15 min
REFERENCE_CONFIRM_REL = 0.10       # set-reference this far from the account needs confirmation


# --------------------------------------------------------------------------- small helpers


def _make_driver() -> Any:
    """The real macOS driver (tests replace this function with a fake)."""
    from .gui.driver import default_driver

    return default_driver()


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _money(v: Any) -> str:
    try:
        return "{:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return "?"


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with permissions 600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):   # not on Windows
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(str(path), 0o600)


def _open_store(cfg: Config) -> Any:
    from .store import Store

    return Store(cfg.db_path)


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url: str, data: Optional[bytes] = None, timeout: float = 10.0,
          headers: Optional[Dict[str, str]] = None) -> Any:
    """(status, body bytes) for a GET (data None) or POST. Local URLs bypass proxies.

    Raises urllib.error.URLError / OSError when the server cannot be reached.
    """
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                 headers=dict(headers or {}))
    host = urlparse(url).hostname or ""
    opener = _no_proxy_opener() if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _local_url(cfg: Config, path: str) -> str:
    return "http://127.0.0.1:%d%s" % (int(cfg.server.port), path)


def _gui_executor(cfg: Config) -> Any:
    """Mt5GuiExecutor in **rehearsal** mode (never clicks Buy/Sell/Close)."""
    from .executors.mt5gui import Mt5GuiExecutor
    from .gui.calibration import load_calibration

    calib = load_calibration(cfg.calibration_path)
    return Mt5GuiExecutor(cfg, _make_driver(), calib, rehearsal=True, shots_dir=cfg.shots_dir)


# --------------------------------------------------------------------------- init


def _example_config_text(home: Path) -> str:
    """config.example.json with a fresh random server.secret (validated)."""
    secret = secrets.token_urlsafe(32)
    placeholder = json.dumps(SECRET_PLACEHOLDER)
    try:
        text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        text = json.dumps({"_comment": "tvbridge configuration; see README.md section 9",
                           "server": {"secret": SECRET_PLACEHOLDER}}, indent=2) + "\n"
    if text.count(placeholder) == 1:
        text = text.replace(placeholder, json.dumps(secret))   # keeps the file's layout and comments
    else:
        data = json.loads(text)
        data.setdefault("server", {})["secret"] = secret
        text = json.dumps(data, indent=2) + "\n"
    config_from_dict(json.loads(text), home)   # never write a config the engine would refuse
    return text


def cmd_init(args: argparse.Namespace) -> int:
    home = default_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(home), 0o700)
    cfg_path = home / "config.json"
    if not cfg_path.exists():
        _write_private(cfg_path, _example_config_text(home))
        print("created %s with a new random server.secret" % cfg_path)
    else:
        text = cfg_path.read_text(encoding="utf-8-sig")
        placeholder = json.dumps(SECRET_PLACEHOLDER)
        if text.count(placeholder) == 1:   # copied from config.example.json by hand
            _write_private(cfg_path, text.replace(placeholder, json.dumps(secrets.token_urlsafe(32))))
            print("%s: replaced the placeholder server.secret with a new random secret" % cfg_path)
        else:
            print("config exists, left unchanged: %s" % cfg_path)
    os.chmod(str(cfg_path), 0o600)

    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        _err("WARNING: %s" % e)
        _err("Fix config.json, then run `tvbridge doctor`.")
        return 1
    calib = "present" if cfg.calibration_path.exists() else "missing (run `tvbridge calibrate` before rehearsal/live)"
    print("home:         %s" % cfg.home)
    print("config:       %s (mode 600)" % cfg_path)
    print("database:     %s" % cfg.db_path)
    print("calibration:  %s: %s" % (cfg.calibration_path, calib))
    print("logs:         %s" % cfg.log_dir)
    print("mode:         %s" % cfg.executor.mode)
    print("secret:       server.secret in config.json; paste it into every alert's \"secret\" field")
    if cfg.ngrok.domain:
        print("webhook URL:  https://%s%s" % (cfg.ngrok.domain, cfg.server.path))
    else:
        print("webhook URL:  set ngrok.domain (and ngrok.authtoken) in config.json, then re-run ./install.sh")
    return 0


# --------------------------------------------------------------------------- run


def _setup_logging(cfg: Config) -> None:
    """Rotating file log (5 MB x 5) in log_dir plus stderr.

    Under launchd stderr is captured into an unrotated file, so when stderr is not a
    terminal only warnings and errors go there; the rotating log has everything.
    """
    level_name = os.environ.get("TVBRIDGE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger("tvbridge")
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)-7s %(name)s: %(message)s")
    fmt.converter = time.gmtime   # UTC, like events, status and the database
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(str(cfg.log_dir / LOG_FILE_NAME), maxBytes=LOG_MAX_BYTES,
                                                  backupCount=LOG_BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except OSError as e:
        _err("WARNING: cannot write logs to %s: %s" % (cfg.log_dir, e))
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(level if sys.stderr.isatty() else max(level, logging.WARNING))
    root.addHandler(sh)


def _acquire_engine_lock(home: Path) -> Any:
    """Exclusive lock on <home>/engine.lock, or None if another engine holds it."""
    home.mkdir(parents=True, exist_ok=True)
    fh = open(str(home / ENGINE_LOCK_NAME), "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write("%d\n" % os.getpid())
    fh.flush()
    return fh


def _start_caffeinate() -> Optional[subprocess.Popen]:
    """Keep the Mac awake while this process lives (``caffeinate -dimsu -w <pid>``)."""
    if not os.path.exists(CAFFEINATE):
        log.warning("%s not found; the Mac may sleep", CAFFEINATE)
        return None
    try:
        return subprocess.Popen([CAFFEINATE, "-dimsu", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("cannot start caffeinate: %s", e)
        return None


def _stop_process(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _startup_failed(cfg: Config, notifier: Any, what: str, e: BaseException) -> int:
    """A start that fails after the config loaded: log, heartbeat with the error, notify (throttled
    across launchd restarts), exit 1."""
    msg = "%s: %s: %s" % (what, type(e).__name__, e)
    log.error("engine failed to start: %s", msg)
    _err("engine failed to start: %s" % msg)
    home = Path(cfg.home)
    try:
        hb = {"ts": clock.iso(clock.utcnow()), "pid": os.getpid(), "version": __version__,
              "mode": cfg.executor.mode, "stopped": True, "error": msg}
        _write_private(cfg.heartbeat_path, json.dumps(hb, indent=2, sort_keys=True) + "\n")
    except OSError as werr:
        log.warning("cannot write the heartbeat: %s", werr)
    marker = home / STARTUP_FAILURE_FILE
    last = None
    try:
        last = clock.from_iso(json.loads(marker.read_text(encoding="utf-8")).get("ts"))
    except (OSError, ValueError, TypeError, AttributeError):
        last = None
    now = clock.utcnow()
    if last is None or (now - last).total_seconds() >= STARTUP_FAILURE_NOTIFY_S:
        try:
            _write_private(marker, json.dumps({"ts": clock.iso(now), "error": msg}) + "\n")
        except OSError:
            pass
        if notifier is not None:
            try:
                notifier.send("tvbridge: ENGINE CANNOT START",
                              "The engine fails to start (%s). launchd keeps retrying every 10 s; open positions "
                              "have no kill switch until this is fixed. Run `tvbridge doctor`." % msg, "critical")
                notifier.flush(timeout=3.0)
            except Exception:  # pragma: no cover - Notifier.send never raises
                pass
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Engine
    from .executors import make_executor
    from .notify import Notifier

    cfg = load_config()
    lock = _acquire_engine_lock(Path(cfg.home))
    if lock is None:
        _err("another tvbridge engine is already running for %s (lock %s held)" % (cfg.home, ENGINE_LOCK_NAME))
        return 1
    _setup_logging(cfg)
    log.info("tvbridge %s starting (pid %d, python %s, home %s)", __version__, os.getpid(), sys.executable, cfg.home)
    notifier = Notifier(cfg.notify)
    stop_ev = threading.Event()

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("received %s, stopping", signal.Signals(signum).name)
        stop_ev.set()

    old_handlers = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    engine = None
    caffeinate = None
    store = None
    code = 0
    try:
        try:
            store = _open_store(cfg)
        except Exception as e:
            return _startup_failed(cfg, notifier, "cannot open the database %s" % cfg.db_path, e)
        try:
            executor = make_executor(cfg, store)
        except Exception as e:   # CalibrationError, ExecutorError, pyobjc import problems
            return _startup_failed(cfg, notifier, "cannot create the %s executor" % cfg.executor.mode, e)
        engine = Engine(cfg, store, executor, notifier)
        try:
            engine.start()
        except Exception as e:
            engine.stop(timeout=STOP_TIMEOUT_S)
            engine = None
            return _startup_failed(cfg, notifier, "engine start", e)
        try:
            (Path(cfg.home) / STARTUP_FAILURE_FILE).unlink()
        except OSError:
            pass
        caffeinate = _start_caffeinate()
        log.info("engine running (mode %s); SIGTERM or Ctrl-C stops it", cfg.executor.mode)
        while not stop_ev.wait(1.0):
            if not engine.threads_alive():
                log.critical("an engine thread died; exiting so launchd restarts the engine")
                code = 1
                break
            if caffeinate is not None and caffeinate.poll() is not None:
                log.warning("caffeinate exited (code %s); restarting it", caffeinate.returncode)
                caffeinate = _start_caffeinate()
        return code
    finally:
        if engine is not None:
            engine.stop(timeout=STOP_TIMEOUT_S)
        _stop_process(caffeinate)
        try:
            notifier.flush(timeout=3.0)
        except Exception:  # pragma: no cover
            pass
        if store is not None:
            store.close()
        for s, h in old_handlers.items():
            signal.signal(s, h)
        lock.close()
        log.info("tvbridge stopped")


# --------------------------------------------------------------------------- doctor


class _Checks(object):
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def add(self, status: str, name: str, detail: str) -> None:
        if status == "FAIL":
            self.failed += 1
        elif status == "WARN":
            self.warned += 1
        print("[%-4s] %-13s %s" % (status, name, detail))


def _running_image() -> str:
    """The executable macOS actually checks for this process (``ps -o comm=``)."""
    try:
        out = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(os.getpid())], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _under_terminal() -> bool:
    return bool(os.environ.get("TERM_PROGRAM")) or sys.stdin.isatty()


def cmd_doctor(args: argparse.Namespace) -> int:
    from .engine import config_warnings, engine_alive, heartbeat_age_s, read_heartbeat

    c = _Checks()
    print("tvbridge doctor %s" % __version__)
    try:
        cfg = load_config()  # type: Optional[Config]
    except ConfigError as e:
        cfg = None
        c.add("FAIL", "config", str(e))
    gui_mode = cfg is not None and cfg.executor.mode in ("rehearsal", "live")
    need = "FAIL" if gui_mode else "WARN"   # GUI prerequisites only matter outside paper mode

    if cfg is not None:
        c.add("OK", "config", str(cfg.source_path))
        c.add("OK", "secret", "%d characters" % len(cfg.server.secret))
        c.add("INFO", "mode", "%s%s" % (cfg.executor.mode, "" if gui_mode else
                                       " (MT5 checks below are warnings only in paper mode)"))
        for w in config_warnings(cfg):
            c.add("WARN", "risk config", w)
        now = clock.utcnow()
        off = cfg.server_offset_at(now)
        c.add("INFO", "server time", "%s (UTC%+g%s); compare with the MT5 Market Watch clock" % (
            clock.to_server(now, off).strftime("%a %Y-%m-%d %H:%M"), off,
            ", auto US DST" if cfg.server_offset_is_auto else ""))

    real = os.path.realpath(sys.executable)
    c.add("INFO", "python", "%s -> %s (this binary needs Accessibility and Screen Recording)"
          % (sys.executable, real))
    image = _running_image()
    if image and os.path.realpath(image) != real:
        c.add("INFO", "running image", "%s (macOS checks this one; add it too if a grant on the path above "
                                       "is not enough)" % image)
    if _under_terminal():
        c.add("INFO", "terminal", "run from a terminal (%s): macOS attributes these permission checks to the "
                                  "terminal app, not to the engine under launchd. Grant both, then restart the "
                                  "engine." % (os.environ.get("TERM_PROGRAM") or "tty"))

    driver = None
    perms = None  # type: Optional[Dict[str, bool]]
    try:
        driver = _make_driver()
        if getattr(args, "prompt", False):
            driver.request_permissions()
            print("       macOS permission prompts requested; grant them in System Settings > Privacy & Security")
        perms = dict(driver.permissions() or {})
    except Exception as e:
        c.add(need, "permissions", "cannot query the macOS driver: %s" % e)
    if perms is not None:
        for key, name, label in (("accessibility", "accessibility", "Accessibility"),
                                 ("screen_recording", "screen rec.", "Screen Recording")):
            if perms.get(key):
                c.add("OK", name, "%s granted" % label)
            else:
                c.add(need, name, "%s NOT granted (System Settings > Privacy & Security > %s)" % (label, label))

    if cfg is not None and driver is not None:
        g = cfg.executor.gui
        try:
            wins = list(driver.list_windows(list(g.owner_names)))
        except Exception as e:
            wins = []
            c.add(need, "MT5 windows", "cannot list windows: %s" % e)
        else:
            from .executors.base import ExecutorError
            from .executors.mt5gui import select_main_window
            from .gui.calibration import CalibrationError as _CalErr, load_calibration as _load_cal

            try:
                calib_for_size = _load_cal(cfg.calibration_path)   # type: Any
            except _CalErr:
                calib_for_size = None
            shown = "; ".join("%r (%s, %.0fx%.0f)" % (w.title, w.owner, w.w, w.h) for w in wins[:6])
            try:
                main = select_main_window(wins, cfg, calib_for_size)
            except ExecutorError as e:
                c.add(need, "MT5 windows", "%s%s" % (e.reason, "; seen: " + shown if wins else ""))
            else:
                c.add("OK", "MT5 windows", "main window %r; all: %s" % (main.title, shown))
                if not any(w.title for w in wins) and perms is not None and not perms.get("screen_recording"):
                    c.add("WARN", "MT5 titles", "window titles are empty: macOS hides them without Screen Recording")
        if cfg.executor.mode == "live" and not (cfg.account.account_login or "").strip():
            c.add("FAIL", "MT5 account", "account.account_login is empty: live mode refuses to start "
                                         "(ACCOUNT_LOGIN_REQUIRED)")

    if cfg is not None:
        from .gui.calibration import CalibrationError, load_calibration

        try:
            calib = load_calibration(cfg.calibration_path)
        except CalibrationError as e:
            c.add(need, "calibration", str(e))
        else:
            od = calib.order_dialog
            c.add("OK", "calibration", "%s (order window %.0fx%.0f, created %s)" % (
                cfg.calibration_path, float(od["w"]), float(od["h"]), calib.created_at or "?"))
        _doctor_ngrok(c, cfg)
        _doctor_engine(c, cfg, engine_alive, heartbeat_age_s, read_heartbeat)

    print("")
    if c.failed:
        print("%d check(s) failed, %d warning(s)." % (c.failed, c.warned))
        return 1
    print("All checks passed%s." % (" (%d warning(s))" % c.warned if c.warned else ""))
    return 0


def _doctor_ngrok(c: _Checks, cfg: Config) -> None:
    domain = cfg.ngrok.domain
    if not domain:
        c.add("WARN", "ngrok", "ngrok.domain is not set: TradingView cannot reach this Mac")
        return
    c.add("INFO", "webhook URL", "https://%s%s" % (domain, cfg.server.path))
    binary = Path(cfg.home) / "bin" / "ngrok"
    if binary.is_file() and os.access(str(binary), os.X_OK):
        c.add("OK", "ngrok binary", str(binary))
    else:
        c.add("FAIL", "ngrok binary", "%s missing (run ./install.sh)" % binary)
    yml = Path(cfg.home) / "ngrok.yml"
    if not cfg.ngrok.authtoken:
        c.add("FAIL", "ngrok config", "ngrok.authtoken is empty in config.json")
    elif yml.is_file():
        c.add("OK", "ngrok config", str(yml))
    else:
        c.add("FAIL", "ngrok config", "%s missing (run `tvbridge ngrok-config`)" % yml)
    try:
        status, body = _http("http://127.0.0.1:4040/api/tunnels", timeout=2.0)
        urls = [t.get("public_url", "") for t in json.loads(body.decode("utf-8")).get("tunnels", [])]
    except (OSError, ValueError, AttributeError):
        c.add("WARN", "ngrok tunnel", "the ngrok agent is not answering on 127.0.0.1:4040 (tunnel not running?)")
        return
    if ("https://" + domain) in urls:
        c.add("OK", "ngrok tunnel", "online: https://%s" % domain)
    else:
        c.add("WARN", "ngrok tunnel", "agent running but no tunnel for https://%s (tunnels: %s)" % (domain, urls))


def _doctor_engine(c: _Checks, cfg: Config, engine_alive: Any, heartbeat_age_s: Any, read_heartbeat: Any) -> None:
    url = _local_url(cfg, "/health")
    try:
        status, body = _http(url, timeout=2.0)
        if status == 200:
            c.add("OK", "listener", "%s -> %s" % (url, body.decode("utf-8", "replace")))
        else:
            c.add("FAIL", "listener", "%s -> HTTP %d" % (url, status))
    except (OSError, ValueError) as e:
        c.add("FAIL", "listener", "%s not reachable (%s): is the engine running?" % (url, e))
    hb = read_heartbeat(cfg.heartbeat_path)
    age = heartbeat_age_s(hb)
    if hb and hb.get("executor_stalled"):
        c.add("FAIL", "heartbeat", "EXECUTOR STALLED: %s" % hb.get("executor_stalled"))
    elif hb and hb.get("error"):
        c.add("FAIL", "heartbeat", "the last engine start failed: %s" % hb.get("error"))
    elif engine_alive(hb):
        c.add("OK", "heartbeat", "%.0f s ago (pid %s, mode %s)" % (age, hb.get("pid"), hb.get("mode")))
        if hb.get("positions_uncertain"):
            c.add("WARN", "positions", "POSITIONS_UNCERTAIN: %s" % hb.get("positions_uncertain"))
    elif age is None:
        c.add("FAIL", "heartbeat", "no heartbeat at %s: the engine has not run" % cfg.heartbeat_path)
    else:
        c.add("FAIL", "heartbeat", "last heartbeat %.0f s ago%s: the engine is not running" % (
            age, " (stopped cleanly)" if hb and hb.get("stopped") else ""))
    try:
        store = _open_store(cfg)
    except Exception as e:  # pragma: no cover - unusual
        c.add("FAIL", "database", "cannot open %s: %s" % (cfg.db_path, e))
        return
    try:
        halted = store.get_kv("halted")
        paused = store.get_kv("paused")
        if halted:
            c.add("WARN", "entries", "HALTED: %s" % halted)
        elif paused:
            c.add("WARN", "entries", "paused (%s)" % (store.get_kv("pause_reason") or "no reason"))
        else:
            c.add("OK", "entries", "allowed (not paused, not halted)")
    finally:
        store.close()


# --------------------------------------------------------------------------- calibrate / read-account / rehearse


def cmd_calibrate(args: argparse.Namespace) -> int:
    from .gui.calibration import CalibrationError, run_wizard

    cfg = load_config()
    try:
        run_wizard(_make_driver(), cfg, cfg.calibration_path)
    except CalibrationError as e:
        _err("calibration failed: %s" % e)
        return 1
    return 0


def cmd_read_account(args: argparse.Namespace) -> int:
    from .executors.base import ExecutorError
    from .gui.calibration import CalibrationError

    cfg = load_config()
    try:
        snap = _gui_executor(cfg).read_account()
    except (CalibrationError, ExecutorError) as e:
        _err("read-account failed: %s" % e)
        return 1
    print("balance:      %s" % _money(snap.balance))
    print("equity:       %s" % _money(snap.equity))
    if snap.margin is not None:
        print("margin:       %s" % _money(snap.margin))
    if snap.free_margin is not None:
        print("free margin:  %s" % _money(snap.free_margin))
    if snap.positions is None:
        print("positions:    UNKNOWN (%s)" % (snap.positions_note or "not readable"))
    positions = snap.positions or []
    print("positions:    %d" % len(positions))
    for p in positions:
        print("  %s %s %s ticket %s open %s sl %s tp %s profit %s" % (
            p.side, p.lots, p.symbol, p.ticket or "?", p.open_price, p.sl, p.tp, p.profit))
    if cfg.executor.mode == "paper":
        print("(note: executor.mode is paper; the engine trades the paper account, not this one)")
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    from .executors.base import ExecutorError
    from .gui.calibration import CalibrationError
    from .models import OrderRequest
    from .risk import round_lots_down

    cfg = load_config()
    tv = cfg.normalize_tv_symbol(args.symbol)
    spec = cfg.spec_for(tv)
    if spec is None:
        _err("no symbols.specs entry for %r" % args.symbol)
        return 2
    symbol = cfg.mt5_symbol(tv)
    if not (math.isfinite(args.sl) and args.sl > 0):
        _err("--sl must be a positive price")
        return 2
    if args.price is not None:
        wrong = (args.side == "buy" and args.sl >= args.price) or (args.side == "sell" and args.sl <= args.price)
        if wrong:
            _err("--sl %s is on the wrong side of --price %s for a %s" % (args.sl, args.price, args.side))
            return 2
    lots = args.lots if args.lots is not None else spec.min_lot
    lots = round_lots_down(min(lots, cfg.risk.max_lots), spec.lot_step, spec.min_lot)
    if lots <= 0:
        _err("--lots rounds below min_lot %s" % spec.min_lot)
        return 2
    req = OrderRequest(symbol=symbol, side=args.side, lots=lots, sl=args.sl, tp=args.tp or None,
                       digits=int(spec.digits), lot_decimals=spec.lot_decimals, comment="rehearse",
                       price_hint=args.price)
    print("rehearsing %s %s %s sl %s tp %s (Escape instead of %s; nothing is sent)" % (
        req.side, lots, symbol, req.sl, req.tp or "-", req.side.capitalize()))
    try:
        res = _gui_executor(cfg).open_market(req)
    except (CalibrationError, ExecutorError) as e:
        _err("rehearsal failed: %s" % e)
        return 1
    print("result:   %s" % res.status)
    print("message:  %s" % res.message)
    for path in res.evidence:
        print("evidence: %s" % path)
    return 0 if res.status == "rehearsed" else 1


# --------------------------------------------------------------------------- status / events


def _status_dict(cfg: Config) -> Dict[str, Any]:
    from .engine import build_status, engine_alive, heartbeat_age_s, read_heartbeat

    store = _open_store(cfg)
    try:
        st = build_status(cfg, store)
        st["recent_signals"] = [
            {"id": r.get("id"), "received_at": r.get("received_at"), "action": r.get("action"),
             "symbol": r.get("symbol"), "status": r.get("status"), "reason": r.get("reason")}
            for r in store.recent_signals(5)]
    finally:
        store.close()
    hb = read_heartbeat(cfg.heartbeat_path)
    alive = engine_alive(hb)
    st["engine"] = {
        "running": alive,
        "heartbeat_age_s": heartbeat_age_s(hb),
        "pid": (hb or {}).get("pid"),
        "port": (hb or {}).get("port"),
        "started_at": (hb or {}).get("started_at"),
        "current_task": (hb or {}).get("current_task"),
        "account_read_failures": (hb or {}).get("account_read_failures"),
        "last_account_error": (hb or {}).get("last_account_error"),
        "account_poll_crashes": (hb or {}).get("account_poll_crashes"),
        "executor_stalled": (hb or {}).get("executor_stalled") or "",
        "start_error": (hb or {}).get("error") or "",
    }
    st["positions_uncertain"] = ""
    if alive and hb is not None:
        st["untracked"] = hb.get("untracked") or []
        st["queue_size"] = hb.get("queue_size", st["queue_size"])
        st["positions_uncertain"] = hb.get("positions_uncertain") or ""
    return st


def _short_ts(ts: Any) -> str:
    s = str(ts or "")
    return s[:19].replace("T", " ") if len(s) >= 19 else s


def _format_status(st: Dict[str, Any]) -> str:
    out = []  # type: List[str]
    eng = st.get("engine") or {}
    out.append("tvbridge status at %s UTC (server time %s, UTC%+g)" % (
        _short_ts(st["now"]), st["server_time"], st["server_utc_offset_hours"]))
    if eng.get("running"):
        line = "running (pid %s, heartbeat %.0f s ago, %s task(s) queued)" % (
            eng.get("pid"), eng.get("heartbeat_age_s") or 0, st.get("queue_size"))
        if eng.get("current_task"):
            line += ", now: %s" % eng["current_task"]
        if eng.get("account_read_failures"):
            line += "; %s failed account read(s): %s" % (eng["account_read_failures"], eng.get("last_account_error"))
    elif eng.get("heartbeat_age_s") is None:
        line = "NOT RUNNING (no heartbeat yet)"
    else:
        line = "NOT RUNNING (last heartbeat %.0f s ago)" % eng["heartbeat_age_s"]
    if eng.get("executor_stalled"):
        line += "; EXECUTOR STALLED: %s" % eng["executor_stalled"]
    if eng.get("start_error"):
        line += "; last start FAILED: %s" % eng["start_error"]
    if eng.get("account_poll_crashes"):
        line += "; %s account poll crash(es)" % eng["account_poll_crashes"]
    out.append("engine:      " + line)
    out.append("mode:        %s   account: %s" % (st["mode"], st.get("account")))
    if st["halted"]:
        entries = "HALTED: %s\n             (check MT5 against this status, then `tvbridge resume`)" % st["halted"]
    elif st["paused"]:
        entries = "PAUSED (%s); `tvbridge resume` to allow entries" % (st.get("pause_reason") or "no reason")
    else:
        entries = "allowed"
    out.append("entries:     " + entries)
    out.append("window:      " + str(st["trading_window"]))
    snap = st.get("snapshot")
    if snap:
        out.append("account:     balance %s  equity %s  (%s, %.0f s ago)" % (
            _money(snap["balance"]), _money(snap["equity"]), snap.get("source") or "?", snap.get("age_s") or 0))
    else:
        out.append("account:     no snapshot yet")
    ref = st.get("day_reference")
    if ref:
        out.append("reference:   %s for %s (source %s)" % (_money(ref["reference"]), ref["server_date"], ref["source"]))
    else:
        out.append("reference:   none for %s (entries refused: NO_DAY_REFERENCE)" % st["server_date"])
    fl = st.get("floors")
    if fl:
        out.append("floors:      entry %s  kill %s  hard daily %s  hard max %s" % (
            _money(fl["entry_floor"]), _money(fl["kill_floor"]), _money(fl["hard_daily_floor"]),
            _money(fl["hard_max_floor"])))
    rows = st.get("open_positions") or []
    out.append("positions:   %d open in the ledger, total risk %s USD" % (len(rows), _money(st.get("open_risk_usd"))))
    for r in rows:
        out.append("  #%s %s %s %s @ %s sl %s tp %s risk %s ticket %s opened %s" % (
            r.get("pid"), r.get("side"), r.get("lots"), r.get("symbol"), r.get("entry_price"), r.get("sl"),
            r.get("tp") or "-", _money(r.get("risk_usd")), r.get("ticket") or "?", _short_ts(r.get("opened_at"))))
    if st.get("positions_uncertain"):
        out.append("UNCERTAIN:   %s (entries refused: POSITIONS_UNCERTAIN)" % st["positions_uncertain"])
    untracked = st.get("untracked") or []
    if untracked:
        out.append("untracked:   %d position(s) in MT5 that tvbridge did not open:" % len(untracked))
        for p in untracked:
            out.append("  %s %s %s ticket %s" % (p.get("side"), p.get("lots"), p.get("symbol"), p.get("ticket") or "?"))
    elif eng.get("running"):
        out.append("untracked:   none")
    out.append("trades:      %s today (max %s)" % (st["trades_today"], st["max_trades_per_day"]))
    mirror = st.get("mirror") or {}
    if mirror.get("enabled"):
        scales = mirror.get("scales") or {}
        fan = mirror.get("fan_out") or {}
        fan_txt = "".join("; fan-out %s -> %s" % (k, ", ".join(
            "%s (%s units per lot)" % (t.get("symbol"), t.get("units_per_lot")) for t in targets))
            for k, targets in sorted(fan.items()))
        lock = mirror.get("resync_lockout_min") or 0
        out.append("mirror:      enabled (%s units per lot, stop %s from the MT5 quote%s)%s%s%s" % (
            mirror.get("units_per_lot"), mirror.get("stop_distance"),
            ", adds allowed" if mirror.get("allow_adds") else "",
            "; scale " + ", ".join("%s %.3f" % (k, v) for k, v in sorted(scales.items())) if scales else "",
            fan_txt,
            "; re-sync a stopped-out leg after %g min (max %s per trade)" % (lock, mirror.get("resync_max_per_trade"))
            if lock else ""))
        for r in st.get("resync_pending") or []:
            out.append("re-sync:     %s %s pending, not before %s (stopped out at %s, re-sync %s)" % (
                r.get("side"), r.get("symbol"), r.get("not_before"), r.get("price"), r.get("n")))
    else:
        out.append("mirror:      off (sync alerts are refused: MIRROR_DISABLED)")
    pending = st.get("pending_signals") or []
    if pending:
        out.append("pending:     %d signal(s): %s" % (len(pending), ", ".join(
            "%s %s %s" % (p["action"], p["symbol"] or "*", p["status"]) for p in pending)))
    recent = st.get("recent_signals") or []
    if recent:
        out.append("recent signals:")
        for r in recent:
            out.append("  %s  %-9s %-10s %-9s %s" % (_short_ts(r.get("received_at")), r.get("action"),
                                                    r.get("symbol") or "*", r.get("status"), r.get("reason") or ""))
    return "\n".join(out)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    st = _status_dict(cfg)
    if args.json:
        print(json.dumps(st, indent=2, sort_keys=True, default=str))
    else:
        print(_format_status(st))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = _open_store(cfg)
    try:
        rows = store.recent_events(max(1, int(args.limit)))
    finally:
        store.close()
    for r in reversed(rows):   # oldest first, newest at the bottom
        print("%s  %-8s %-22s %s" % (_short_ts(r.get("ts")), r.get("level"), r.get("kind"), r.get("message")))
    if not rows:
        print("no events yet")
    return 0


# --------------------------------------------------------------------------- send-test


def cmd_send_test(args: argparse.Namespace) -> int:
    cfg = load_config()
    action = args.action.strip().lower()
    if not args.symbol and action.replace("-", "_") not in ("close_all", "closeall", "flatten", "flatten_all"):
        _err("--symbol is required for action %r" % action)
        return 2
    url = args.url or _local_url(cfg, cfg.server.path)
    if cfg.executor.mode == "live" and action in ENTRY_ACTIONS:
        print("WARNING: executor.mode is live: this test alert will place a REAL order in MT5.")
        if not sys.stdin.isatty():
            _err("refusing to send a live test entry without a terminal to confirm it")
            return 1
        if input("Type LIVE to send it: ").strip() != "LIVE":
            print("not sent")
            return 1
    payload = {
        "secret": cfg.server.secret,
        "time": clock.iso(clock.utcnow()),
        "action": action,
        "id": "test-" + uuid.uuid4().hex[:12],
        "strategy": "send-test",
        "comment": "send-test",
    }  # type: Dict[str, Any]
    if args.symbol:
        payload["symbol"] = args.symbol
    for key in ("price", "sl", "tp"):
        value = getattr(args, key)
        if value is not None:
            payload[key] = value
    shown = {k: v for k, v in payload.items() if k != "secret"}
    print("POST %s %s" % (url, json.dumps(shown, sort_keys=True)))
    try:
        status, body = _http(url, json.dumps(payload).encode("utf-8"), timeout=15.0,
                             headers={"Content-Type": "application/json"})
    except (OSError, ValueError) as e:
        _err("could not reach %s: %s (is the engine running?)" % (url, e))
        return 1
    print("HTTP %d %s" % (status, body.decode("utf-8", "replace")))
    return 0 if 200 <= status < 300 else 1


# --------------------------------------------------------------------------- pause / resume / flatten / set-reference


def cmd_pause(args: argparse.Namespace) -> int:
    cfg = load_config()
    reason = (args.reason or "manual").strip() or "manual"
    store = _open_store(cfg)
    try:
        store.set_kv("paused", "1")
        store.set_kv("pause_reason", reason)
        store.log_event("info", "pause", "entries paused: %s" % reason)
    finally:
        store.close()
    print("New entries paused (%s). Closes, flatten and the kill switch keep working." % reason)
    print("Run `tvbridge resume` to allow entries again.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from .engine import floors_for_day

    cfg = load_config()
    store = _open_store(cfg)
    try:
        paused = store.get_kv("paused")
        halted = store.get_kv("halted")
        if halted and halted.startswith("KILL") and not args.force:
            now = clock.utcnow()
            floors = floors_for_day(cfg, store, cfg.server_date(now))
            snap = store.latest_snapshot()
            problem = ""
            if snap is None:
                problem = "there is no account snapshot"
            elif (now - snap.ts).total_seconds() > float(cfg.risk.equity_max_age_s):
                problem = "the last account snapshot is %.0f s old" % (now - snap.ts).total_seconds()
            elif floors is not None and snap.equity <= floors.kill_floor:
                problem = "equity %s is still at or below the kill floor %s" % (
                    _money(snap.equity), _money(floors.kill_floor))
            if problem:
                _err("Not resumed: the halt is a kill-switch halt and %s." % problem)
                if snap is not None and floors is not None:
                    _err("  equity %s, kill floor %s, entry floor %s" % (
                        _money(snap.equity), _money(floors.kill_floor), _money(floors.entry_floor)))
                _err("Wait for equity to recover (or a fresh account read), or use `tvbridge resume --force`.")
                return 1
        for key in ("paused", "pause_reason", "halted"):
            store.set_kv(key, None)
        # tells the engine a human checked MT5: ledger rows MT5 no longer shows are closed
        store.set_kv("resume_ack", clock.iso(clock.utcnow()))
        store.log_event("info", "resume", "entries resumed (was paused: %s; halted: %s)" % (
            bool(paused), halted or "no"))
    finally:
        store.close()
    if halted:
        print("Cleared halt: %s" % halted)
    if paused:
        print("Cleared pause.")
    if not halted and not paused:
        print("Entries were neither paused nor halted.")
    print("Ledger positions MT5 no longer shows (POSITIONS_UNCERTAIN) are now treated as closed.")
    print("New entries are allowed (subject to the risk checks). Check `tvbridge status`.")
    return 0


def cmd_flatten(args: argparse.Namespace) -> int:
    from .engine import engine_alive, read_heartbeat

    cfg = load_config()
    if not args.yes:
        print("This asks the running engine to close EVERY open position.")
        try:
            answer = input("Type FLATTEN to confirm: ")
        except EOFError:
            answer = ""
        if answer.strip() != "FLATTEN":
            print("Not confirmed; nothing done.")
            return 1
    store = _open_store(cfg)
    try:
        store.set_kv("command", json.dumps({"cmd": "flatten", "ts": clock.iso(clock.utcnow()), "by": "cli"}))
        store.log_event("warn", "command", "flatten requested from the CLI")
    finally:
        store.close()
    print("Flatten requested. The engine closes every position within a few seconds; follow with "
          "`tvbridge status` / `tvbridge events`.")
    if not engine_alive(read_heartbeat(cfg.heartbeat_path)):
        print("WARNING: the engine does not seem to be running; the request runs when it starts (it expires "
              "after an hour). Close positions in MT5 by hand if needed.")
    return 0


def cmd_set_reference(args: argparse.Namespace) -> int:
    from .risk import compute_floors

    cfg = load_config()
    try:
        value = float(args.value)
        floors = compute_floors(value, cfg)
    except ValueError as e:
        _err("invalid reference %r: %s" % (args.value, e))
        return 2
    if args.date:
        try:
            day = date.fromisoformat(args.date)
        except ValueError:
            _err("--date must be YYYY-MM-DD (got %r)" % args.date)
            return 2
    else:
        day = cfg.server_date(clock.utcnow())
    store = _open_store(cfg)
    try:
        old = store.get_day_state(day)
        snap = store.latest_snapshot()
        reasons = []  # type: List[str]
        if old is not None and value < float(old["reference"]):
            reasons.append("it LOWERS the reference from %s (looser floors)" % _money(old["reference"]))
        if snap is not None:
            acct = max(float(snap.balance), float(snap.equity))
            if acct > 0 and abs(value - acct) / acct > REFERENCE_CONFIRM_REL:
                reasons.append("it is more than %.0f %% away from the account's last balance/equity %s" % (
                    100 * REFERENCE_CONFIRM_REL, _money(acct)))
        if reasons and not args.force:
            print("Check this: %s." % "; ".join(reasons))
            if old is not None:
                of = compute_floors(float(old["reference"]), cfg)
                print("  now: entry floor %s, kill floor %s" % (_money(of.entry_floor), _money(of.kill_floor)))
            print("  new: entry floor %s, kill floor %s" % (_money(floors.entry_floor), _money(floors.kill_floor)))
            if not sys.stdin.isatty():
                _err("refusing without a terminal to confirm it; use --force if the value is right")
                return 1
            try:
                answer = input("Type %s again to confirm: " % args.value)
            except EOFError:
                answer = ""
            if answer.strip() != str(args.value).strip():
                print("Not confirmed; nothing changed.")
                return 1
        store.set_day_state(day, value, value, "manual")
        store.log_event("info", "set_reference", "daily reference for %s set to %s (was %s)" % (
            day.isoformat(), _money(value), _money(old["reference"]) if old else "unset"))
    finally:
        store.close()
    print("Daily reference for server day %s: %s (was %s)" % (
        day.isoformat(), _money(value), _money(old["reference"]) if old else "unset"))
    print("  hard daily floor %s, hard max floor %s" % (_money(floors.hard_daily_floor), _money(floors.hard_max_floor)))
    print("  entry floor %s, kill floor %s" % (_money(floors.entry_floor), _money(floors.kill_floor)))
    return 0


# --------------------------------------------------------------------------- ngrok-config


def cmd_ngrok_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    token = (cfg.ngrok.authtoken or "").strip()
    if not token:
        _err("ngrok.authtoken is empty in %s (dashboard.ngrok.com > Your Authtoken)" % cfg.source_path)
        return 1
    path = Path(cfg.home) / "ngrok.yml"
    # JSON string syntax is a valid YAML double-quoted scalar.
    _write_private(path, 'version: "3"\nagent:\n  authtoken: %s\n' % json.dumps(token))
    print("wrote %s (mode 600)" % path)
    if not cfg.ngrok.domain:
        _err("WARNING: ngrok.domain is empty; set your reserved domain to get a stable webhook URL")
        return 0
    print("https://%s%s" % (cfg.ngrok.domain, cfg.server.path))
    return 0


# --------------------------------------------------------------------------- parser / main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tvbridge",
        description="TradingView alert -> webhook -> risk guard -> MetaTrader 5 GUI clicker.",
        epilog="Data: $TVBRIDGE_HOME (default ~/.tvbridge). Logs: ~/Library/Logs/tvbridge. See README.md.",
    )
    p.add_argument("--version", action="version", version="tvbridge %s" % __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Any, text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=text, description=text)
        sp.set_defaults(func=func)
        return sp

    add("init", cmd_init, "create the data folder and config.json (with a random secret) if absent")
    add("run", cmd_run, "run the engine in the foreground (normally started by launchd)")
    sp = add("doctor", cmd_doctor, "check config, permissions, MT5, calibration, ngrok and the engine")
    sp.add_argument("--prompt", action="store_true", help="trigger the macOS permission prompts")
    add("calibrate", cmd_calibrate, "interactive calibration wizard (hover only; use a DEMO account)")
    sp = add("status", cmd_status, "show mode, halts, account, floors, positions and queue")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    add("read-account", cmd_read_account, "read balance/equity/positions from the MT5 Toolbox once")
    sp = add("rehearse", cmd_rehearse, "fill and verify one order ticket, then Escape (never clicks Buy/Sell)")
    sp.add_argument("--symbol", required=True, help="e.g. EURUSD")
    sp.add_argument("--side", required=True, choices=("buy", "sell"))
    sp.add_argument("--sl", required=True, type=float, help="stop-loss price")
    sp.add_argument("--tp", type=float, default=None, help="take-profit price")
    sp.add_argument("--lots", type=float, default=None, help="volume (default: the symbol's min_lot)")
    sp.add_argument("--price", type=float, default=None, help="reference price (checks the SL side)")
    sp = add("send-test", cmd_send_test, "post a correctly signed test alert (runs in the CURRENT mode)")
    sp.add_argument("--action", required=True, help="buy, sell, close or close_all (aliases allowed)")
    sp.add_argument("--symbol", default=None, help="e.g. EURUSD (not needed for close_all)")
    sp.add_argument("--price", type=float, default=None)
    sp.add_argument("--sl", type=float, default=None)
    sp.add_argument("--tp", type=float, default=None)
    sp.add_argument("--url", default=None, help="post here instead of the local listener")
    sp = add("pause", cmd_pause, "block new entries (closes keep working)")
    sp.add_argument("--reason", default=None)
    sp = add("resume", cmd_resume, "clear pause and halt (confirms MT5 matches `tvbridge status`)")
    sp.add_argument("--force", action="store_true", help="clear a kill-switch halt even below the kill floor")
    sp = add("flatten", cmd_flatten, "ask the running engine to close every position")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp = add("set-reference", cmd_set_reference, "override the daily loss reference")
    sp.add_argument("value", metavar="VALUE", help="reference balance/equity, e.g. 50250.00")
    sp.add_argument("--date", default=None, help="server date YYYY-MM-DD (default: today)")
    sp.add_argument("--force", action="store_true", help="no confirmation when lowering it or far from the account")
    sp = add("events", cmd_events, "show recent events")
    sp.add_argument("--limit", type=int, default=30)
    add("ngrok-config", cmd_ngrok_config, "write ngrok.yml from config.json and print the webhook URL")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return int(func(args) or 0)
    except ConfigError as e:
        _err("config error: %s" % e)
        return 2
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
