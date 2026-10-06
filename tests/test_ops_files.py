"""Static checks for the ops files: install/uninstall scripts, launchd templates, docs.

These tests never execute install.sh or uninstall.sh. They only
  * parse the scripts with ``bash -n``,
  * run the plist renderer and config reader *extracted verbatim* from install.sh
    (the ``RENDER_PY`` / ``CONFIG_PY`` heredocs) against temp files, and
  * check that the docs cover the CLI, the payload schema and the reason codes.
"""

import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"
ENGINE_TEMPLATE = ROOT / "launchd" / "com.tvbridge.engine.plist.template"
NGROK_TEMPLATE = ROOT / "launchd" / "com.tvbridge.ngrok.plist.template"
README = ROOT / "README.md"
ALERTS = ROOT / "tradingview" / "ALERTS.md"
PINE = ROOT / "tradingview" / "tvbridge_example_strategy.pine"
SPEC = ROOT / "SPEC.md"

# SPEC §13 subcommands with the options each one takes.
CLI_SUBCOMMANDS = {
    "init": [],
    "run": [],
    "doctor": ["--prompt"],
    "calibrate": [],
    "status": ["--json"],
    "read-account": [],
    "rehearse": ["--symbol", "--side", "--sl", "--tp", "--lots", "--price"],
    "send-test": ["--action", "--symbol", "--price", "--sl", "--tp", "--url"],
    "pause": ["--reason"],
    "resume": ["--force"],
    "flatten": ["--yes"],
    "set-reference": ["--date", "--force"],
    "events": ["--limit"],
    "ngrok-config": [],
}

# Every reason code a user can see (SPEC §7, §9, §10, §12).
REASON_CODES = [
    # signals.py
    "BAD_JSON", "BAD_ACTION", "NO_SYMBOL", "SYMBOL_NOT_ALLOWED", "BAD_NUMBER", "NO_TIME",
    "STALE", "FUTURE",
    # risk.plan_entry
    "HALTED", "PAUSED", "NO_SPEC", "WINDOW", "STALE_SIGNAL", "NO_SNAPSHOT", "SNAPSHOT_STALE",
    "NO_DAY_REFERENCE", "UNTRACKED_POSITIONS", "NO_PRICE", "SL_MISSING", "SL_WRONG_SIDE",
    "TP_WRONG_SIDE", "SL_TOO_TIGHT", "OPPOSITE_OPEN", "PYRAMIDING", "MAX_POSITIONS",
    "MAX_TRADES_DAY", "NO_FX_RATE", "SIZE_TOO_SMALL", "TOTAL_OPEN_RISK", "BELOW_ENTRY_FLOOR",
    "WORST_CASE", "KILL",
    # executors
    "MT5_NOT_FOUND", "WRONG_ACCOUNT", "STRAY_DIALOG", "ORDER_DIALOG_NOT_OPENED",
    "DIALOG_LAYOUT_CHANGED", "VERIFY_FAILED", "BUTTON_LABEL_MISMATCH", "ACCOUNT_UNREADABLE",
    # engine
    "REVERSAL_CLOSE_FAILED", "UNCERTAIN_EXECUTION", "INTERRUPTED", "NO_POSITION",
    # review fixes
    "POSITIONS_UNCERTAIN", "SL_MISSING_ON_SERVER", "TOOLBOX_INCOMPLETE", "NO_ROWS_VISIBLE",
    "MAIN_LAYOUT_CHANGED", "AMBIGUOUS_MAIN_WINDOW", "ACCOUNT_LOGIN_REQUIRED", "GUI_ERROR", "ABORTED",
    "FLATTEN_PENDING", "SUPERSEDED_BY_CLOSE", "ID_REUSED", "PARTIAL_EXIT", "RESULT_MISMATCH",
    "RATE_LIMITED", "UNAUTHORIZED", "FORBIDDEN", "STORE_ERROR", "DEFERRED", "CLOSE_FAILED",
    # mirror mode
    "BAD_POSITION", "BAD_SIZE", "MIRROR_DISABLED", "MIRROR_ADD_REFUSED", "MIRROR_NOT_AN_ENTRY",
    "SUPERSEDED_BY_SYNC", "POSITIONS_UNKNOWN", "IN_SYNC", "QUOTE_UNREADABLE", "PRICE_GAP",
    "PARTIAL_VERIFY_FAILED", "FANNED_OUT",
]

