import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from tvbridge import config as C
from tvbridge.config import (
    DEFAULTS, EXAMPLE_CONFIG_PATH, SECRET_PLACEHOLDER, Config, ConfigError, SymbolSpec, config_from_dict,
    deep_merge, default_home, load_config, normalize_tv_symbol,
)

SECRET = "s" * 24
HOME = Path("/nonexistent/tvbridge-test-home")


def cfg_with(**sections):
    d = {"server": {"secret": SECRET}}
    d = deep_merge(d, sections)
    return config_from_dict(d, HOME)


class DefaultsTests(unittest.TestCase):
    def test_minimal_config_uses_spec_defaults(self):
        cfg = cfg_with()
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.home, HOME)
        s = cfg.server
        self.assertEqual((s.host, s.port, s.path), ("127.0.0.1", 8787, "/webhook"))
        self.assertTrue(s.enforce_ip_allowlist)
        self.assertEqual(s.tradingview_ips, ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"])
        self.assertTrue(s.allow_local_requests)
        self.assertEqual((s.max_body_bytes, s.max_signal_age_s, s.max_future_skew_s, s.rate_limit_per_min),
                         (8192, 120, 30, 30))
        a = cfg.account
        self.assertEqual((a.name, a.initial_balance, a.currency, a.server_utc_offset_hours),
                         ("Hantec Endurance 50k", 50000.0, "USD", 3.0))
        r = cfg.risk
        self.assertEqual((r.daily_loss_pct, r.max_loss_pct, r.daily_buffer_pct, r.max_buffer_pct, r.kill_buffer_pct),
                         (4.0, 8.0, 1.0, 1.0, 0.3))
        self.assertEqual((r.risk_per_trade_pct, r.max_risk_per_trade_pct, r.max_total_open_risk_pct), (0.5, 1.0, 2.0))
        self.assertEqual((r.max_open_positions, r.max_trades_per_day, r.max_lots), (3, 8, 5.0))
        self.assertEqual((r.commission_per_lot_usd, r.slippage_buffer_pct, r.equity_max_age_s, r.entry_max_delay_s),
                         (5.0, 15.0, 90, 45))
        self.assertEqual((r.reverse_on_opposite, r.allow_pyramiding, r.block_untracked_positions), (True, False, True))
        self.assertEqual((r.trading_start_server, r.trading_end_server, r.friday_cutoff_server),
                         ("00:05", "23:50", "22:00"))
        self.assertEqual(r.trading_days_server, [0, 1, 2, 3, 4])
        self.assertEqual(r.min_hold_s_for_signal_close, 0)
        self.assertEqual(cfg.symbols.suffix, ".h")
        self.assertEqual(cfg.symbols.map, {})
        self.assertEqual(cfg.symbols.allowed, [])
        self.assertEqual(set(cfg.symbols.specs),
                         {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF", "XAUUSD"})
        g = cfg.executor.gui
        self.assertEqual(cfg.executor.mode, "paper")
        self.assertIsNone(cfg.executor.paper_start_balance)
        self.assertEqual(g.owner_names, ["MetaTrader 5", "terminal64", "wine64-preloader", "wine-preloader", "wine"])
        self.assertEqual(g.order_dialog_title_contains, ["Order"])
        self.assertEqual(g.position_dialog_title_contains, ["Position", "Order"])
        self.assertEqual(g.require_dialog_text, ["Market"])
        self.assertEqual((g.dialog_timeout_s, g.result_timeout_s, g.action_delay_s), (4.0, 8.0, 0.15))
        self.assertEqual((g.size_tolerance_px, g.ocr_min_confidence, g.account_poll_s, g.keep_screenshots_days),
                         (12, 0.3, 15, 14))
        n = cfg.notify
        self.assertEqual((n.macos, n.ntfy_url, n.min_level), (True, "", "info"))
        self.assertEqual((cfg.ngrok.authtoken, cfg.ngrok.domain), ("", ""))

    def test_default_specs(self):
        specs = cfg_with().symbols.specs
        for fx in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"):
            sp = specs[fx]
            self.assertEqual((sp.contract_size, sp.quote, sp.digits, sp.point, sp.min_sl_points),
                             (100000.0, "USD", 5, 0.00001, 50))
        self.assertEqual((specs["USDJPY"].quote, specs["USDJPY"].digits, specs["USDJPY"].point), ("JPY", 3, 0.001))
        self.assertEqual((specs["USDCAD"].quote, specs["USDCAD"].digits), ("CAD", 5))
        self.assertEqual((specs["USDCHF"].quote, specs["USDCHF"].digits), ("CHF", 5))
        x = specs["XAUUSD"]
        self.assertEqual((x.contract_size, x.quote, x.digits, x.point, x.min_sl_points), (100.0, "USD", 2, 0.01, 100))
        for sp in specs.values():
            self.assertEqual((sp.lot_step, sp.min_lot, sp.lot_decimals), (0.01, 0.01, 2))

    def test_path_properties(self):
        cfg = cfg_with()
        self.assertEqual(cfg.db_path, HOME / "tvbridge.db")
        self.assertEqual(cfg.calibration_path, HOME / "calibration.json")
        self.assertEqual(cfg.heartbeat_path, HOME / "heartbeat.json")
        self.assertEqual(cfg.shots_dir, HOME / "shots")
        self.assertEqual(cfg.log_dir, Path("~/Library/Logs/tvbridge").expanduser())

    def test_defaults_not_mutated_by_loading(self):
        before = copy.deepcopy(DEFAULTS)
        cfg_with(risk={"daily_loss_pct": 5.0}, symbols={"specs": {"eurusd": {"digits": 4}}})
        self.assertEqual(DEFAULTS, before)

    def test_to_dict_redacts_secrets(self):
        cfg = cfg_with(notify={"telegram_bot_token": "123:abc"}, ngrok={"authtoken": "tok"})
        d = cfg.to_dict()
        json.dumps(d)
        self.assertEqual(d["server"]["secret"], "***")
        self.assertEqual(d["notify"]["telegram_bot_token"], "***")
        self.assertEqual(d["ngrok"]["authtoken"], "***")
        self.assertNotIn(SECRET, json.dumps(d))
        self.assertEqual(cfg.to_dict(redact=False)["server"]["secret"], SECRET)


class DeepMergeTests(unittest.TestCase):
    def test_deep_merge_function(self):
        base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
        out = deep_merge(base, {"a": {"c": [3]}, "e": 2})
        self.assertEqual(out, {"a": {"b": 1, "c": [3]}, "d": 1, "e": 2})
        self.assertEqual(base, {"a": {"b": 1, "c": [1, 2]}, "d": 1})

    def test_partial_section_keeps_other_defaults(self):
        cfg = cfg_with(risk={"daily_loss_pct": 5.0, "max_trades_per_day": 3})
        self.assertEqual(cfg.risk.daily_loss_pct, 5.0)
        self.assertEqual(cfg.risk.max_trades_per_day, 3)
        self.assertEqual(cfg.risk.max_loss_pct, 8.0)
        self.assertEqual(cfg.risk.risk_per_trade_pct, 0.5)
        self.assertEqual(cfg.server.port, 8787)

    def test_nested_gui_merge(self):
        cfg = cfg_with(executor={"mode": "rehearsal", "gui": {"dialog_timeout_s": 6}})
        self.assertEqual(cfg.executor.mode, "rehearsal")
        self.assertEqual(cfg.executor.gui.dialog_timeout_s, 6.0)
        self.assertIsInstance(cfg.executor.gui.dialog_timeout_s, float)
        self.assertEqual(cfg.executor.gui.result_timeout_s, 8.0)

    def test_lists_replace(self):
        cfg = cfg_with(server={"tradingview_ips": ["1.2.3.4"]}, risk={"trading_days_server": [0, 1]})
        self.assertEqual(cfg.server.tradingview_ips, ["1.2.3.4"])
        self.assertEqual(cfg.risk.trading_days_server, [0, 1])

    def test_spec_merge_keeps_unspecified_fields(self):
        cfg = cfg_with(symbols={"specs": {"XAUUSD": {"contract_size": 10}}})
        x = cfg.symbols.specs["XAUUSD"]
        self.assertEqual(x.contract_size, 10.0)
        self.assertEqual((x.digits, x.point, x.min_sl_points), (2, 0.01, 100))

    def test_spec_keys_normalized_before_merge(self):
        cfg = cfg_with(symbols={"specs": {"oanda:xauusd": {"contract_size": 10}, "EUR/USD": {"min_sl_points": 80}}})
        self.assertEqual(cfg.symbols.specs["XAUUSD"].contract_size, 10.0)
        self.assertEqual(cfg.symbols.specs["XAUUSD"].digits, 2)
        self.assertEqual(cfg.symbols.specs["EURUSD"].min_sl_points, 80)
        self.assertEqual(cfg.symbols.specs["EURUSD"].digits, 5)
        self.assertNotIn("oanda:xauusd", cfg.symbols.specs)

    def test_new_symbol_gets_symbolspec_defaults(self):
        cfg = cfg_with(symbols={"specs": {"EURGBP": {"quote": "gbp"}}})
        sp = cfg.symbols.specs["EURGBP"]
        self.assertEqual(sp.quote, "GBP")
        self.assertEqual((sp.contract_size, sp.digits, sp.point, sp.lot_step, sp.min_lot, sp.min_sl_points),
                         (100000.0, 5, 0.00001, 0.01, 0.01, 50))
        self.assertIn("EURUSD", cfg.symbols.specs)

    def test_null_spec_removes_default(self):
        cfg = cfg_with(symbols={"specs": {"USDJPY": None}})
        self.assertNotIn("USDJPY", cfg.symbols.specs)
        self.assertIn("EURUSD", cfg.symbols.specs)

    def test_comment_keys_ignored_everywhere(self):
        cfg = cfg_with(_comment="x", server={"_comment": "y"}, risk={"_note": 1},
                       executor={"gui": {"_comment": "z"}},
                       symbols={"_comment": "c", "specs": {"_comment": "check Hantec", "EURUSD": {"_c": 1}}})
        self.assertNotIn("_comment", cfg.symbols.specs)
        self.assertIn("EURUSD", cfg.symbols.specs)

    def test_int_accepts_integral_float(self):
        self.assertEqual(cfg_with(server={"port": 9000.0}).server.port, 9000)


class ValidationTests(unittest.TestCase):
    def assertConfigError(self, needle, **sections):
        with self.assertRaises(ConfigError) as cm:
            cfg_with(**sections)
        self.assertIn(needle, str(cm.exception))
        return cm.exception

    # -- secret
    def test_missing_secret(self):
        with self.assertRaises(ConfigError) as cm:
            config_from_dict({}, HOME)
        self.assertIn("server.secret", str(cm.exception))
        self.assertIn("16", str(cm.exception))

    def test_short_secret(self):
        self.assertConfigError("server.secret", server={"secret": "x" * 15})
        cfg_with(server={"secret": "x" * 16})  # boundary is OK

    def test_placeholder_secret_rejected(self):
        e = self.assertConfigError("placeholder", server={"secret": SECRET_PLACEHOLDER})
        self.assertIn("python -m tvbridge init", str(e))

    def test_secret_whitespace(self):
        self.assertConfigError("whitespace", server={"secret": " " + "x" * 20})

    # -- risk relations
    def test_risk_per_trade_above_max(self):
        self.assertConfigError("risk_per_trade_pct", risk={"risk_per_trade_pct": 1.5, "max_risk_per_trade_pct": 1.0})

    def test_max_risk_above_three(self):
        self.assertConfigError("max_risk_per_trade_pct", risk={"max_risk_per_trade_pct": 3.5})
        cfg_with(risk={"max_risk_per_trade_pct": 3.0})

    def test_risk_per_trade_zero(self):
        self.assertConfigError("risk_per_trade_pct", risk={"risk_per_trade_pct": 0})

    def test_daily_buffer_not_below_daily_loss(self):
        self.assertConfigError("daily_buffer_pct", risk={"daily_buffer_pct": 4.0})
        self.assertConfigError("daily_buffer_pct", risk={"daily_buffer_pct": -0.1})
        cfg_with(risk={"daily_buffer_pct": 0})

    def test_max_buffer_not_below_max_loss(self):
        self.assertConfigError("max_buffer_pct", risk={"max_buffer_pct": 8.0})

    def test_max_open_positions(self):
        self.assertConfigError("max_open_positions", risk={"max_open_positions": 0})

    def test_bad_times_and_days(self):
        self.assertConfigError("risk.trading_start_server", risk={"trading_start_server": "25:00"})
        self.assertConfigError("risk.friday_cutoff_server", risk={"friday_cutoff_server": "late"})
        self.assertIsNone(cfg_with(risk={"friday_cutoff_server": None}).risk.friday_cutoff_server)
        self.assertConfigError("trading_days_server", risk={"trading_days_server": [0, 7]})

    # -- unknown keys & types
    def test_unknown_top_level_key(self):
        self.assertConfigError("unknown config key 'sever'", sever={})

    def test_unknown_nested_key(self):
        self.assertConfigError("risk.dailly_loss_pct", risk={"dailly_loss_pct": 3})

    def test_unknown_gui_key(self):
        self.assertConfigError("executor.gui.dialog_timeout", executor={"gui": {"dialog_timeout": 3}})

    def test_unknown_spec_key(self):
        self.assertConfigError("symbols.specs.EURUSD.contractsize",
                               symbols={"specs": {"EURUSD": {"contractsize": 1}}})

    def test_type_errors(self):
        self.assertConfigError("server.port", server={"port": "8787"})
        self.assertConfigError("server.port", server={"port": 87.5})
        self.assertConfigError("server.enforce_ip_allowlist", server={"enforce_ip_allowlist": "yes"})
        self.assertConfigError("risk.max_lots", risk={"max_lots": True})
        self.assertConfigError("risk.max_lots", risk={"max_lots": "5"})
        self.assertConfigError("server.tradingview_ips", server={"tradingview_ips": "1.2.3.4"})
        self.assertConfigError("risk", risk=[1, 2])
        self.assertConfigError("symbols.specs.EURUSD", symbols={"specs": {"EURUSD": 5}})

    def test_other_validations(self):
        self.assertConfigError("executor.mode", executor={"mode": "yolo"})
        self.assertConfigError("notify.min_level", notify={"min_level": "loud"})
        self.assertConfigError("notify.ntfy_url", notify={"ntfy_url": "ntfy.sh/topic"})
        self.assertConfigError("tradingview_ips", server={"tradingview_ips": ["1.2.3"]})
        self.assertConfigError("server.path", server={"path": "webhook"})
        self.assertConfigError("server.port", server={"port": 70000})
        self.assertConfigError("ngrok.domain", ngrok={"domain": "https://x.ngrok-free.app"})
        self.assertConfigError("symbols.allowed", symbols={"allowed": ["GER40"]})
        self.assertConfigError("contract_size", symbols={"specs": {"EURUSD": {"contract_size": 0}}})
        self.assertConfigError("quote", symbols={"specs": {"EURUSD": {"quote": "US"}}})
        self.assertConfigError("account.initial_balance", account={"initial_balance": 0})
        self.assertConfigError("paper_start_balance", executor={"paper_start_balance": -1})

    def test_normalizations(self):
        cfg = cfg_with(executor={"mode": " LIVE "}, notify={"min_level": "WARN"},
                       ngrok={"domain": "me.ngrok-free.app/"})
        self.assertEqual(cfg.executor.mode, "live")
        self.assertEqual(cfg.notify.min_level, "warn")
        self.assertEqual(cfg.ngrok.domain, "me.ngrok-free.app")

    def test_top_level_must_be_dict(self):
        with self.assertRaises(ConfigError):
            config_from_dict([], HOME)  # type: ignore[arg-type]


class SymbolTests(unittest.TestCase):
    def test_normalize_module_function(self):
        cases = {
            "OANDA:EURUSD": "EURUSD", "EUR/USD": "EURUSD", "eurusd.h": "EURUSD", "EURUSD.H": "EURUSD",
            " fx:gbp_usd ": "GBPUSD", "XAUUSD": "XAUUSD", "a:b:usdjpy": "USDJPY", "": "", None: "",
        }
        for raw, want in cases.items():
            self.assertEqual(normalize_tv_symbol(raw), want, raw)
        self.assertEqual(normalize_tv_symbol("EURUSD.pro", ".pro"), "EURUSD")
        self.assertEqual(normalize_tv_symbol("EURUSD.h", ""), "EURUSD.H")
        self.assertEqual(normalize_tv_symbol(".h"), ".H")

    def test_config_normalize_uses_configured_suffix(self):
        cfg = cfg_with(symbols={"suffix": "-ECN"})
        self.assertEqual(cfg.normalize_tv_symbol("eurusd-ecn"), "EURUSD")
        self.assertEqual(cfg.mt5_symbol("OANDA:EURUSD"), "EURUSD-ECN")

    def test_mt5_symbol(self):
        cfg = cfg_with()
        self.assertEqual(cfg.mt5_symbol("OANDA:EURUSD"), "EURUSD.h")
        self.assertEqual(cfg.mt5_symbol("EUR/USD"), "EURUSD.h")
        self.assertEqual(cfg.mt5_symbol("eurusd.h"), "EURUSD.h")
        self.assertEqual(cfg.mt5_symbol("xauusd"), "XAUUSD.h")

    def test_map_override(self):
        cfg = cfg_with(symbols={"map": {"oanda:xauusd": "GOLD.h", "US30": "DJ30.h"}})
        self.assertEqual(cfg.symbols.map, {"XAUUSD": "GOLD.h", "US30": "DJ30.h"})
        self.assertEqual(cfg.mt5_symbol("TVC:XAUUSD"), "GOLD.h")
        self.assertEqual(cfg.mt5_symbol("EURUSD"), "EURUSD.h")
        self.assertIn("GOLD.h", cfg.known_mt5_symbols())
        self.assertNotIn("XAUUSD.h", cfg.known_mt5_symbols())

    def test_spec_for_tv_and_mt5_forms(self):
        cfg = cfg_with()
        eur = cfg.symbols.specs["EURUSD"]
        for s in ("EURUSD", "OANDA:EURUSD", "EUR/USD", "EURUSD.h", "eurusd.H"):
            self.assertIs(cfg.spec_for(s), eur, s)
        self.assertIsNone(cfg.spec_for("GER40"))
        self.assertIsNone(cfg.spec_for(""))
        self.assertIsNone(cfg.spec_for(None))  # type: ignore[arg-type]

    def test_spec_for_reverse_lookup_of_map_values(self):
        cfg = cfg_with(symbols={"map": {"XAUUSD": "GOLD"}})
        gold = cfg.symbols.specs["XAUUSD"]
        self.assertIs(cfg.spec_for("GOLD"), gold)
        self.assertIs(cfg.spec_for("gold"), gold)
        self.assertIs(cfg.spec_for("GOLD.h"), gold)   # normalized map value match
        self.assertIs(cfg.spec_for("XAUUSD"), gold)
        self.assertEqual(cfg.tv_symbol_for("GOLD"), "XAUUSD")
        self.assertIsNone(cfg.tv_symbol_for("SILVER"))

    def test_is_allowed(self):
        cfg = cfg_with()
        self.assertTrue(cfg.is_allowed("OANDA:EURUSD"))
        self.assertTrue(cfg.is_allowed("xauusd"))
        self.assertFalse(cfg.is_allowed("GER40"))
        self.assertFalse(cfg.is_allowed(""))
        cfg2 = cfg_with(symbols={"allowed": ["eur/usd", "XAUUSD.h"]})
        self.assertEqual(cfg2.symbols.allowed, ["EURUSD", "XAUUSD"])
        self.assertTrue(cfg2.is_allowed("FX:EURUSD"))
        self.assertTrue(cfg2.is_allowed("XAUUSD"))
        self.assertFalse(cfg2.is_allowed("GBPUSD"))

    def test_known_mt5_symbols(self):
        cfg = cfg_with()
        self.assertEqual(sorted(cfg.known_mt5_symbols()),
                         sorted(s + ".h" for s in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD",
                                                   "USDCHF", "XAUUSD")))

    def test_lot_decimals(self):
        cases = {0.01: 2, 0.1: 1, 1: 0, 1.0: 0, 0.001: 3, 0.05: 2, 10: 0, 0.5: 1}
        for step, want in cases.items():
            self.assertEqual(SymbolSpec(lot_step=step).lot_decimals, want, step)


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data, name="config.json"):
        p = self.dir / name
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p

    def test_load_valid_file_sets_home_to_parent(self):
        p = self.write({"server": {"secret": SECRET, "port": 9999}, "risk": {"max_lots": 2}})
        cfg = load_config(p)
        self.assertEqual(cfg.server.port, 9999)
        self.assertEqual(cfg.risk.max_lots, 2.0)
        self.assertEqual(cfg.home.resolve(), self.dir.resolve())
        self.assertEqual(cfg.db_path.resolve(), (self.dir / "tvbridge.db").resolve())
        self.assertEqual(cfg.source_path.resolve(), p.resolve())

    def test_load_with_bom(self):
        p = self.dir / "config.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"server": {"secret": SECRET}}).encode())
        self.assertEqual(load_config(p).server.secret, SECRET)

    def test_default_path_uses_tvbridge_home(self):
        self.write({"server": {"secret": SECRET}})
        with mock.patch.dict(os.environ, {"TVBRIDGE_HOME": str(self.dir)}):
            self.assertEqual(default_home(), self.dir)
            cfg = load_config()
        self.assertEqual(cfg.home.resolve(), self.dir.resolve())

    def test_default_home_without_env(self):
        env = dict(os.environ)
        env.pop("TVBRIDGE_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_home(), Path("~/.tvbridge").expanduser())

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as cm:
            load_config(self.dir / "nope.json")
        self.assertIn("python -m tvbridge init", str(cm.exception))

    def test_invalid_json(self):
        p = self.write('{"server": {"secret": ')
        with self.assertRaises(ConfigError) as cm:
            load_config(p)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_not_an_object(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[1, 2]"))

    def test_validation_error_mentions_file(self):
        p = self.write({"server": {"secret": SECRET}, "risk": {"bogus": 1}})
        with self.assertRaises(ConfigError) as cm:
            load_config(p)
        self.assertIn("risk.bogus", str(cm.exception))
        self.assertIn(str(p.name), str(cm.exception))

    def test_example_file_placeholder_is_rejected(self):
        self.assertTrue(EXAMPLE_CONFIG_PATH.exists(), EXAMPLE_CONFIG_PATH)
        p = self.write(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
        with self.assertRaises(ConfigError) as cm:
            load_config(p)
        self.assertIn("python -m tvbridge init", str(cm.exception))
        self.assertIn("placeholder", str(cm.exception))

    def test_example_file_matches_defaults(self):
        data = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["server"]["secret"], SECRET_PLACEHOLDER)
        self.assertIn("_comment", data["symbols"]["specs"])
        self.assertIn("Hantec", data["symbols"]["specs"]["_comment"])
        data["server"]["secret"] = SECRET
        from_example = config_from_dict(data, HOME).to_dict(redact=False)
        from_defaults = cfg_with().to_dict(redact=False)
        self.assertEqual(from_example, from_defaults)

        def strip(d):
            if isinstance(d, dict):
                return {k: strip(v) for k, v in d.items() if not k.startswith("_")}
            return d

        expected = copy.deepcopy(DEFAULTS)
        expected["server"]["secret"] = SECRET_PLACEHOLDER
        self.assertEqual(strip(json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))), expected)


class ModuleSurfaceTests(unittest.TestCase):
    def test_exports(self):
        for name in ("ServerCfg", "AccountCfg", "RiskCfg", "SymbolSpec", "SymbolsCfg", "GuiCfg", "ExecutorCfg",
                     "NotifyCfg", "NgrokCfg", "Config", "ConfigError", "load_config", "config_from_dict",
                     "default_home", "normalize_tv_symbol", "DEFAULTS"):
            self.assertTrue(hasattr(C, name), name)


class ServerOffsetTests(unittest.TestCase):
    UTC = timezone.utc

    def test_fixed_offset(self):
        cfg = cfg_with()
        self.assertFalse(cfg.server_offset_is_auto)
        self.assertEqual(cfg.server_offset_at(datetime(2026, 12, 1, tzinfo=self.UTC)), 3.0)
        self.assertEqual(cfg.server_midnight_utc(date(2026, 10, 6)), datetime(2026, 10, 5, 21, tzinfo=self.UTC))
        self.assertEqual(cfg.server_date(datetime(2026, 10, 5, 21, 0, tzinfo=self.UTC)), date(2026, 10, 6))

    def test_auto_offset_follows_us_dst(self):
        cfg = cfg_with(account={"server_utc_offset_hours": " AUTO "})
        self.assertEqual(cfg.account.server_utc_offset_hours, "auto")
        self.assertTrue(cfg.server_offset_is_auto)
        self.assertEqual(cfg.server_offset_at(datetime(2026, 10, 6, 7, tzinfo=self.UTC)), 3.0)
        self.assertEqual(cfg.server_offset_at(datetime(2026, 11, 3, 7, tzinfo=self.UTC)), 2.0)
        self.assertEqual(cfg.server_midnight_utc(date(2026, 10, 6)), datetime(2026, 10, 5, 21, tzinfo=self.UTC))
        # Monday after the switch (Sun 1 Nov): 00:00 server = Sun 22:00 UTC
        self.assertEqual(cfg.server_midnight_utc(date(2026, 11, 2)), datetime(2026, 11, 1, 22, tzinfo=self.UTC))
        self.assertEqual(cfg.server_date(datetime(2026, 11, 2, 21, 30, tzinfo=self.UTC)), date(2026, 11, 2))
        self.assertEqual(cfg.server_date(datetime(2026, 11, 2, 22, 0, tzinfo=self.UTC)), date(2026, 11, 3))

    def test_trading_window_uses_the_resolved_offset(self):
        from tvbridge import risk
        fri = datetime(2026, 11, 6, 19, 30, tzinfo=self.UTC)    # 21:30 at UTC+2, 22:30 at UTC+3
        self.assertIsNone(risk.check_trading_window(fri, cfg_with(account={"server_utc_offset_hours": "auto"})))
        self.assertIn("Friday cutoff", risk.check_trading_window(fri, cfg_with()))

    def test_bad_offset_values(self):
        for bad in ("summer", "", True, 15, [3]):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                cfg_with(account={"server_utc_offset_hours": bad})


if __name__ == "__main__":
    unittest.main()