# Every payload key (and alias) accepted by signals.parse_payload (SPEC §7).
PAYLOAD_KEYS = [
    "secret", "passphrase", "time", "timenow", "fired", "symbol", "ticker", "action",
    "position", "market_position", "price", "close", "sl", "stop", "stop_loss", "tp",
    "take_profit", "limit", "risk_pct", "risk", "quote_usd", "side", "id", "strategy",
    "comment", "order",
    # mirror mode (sync)
    "size", "market_position_size", "prev_position", "prev_size", "order_action", "order_contracts",
    "order_id",
]

ACTION_ALIASES = [
    "buy", "sell", "long", "short", "close", "exit", "flat", "close_position",
    "close_all", "closeall", "flatten", "flatten_all", "sync",
]

ENGINE_PLACEHOLDERS = {"PYTHON", "APPDIR", "TVBRIDGE_HOME", "LOGDIR"}
NGROK_PLACEHOLDERS = {"NGROK", "PORT", "DOMAIN", "TVBRIDGE_HOME", "LOGDIR"}

PLACEHOLDER_RE = re.compile(r"__([A-Z][A-Z_]*?)__")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_heredoc(script: str, tag: str) -> str:
    """Return the body of the quoted heredoc ``<<'TAG' ... TAG`` in a bash script."""
    match = re.search(r"<<'%s'\n(.*?)\n%s\n" % (re.escape(tag), re.escape(tag)), script, re.S)
    if not match:
        raise AssertionError("heredoc <<'%s' not found in install.sh" % tag)
    return match.group(1) + "\n"


def run_python_snippet(code: str, args: List[str]) -> subprocess.CompletedProcess:
    """Run ``python - <args>`` with ``code`` on stdin, like install.sh does."""
    return subprocess.run(
        [sys.executable, "-"] + list(args),
        input=code,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=30,
    )


def _code_lines(script: str) -> List[str]:
    """Lines of a bash script with full-line comments removed."""
    return [line for line in script.splitlines() if not line.lstrip().startswith("#")]


def pine_call_args(source: str, name: str) -> List[str]:
    """Argument text of every ``name(...)`` call in Pine source (calls may span lines).

    Full-line ``//`` comments are ignored; parentheses inside string literals are skipped.
    """
    code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("//"))
    results = []
    for match in re.finditer(r"(?<![\w.])%s\(" % re.escape(name), code):
        depth, quote, i = 1, "", match.end()
        while i < len(code) and depth:
            ch = code[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        results.append(code[match.end():i - 1])
    return results


class ShellScriptTests(unittest.TestCase):
    """(1) install.sh / uninstall.sh parse cleanly and follow the house rules."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = shutil.which("bash") or "/bin/bash"

    def _bash_n(self, path: Path) -> None:
        self.assertTrue(path.is_file(), "%s is missing" % path.name)
        proc = subprocess.run(
            [self.bash, "-n", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, "bash -n %s failed:\n%s" % (path.name, proc.stderr))

    def test_install_sh_syntax(self) -> None:
        self._bash_n(INSTALL_SH)

    def test_uninstall_sh_syntax(self) -> None:
        self._bash_n(UNINSTALL_SH)

    def test_strict_mode_and_no_sudo(self) -> None:
        for path in (INSTALL_SH, UNINSTALL_SH):
            text = _read(path)
            self.assertTrue(text.startswith("#!/usr/bin/env bash"), path.name)
            self.assertIn("set -euo pipefail", text, path.name)
            for line in _code_lines(text):
                # "sudo" may appear inside messages, never as a command.
                self.assertIsNone(
                    re.search(r"(^|[;&|(]\s*|\$\(\s*)sudo\s", line.strip()),
                    "%s runs sudo: %r" % (path.name, line),
                )

    def test_scripts_are_executable(self) -> None:
        for path in (INSTALL_SH, UNINSTALL_SH):
            mode = path.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, "%s is not executable (chmod +x)" % path.name)

    def test_install_sh_flags_and_launchctl_usage(self) -> None:
        text = _read(INSTALL_SH)
        self.assertIn("--no-launchd)", text)
        self.assertIn("--no-ngrok)", text)
        self.assertIn('uname -m', text)
        self.assertIn("https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin", text)
        self.assertIn("curl -fsSL", text)
        self.assertIn("plutil -lint", text)
        self.assertIn('launchctl bootout "$GUI_DOMAIN/$label"', text)
        self.assertIn('launchctl bootstrap "$GUI_DOMAIN" "$plist"', text)
        self.assertIn('GUI_DOMAIN="gui/$(id -u)"', text)
        self.assertNotIn("jq ", text)

    def test_install_sh_passes_every_template_placeholder(self) -> None:
        text = _read(INSTALL_SH)
        for key in sorted(ENGINE_PLACEHOLDERS | NGROK_PLACEHOLDERS):
            self.assertIn('"%s=$' % key, text, "install.sh never passes %s=... to the renderer" % key)

    def test_uninstall_sh_removes_both_agents_and_keeps_data(self) -> None:
        text = _read(UNINSTALL_SH)
        self.assertIn("com.tvbridge.engine", text)
        self.assertIn("com.tvbridge.ngrok", text)
        self.assertIn("launchctl bootout", text)
        for line in _code_lines(text):
            self.assertNotIn("rm -rf", line, "uninstall.sh must keep data: %r" % line)


@unittest.skipUnless(shutil.which("plutil"), "plutil is only available on macOS")
class PlistTemplateTests(unittest.TestCase):
    """(2) Render both templates exactly like install.sh does and lint them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.render_code = extract_heredoc(_read(INSTALL_SH), "RENDER_PY")

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tvbridge ops test ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        base = os.path.join(self.tmp, "Application Support", "Trading & Co <test>")
        self.values = {
            "PYTHON": os.path.join(base, "tvbridge", ".venv", "bin", "python"),
            "APPDIR": os.path.join(base, "tvbridge"),
            "TVBRIDGE_HOME": os.path.join(self.tmp, "home dir", ".tvbridge"),
            "LOGDIR": os.path.join(self.tmp, "Library", "Logs", "tvbridge"),
            "NGROK": os.path.join(self.tmp, "home dir", ".tvbridge", "bin", "ngrok"),
            "PORT": "8787",
            "DOMAIN": "example-name.ngrok-free.app",
        }

    def _render(self, template: Path, keys: List[str], out_name: str) -> subprocess.CompletedProcess:
        out = os.path.join(self.tmp, out_name)
        args = [str(template), out] + ["%s=%s" % (k, self.values[k]) for k in keys]
        return run_python_snippet(self.render_code, args)

    def _render_ok(self, template: Path, keys: List[str], out_name: str, check_leftovers: bool = True) -> Dict:
        proc = self._render(template, keys, out_name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.tmp, out_name)
        lint = subprocess.run(
            ["plutil", "-lint", out],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=30,
        )
        self.assertEqual(lint.returncode, 0, lint.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o644)
        if check_leftovers:
            rendered = _read(Path(out))
            self.assertIsNone(PLACEHOLDER_RE.search(rendered), "unrendered placeholder left")
        with open(out, "rb") as fh:
            return plistlib.load(fh)

    def test_templates_use_exactly_the_spec_placeholders(self) -> None:
        self.assertEqual(set(PLACEHOLDER_RE.findall(_read(ENGINE_TEMPLATE))), ENGINE_PLACEHOLDERS)
        self.assertEqual(set(PLACEHOLDER_RE.findall(_read(NGROK_TEMPLATE))), NGROK_PLACEHOLDERS)

    def _assert_common(self, plist: Dict, label: str) -> None:
        self.assertEqual(plist["Label"], label)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertIs(plist["KeepAlive"], True)
        self.assertEqual(plist["ThrottleInterval"], 10)
        self.assertEqual(plist["ProcessType"], "Interactive")
        env = plist["EnvironmentVariables"]
        self.assertEqual(env["TVBRIDGE_HOME"], self.values["TVBRIDGE_HOME"])
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        logdir = self.values["LOGDIR"] + "/"
        self.assertTrue(plist["StandardOutPath"].startswith(logdir), plist["StandardOutPath"])
        self.assertTrue(plist["StandardErrorPath"].startswith(logdir), plist["StandardErrorPath"])

    def test_engine_template_renders_and_lints(self) -> None:
        plist = self._render_ok(ENGINE_TEMPLATE, sorted(ENGINE_PLACEHOLDERS), "com.tvbridge.engine.plist")
        self._assert_common(plist, "com.tvbridge.engine")
        self.assertEqual(plist["ProgramArguments"], [self.values["PYTHON"], "-m", "tvbridge", "run"])
        self.assertEqual(plist["WorkingDirectory"], self.values["APPDIR"])

    def test_ngrok_template_renders_and_lints(self) -> None:
        plist = self._render_ok(NGROK_TEMPLATE, sorted(NGROK_PLACEHOLDERS), "com.tvbridge.ngrok.plist")
        self._assert_common(plist, "com.tvbridge.ngrok")
        self.assertEqual(
            plist["ProgramArguments"],
            [
                self.values["NGROK"], "http", "127.0.0.1:8787",
                "--url", "https://example-name.ngrok-free.app",
                "--config", self.values["TVBRIDGE_HOME"] + "/ngrok.yml",
                "--log", "stdout",
                "--inspect=false",        # no request bodies (with the secret) kept on 127.0.0.1:4040
            ],
        )

    def test_renderer_refuses_missing_or_empty_values(self) -> None:
        keys = sorted(ENGINE_PLACEHOLDERS - {"LOGDIR"})
        proc = self._render(ENGINE_TEMPLATE, keys, "missing.plist")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("__LOGDIR__", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "missing.plist")))

        self.values["DOMAIN"] = "  "
        proc = self._render(NGROK_TEMPLATE, sorted(NGROK_PLACEHOLDERS), "empty.plist")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("__DOMAIN__", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "empty.plist")))

    def test_renderer_does_not_resubstitute_inside_values(self) -> None:
        self.values["APPDIR"] = os.path.join(self.tmp, "odd __LOGDIR__ name")
        plist = self._render_ok(ENGINE_TEMPLATE, sorted(ENGINE_PLACEHOLDERS), "odd.plist", check_leftovers=False)
        self.assertEqual(plist["WorkingDirectory"], self.values["APPDIR"])


class InstallConfigReaderTests(unittest.TestCase):
    """The config.json reader embedded in install.sh (json module, no jq)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.code = extract_heredoc(_read(INSTALL_SH), "CONFIG_PY")

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tvbridge cfg test ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "config.json")

    def _write(self, data: Dict) -> None:
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _run(self, *args: str) -> str:
        proc = run_python_snippet(self.code, [self.config] + list(args))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_get_with_defaults(self) -> None:
        self._write({"server": {"port": 9000}, "ngrok": {"domain": "abc.ngrok-free.app"}})
        self.assertEqual(self._run("get", "server.port", "8787"), "9000")
        self.assertEqual(self._run("get", "server.path", "/webhook"), "/webhook")
        self.assertEqual(self._run("get", "ngrok.domain"), "abc.ngrok-free.app")
        self.assertEqual(self._run("get", "executor.mode", "paper"), "paper")

    def test_configured_detects_empty_and_placeholder_values(self) -> None:
        self._write({"ngrok": {"authtoken": "", "domain": "your-name.ngrok-free.app"}})
        self.assertEqual(self._run("configured", "ngrok.authtoken"), "no")
        self.assertEqual(self._run("configured", "ngrok.domain"), "no")
        self._write({"ngrok": {"authtoken": "2abcDEF_123456789", "domain": "calm-fox.ngrok-free.app"}})
        self.assertEqual(self._run("configured", "ngrok.authtoken"), "yes")
        self.assertEqual(self._run("configured", "ngrok.domain"), "yes")

    def test_configured_never_prints_the_secret(self) -> None:
        self._write({"ngrok": {"authtoken": "2abcDEF_123456789"}})
        self.assertNotIn("2abcDEF", self._run("configured", "ngrok.authtoken"))

    def test_missing_file_uses_defaults_and_bad_json_fails(self) -> None:
        self.assertEqual(self._run("get", "server.port", "8787"), "8787")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        proc = run_python_snippet(self.code, [self.config, "get", "server.port", "8787"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not valid JSON", proc.stderr)


class DocsTests(unittest.TestCase):
    """(3) README covers the CLI; ALERTS.md covers the payload; the Pine example is wired up."""

    def test_spec_subcommand_list_matches_this_test(self) -> None:
        if not SPEC.exists():
            self.skipTest("SPEC.md not present")
        line = next((l for l in _read(SPEC).splitlines() if "argparse subcommands:" in l), "")
        self.assertTrue(line, "SPEC §13 subcommand line not found")
        names = [item.split()[0] for item in re.findall(r"`([^`]+)`", line)]
        self.assertEqual(set(names), set(CLI_SUBCOMMANDS), "SPEC §13 changed; update CLI_SUBCOMMANDS")

    def test_readme_mentions_every_cli_subcommand_and_option(self) -> None:
        text = _read(README)
        for cmd, options in CLI_SUBCOMMANDS.items():
            self.assertRegex(text, r"tvbridge %s(?![\w-])" % re.escape(cmd), "README lacks `tvbridge %s`" % cmd)
            for opt in options:
                self.assertRegex(
                    text, r"%s(?![\w-])" % re.escape(opt), "README lacks option %s of %s" % (opt, cmd)
                )

    def test_readme_troubleshooting_covers_every_reason_code(self) -> None:
        text = _read(README)
        missing = [code for code in REASON_CODES if not re.search(r"\b%s\b" % code, text)]
        self.assertEqual(missing, [], "README troubleshooting lacks reason codes")

    def test_readme_key_topics(self) -> None:
        text = _read(README)
        for needle in (
            "paper", "rehearsal", "live", "DEMO", "Accessibility", "Screen Recording",
            "FileVault", "Do Not Disturb", "Login Items", "48,000", "46,000", "48,150",
            "LiveUpdate", "~/Library/Logs/tvbridge", "~/.tvbridge/shots", "./install.sh",
            "doctor --prompt", "scalping", "inactivity", "red-folder",
        ):
            self.assertIn(needle, text, "README lacks %r" % needle)

    def test_alerts_md_documents_every_payload_key(self) -> None:
        text = _read(ALERTS)
        for key in PAYLOAD_KEYS:
            self.assertRegex(text, r"`%s`" % re.escape(key), "ALERTS.md lacks payload key %s" % key)
        for alias in ACTION_ALIASES:
            self.assertRegex(text, r"`%s`" % re.escape(alias), "ALERTS.md lacks action alias %s" % alias)
        for needle in ("{{strategy.order.alert_message}}", "{{timenow}}", "{{ticker}}", "{{close}}",
                       "/webhook", "Once Per Bar Close", "2FA"):
            self.assertIn(needle, text)

    def test_alerts_md_json_examples_are_valid(self) -> None:
        """Every ```json block parses once TradingView placeholders are substituted."""
        text = _read(ALERTS)
        blocks = re.findall(r"```json\n(.*?)```", text, re.S)
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            sample = block.replace("{{strategy.order.alert_message}}", '{"action":"buy","sl":1.081}')
            sample = sample.replace("{{close}}", "1.0855")
            # Quoted placeholders become strings, bare ones ({{plot("SL")}}, {{plot_0}}) numbers.
            sample = re.sub(r'"\{\{[^{}]+\}\}"', '"X"', sample)
            sample = re.sub(r"\{\{[^{}]+\}\}", "1.08", sample)
            try:
                json.loads(sample)
            except ValueError as exc:  # pragma: no cover - failure path
                self.fail("invalid JSON example in ALERTS.md (%s):\n%s" % (exc, block))

    def test_pine_example_plumbing(self) -> None:
        text = _read(PINE)
        self.assertTrue(text.lstrip().startswith("//@version=6") or "\n//@version=6" in text)
        self.assertIn("strategy(", text)
        self.assertIn('request.currency_rate(syminfo.currency, "USD")', text)
        self.assertIn("format.mintick", text)
        self.assertIn("quote_usd", text)
        self.assertIn('"action":"close"', text)
        self.assertRegex(text, r"(?i)not a trading recommendation")
        for call in ("strategy.entry", "strategy.exit", "strategy.close"):
            calls = pine_call_args(text, call)
            self.assertTrue(calls, "%s missing" % call)
            for args in calls:
                self.assertRegex(args, r"\balert_message\s*=", "%s without alert_message" % call)
        self.assertNotIn("when =", text)  # removed in Pine v6


if __name__ == "__main__":
    unittest.main()
