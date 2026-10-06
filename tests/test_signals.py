"""Tests for tvbridge.signals (payload decoding, auth, parsing, freshness)."""

import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tvbridge import clock, signals
from tvbridge.config import config_from_dict
from tvbridge.models import Signal
from tvbridge.signals import SignalError, check_freshness, check_secret, decode_body, parse_payload

UTC = timezone.utc
SECRET = "test-secret-0123456789abcdef"
T0 = datetime(2026, 10, 1, 9, 56, tzinfo=UTC)
T0_ISO = "2026-10-01T09:56:00Z"
RECEIVED = T0 + timedelta(seconds=2)


def make_cfg(**sections):
    d = {"server": {"secret": SECRET}}
    for name, values in sections.items():
        d.setdefault(name, {}).update(values)
    return config_from_dict(d, home="/nonexistent/tvbridge-test-home")


CFG = make_cfg()


def payload(**overrides):
    """A valid buy alert; keyword ``None`` values remove that key."""
    d = {"secret": SECRET, "time": T0_ISO, "symbol": "OANDA:EURUSD", "price": 1.0855,
         "action": "buy", "sl": 1.0825, "tp": 1.0915, "strategy": "s1"}
    for k, v in overrides.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return d


def parse(d, cfg=CFG, received_at=RECEIVED):
    return parse_payload(d, cfg, received_at)


class SignalErrorTests(unittest.TestCase):
    def test_attributes_and_str(self):
        e = SignalError("BAD_NUMBER", "sl: 'x' is not a valid number")
        self.assertEqual(e.code, "BAD_NUMBER")
        self.assertEqual(e.message, "sl: 'x' is not a valid number")
        self.assertEqual(str(e), "BAD_NUMBER: sl: 'x' is not a valid number")
        self.assertIsInstance(e, Exception)

    def test_message_optional(self):
        e = SignalError("NO_TIME")
        self.assertEqual(e.message, "")
        self.assertEqual(str(e), "NO_TIME")


class DecodeBodyTests(unittest.TestCase):
    def assertBadJson(self, body):
        with self.assertRaises(SignalError) as cm:
            decode_body(body)
        self.assertEqual(cm.exception.code, "BAD_JSON")
        return cm.exception

    def test_valid_object(self):
        self.assertEqual(decode_body(b'{"a": 1, "b": "x"}'), {"a": 1, "b": "x"})

    def test_utf8_content(self):
        self.assertEqual(decode_body('{"comment": "café"}'.encode("utf-8")), {"comment": "café"})

    def test_bom_is_stripped(self):
        self.assertEqual(decode_body(b"\xef\xbb\xbf" + b'{"a": 1}'), {"a": 1})

    def test_surrounding_whitespace_ok(self):
        self.assertEqual(decode_body(b'\n  {"a": 1}  \r\n'), {"a": 1})

    def test_invalid_utf8(self):
        self.assertBadJson(b'{"a": "\xff\xfe"}')

    def test_invalid_json(self):
        self.assertBadJson(b'{"secret":"x","order":}')   # Pine order without alert_message
        self.assertBadJson(b"{'single': 'quotes'}")
        self.assertBadJson(b'{"a": 1,}')
        self.assertBadJson("{“smart”: 1}".encode("utf-8"))

    def test_empty_body(self):
        self.assertBadJson(b"")
        self.assertBadJson(b"   \n")
        self.assertBadJson(b"\xef\xbb\xbf")

    def test_non_object_json(self):
        for body in (b"[1, 2]", b'"text"', b"42", b"null", b"true"):
            with self.subTest(body=body):
                e = self.assertBadJson(body)
                self.assertIn("object", e.message)

    def test_bare_nan_token_is_accepted(self):
        # Pine may render an undefined plot as NaN without quotes.
        d = decode_body(b'{"sl": NaN}')
        self.assertTrue(d["sl"] != d["sl"])

    def test_too_deep_nesting(self):
        deep = '{"a":' * 40 + "1" + "}" * 40
        self.assertBadJson(deep.encode())
        very_deep = "[" * 5000 + "]" * 5000
        self.assertBadJson(very_deep.encode())

    def test_moderate_nesting_ok(self):
        d = decode_body(b'{"order": {"x": [1, {"y": 2}]}}')
        self.assertEqual(d["order"]["x"][1]["y"], 2)


class CheckSecretTests(unittest.TestCase):
    def test_correct_secret(self):
        self.assertTrue(check_secret({"secret": SECRET}, CFG))

    def test_passphrase_alias(self):
        self.assertTrue(check_secret({"passphrase": SECRET}, CFG))

    def test_empty_secret_falls_back_to_passphrase(self):
        self.assertTrue(check_secret({"secret": "", "passphrase": SECRET}, CFG))
        self.assertTrue(check_secret({"secret": None, "passphrase": SECRET}, CFG))

    def test_secret_key_takes_precedence(self):
        self.assertFalse(check_secret({"secret": "wrong-wrong-wrong", "passphrase": SECRET}, CFG))

    def test_wrong_secret(self):
        self.assertFalse(check_secret({"secret": "x" * len(SECRET)}, CFG))
        self.assertFalse(check_secret({"secret": SECRET[:-1]}, CFG))
        self.assertFalse(check_secret({"secret": SECRET + "x"}, CFG))
        self.assertFalse(check_secret({"secret": SECRET.upper()}, CFG))

    def test_whitespace_is_not_stripped(self):
        self.assertFalse(check_secret({"secret": " " + SECRET}, CFG))
        self.assertFalse(check_secret({"secret": SECRET + "\n"}, CFG))

    def test_missing_secret(self):
        self.assertFalse(check_secret({}, CFG))
        self.assertFalse(check_secret({"action": "buy"}, CFG))
        self.assertFalse(check_secret({"secret": ""}, CFG))
        self.assertFalse(check_secret({"secret": None}, CFG))

    def test_secret_in_nested_order_is_not_accepted(self):
        self.assertFalse(check_secret({"order": {"secret": SECRET}}, CFG))

    def test_non_string_secret(self):
        for v in (12345, 1.5, True, [SECRET], {"s": SECRET}):
            with self.subTest(v=v):
                self.assertFalse(check_secret({"secret": v}, CFG))

    def test_non_ascii_secret_does_not_raise(self):
        self.assertFalse(check_secret({"secret": "éè€" * 8}, CFG))

    def test_not_a_dict(self):
        self.assertFalse(check_secret(["secret", SECRET], CFG))  # type: ignore[arg-type]

    def test_no_configured_secret_never_matches(self):
        cfg = make_cfg()
        cfg.server.secret = ""
        self.assertFalse(check_secret({"secret": ""}, cfg))
        self.assertFalse(check_secret({"secret": "anything"}, cfg))
        self.assertFalse(check_secret({}, cfg))

    def test_uses_constant_time_compare(self):
        real = hmac.compare_digest
        with mock.patch.object(signals.hmac, "compare_digest", side_effect=real) as cd:
            self.assertTrue(check_secret({"secret": SECRET}, CFG))
            self.assertEqual(cd.call_count, 1)
            self.assertEqual(cd.call_args[0], (SECRET.encode(), SECRET.encode()))
            self.assertFalse(check_secret({"secret": "nope"}, CFG))
            self.assertEqual(cd.call_count, 2)

    def test_missing_secret_still_runs_a_comparison(self):
        real = hmac.compare_digest
        with mock.patch.object(signals.hmac, "compare_digest", side_effect=real) as cd:
            self.assertFalse(check_secret({}, CFG))
            self.assertEqual(cd.call_count, 1)

    def test_no_plain_equality_shortcut(self):
        # Even if compare_digest says "different", plain equality must not override it.
        with mock.patch.object(signals.hmac, "compare_digest", return_value=False):
            self.assertFalse(check_secret({"secret": SECRET}, CFG))


class ActionTests(unittest.TestCase):
    def action_of(self, **kw):
        return parse(payload(**kw)).action

    def test_aliases(self):
        cases = {
            "buy": "buy", "long": "buy",
            "sell": "sell", "short": "sell",
            "close": "close", "exit": "close", "flat": "close", "close_position": "close",
            "close_all": "close_all", "closeall": "close_all", "flatten": "close_all",
            "flatten_all": "close_all",
        }
        for given, want in cases.items():
            with self.subTest(action=given):
                self.assertEqual(self.action_of(action=given), want)

    def test_case_and_whitespace(self):
        self.assertEqual(self.action_of(action=" BUY "), "buy")
        self.assertEqual(self.action_of(action="Short"), "sell")
        self.assertEqual(self.action_of(action="CLOSE_ALL"), "close_all")
        self.assertEqual(self.action_of(action="\tExit\n"), "close")

    def test_spaces_and_hyphens_fold_to_underscore(self):
        self.assertEqual(self.action_of(action="close all"), "close_all")
        self.assertEqual(self.action_of(action="Close-All"), "close_all")
        self.assertEqual(self.action_of(action="flatten-all"), "close_all")
        self.assertEqual(self.action_of(action="close position"), "close")

    def test_unknown_action(self):
        for bad in ("hold", "buy_stop", "open", "cover", "1", "buysell"):
            with self.subTest(action=bad):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(action=bad))
                self.assertEqual(cm.exception.code, "BAD_ACTION")

    def test_non_string_action(self):
        for bad in (1, True, ["buy"], {"a": "buy"}):
            with self.subTest(action=bad):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(action=bad))
                self.assertEqual(cm.exception.code, "BAD_ACTION")

    def test_missing_action(self):
        for d in (payload(action=None), payload(action=""), payload(action="   ")):
            with self.assertRaises(SignalError) as cm:
                parse(d)
            self.assertEqual(cm.exception.code, "BAD_ACTION")

    def test_missing_action_with_flat_position_is_close(self):
        self.assertEqual(self.action_of(action=None, position="flat"), "close")
        self.assertEqual(self.action_of(action=None, market_position="flat"), "close")
        self.assertEqual(self.action_of(action=None, market_position="FLAT"), "close")
        self.assertEqual(self.action_of(action="", market_position=" Flat "), "close")

    def test_missing_action_with_non_flat_position(self):
        for pos in ("long", "short", "", "0"):
            with self.subTest(position=pos):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(action=None, market_position=pos))
                self.assertEqual(cm.exception.code, "BAD_ACTION")

    def test_buy_sell_with_flat_market_position_is_close(self):
        self.assertEqual(self.action_of(action="sell", market_position="flat"), "close")
        self.assertEqual(self.action_of(action="buy", market_position="flat"), "close")
        self.assertEqual(self.action_of(action="buy", position="flat"), "close")
        self.assertEqual(self.action_of(action="long", market_position="flat"), "close")
        self.assertEqual(self.action_of(action="short", market_position="Flat"), "close")

    def test_buy_sell_with_open_market_position_stays_entry(self):
        self.assertEqual(self.action_of(action="sell", market_position="short"), "sell")
        self.assertEqual(self.action_of(action="buy", market_position="long"), "buy")

    def test_partial_exit_is_refused_not_an_entry(self):
        # a sell while the strategy is still long (or a buy while still short) is a partial exit
        for action, pos in (("sell", "long"), ("buy", "short"), ("short", "long"), ("long", "Short")):
            with self.subTest(action=action, position=pos):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(action=action, market_position=pos, sl=1.09))
                self.assertEqual(cm.exception.code, "BAD_ACTION")
                self.assertIn("PARTIAL_EXIT", cm.exception.message)

    def test_flat_exit_closes_only_the_side_it_closed(self):
        self.assertEqual(parse(payload(action="sell", market_position="flat")).side, "buy")
        self.assertEqual(parse(payload(action="buy", market_position="flat")).side, "sell")
        self.assertIsNone(parse(payload(action=None, market_position="flat")).side)
        # an explicit side wins
        self.assertEqual(parse(payload(action="sell", market_position="flat", side="short")).side, "sell")

    def test_flat_position_does_not_change_close_all(self):
        self.assertEqual(self.action_of(action="close_all", market_position="flat"), "close_all")

    def test_market_position_preferred_over_position(self):
        self.assertEqual(self.action_of(action="buy", market_position="long", position="flat"), "buy")
        self.assertEqual(self.action_of(action="buy", market_position="flat", position="long"), "close")

    def test_tradingview_strategy_exit_template(self):
        # ALERTS.md section 6: closing a long arrives as sell + market_position flat.
        d = {"secret": SECRET, "time": T0_ISO, "symbol": "EURUSD", "price": 1.0855,
             "action": "sell", "market_position": "flat"}
        sig = parse(d)
        self.assertEqual(sig.action, "close")
        self.assertEqual(sig.symbol, "EURUSD.h")
        self.assertEqual(sig.side, "buy")       # the sell closed a long


class OrderMergeTests(unittest.TestCase):
    def test_order_dict_merges_and_wins(self):
        d = payload(action="sell", sl=None, tp=None, order={"action": "buy", "sl": 1.08, "tp": 1.09})
        sig = parse(d)
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.sl, 1.08)
        self.assertEqual(sig.tp, 1.09)
        self.assertEqual(sig.raw["action"], "buy")

    def test_order_json_string_merges(self):
        order = json.dumps({"action": "sell", "sl": 1.0900, "tp": 1.0800, "quote_usd": 1})
        sig = parse(payload(action=None, sl=None, tp=None, order=order))
        self.assertEqual(sig.action, "sell")
        self.assertEqual((sig.sl, sig.tp, sig.quote_usd), (1.09, 1.08, 1.0))
        self.assertIsInstance(sig.raw["order"], dict)

    def test_order_string_with_whitespace_and_bom(self):
        sig = parse(payload(action=None, order='﻿  {"action": "close"}  '))
        self.assertEqual(sig.action, "close")

    def test_double_encoded_order_string(self):
        order = json.dumps(json.dumps({"action": "close"}))
        self.assertEqual(parse(payload(action=None, order=order)).action, "close")

    def test_order_keys_win_over_outer_keys(self):
        d = payload(symbol="GBPUSD", price=1.30, order={"symbol": "EURUSD", "price": 1.0855, "strategy": "inner"})
        sig = parse(d)
        self.assertEqual(sig.tv_symbol, "EURUSD")
        self.assertEqual(sig.price, 1.0855)
        self.assertEqual(sig.strategy, "inner")

    def test_outer_fields_used_when_not_in_order(self):
        d = {"secret": SECRET, "time": T0_ISO, "symbol": "EURUSD", "price": 1.0855,
             "order": {"action": "buy", "sl": 1.0825}}
        sig = parse(d)
        self.assertEqual((sig.action, sig.symbol, sig.price, sig.sl), ("buy", "EURUSD.h", 1.0855, 1.0825))
        self.assertEqual(sig.fired_at, T0)

    def test_alerts_md_strategy_example(self):
        body = ('{"secret":"%s","time":"2026-10-01T09:56:00Z","symbol":"EURUSD","price":1.08550,'
                '"order":{"action":"buy","sl":1.08250,"tp":1.09150,"quote_usd":1,"strategy":"tvb-example"}}'
                % SECRET).encode()
        sig = parse(decode_body(body))
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.symbol, "EURUSD.h")
        self.assertEqual((sig.price, sig.sl, sig.tp, sig.quote_usd), (1.0855, 1.0825, 1.0915, 1.0))
        self.assertEqual(sig.strategy, "tvb-example")
        self.assertEqual(sig.fired_at, T0)

    def test_empty_or_null_order_is_ignored(self):
        for order in ("", "   ", None):
            with self.subTest(order=order):
                d = payload()
                d["order"] = order
                self.assertEqual(parse(d).action, "buy")

    def test_invalid_order_string_is_bad_json(self):
        for order in ('{"action": "buy"', "{'action': 'buy'}", "buy", "[1,2]", "42", "null"):
            with self.subTest(order=order):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(order=order))
                self.assertEqual(cm.exception.code, "BAD_JSON")

    def test_non_object_order_is_bad_json(self):
        for order in ([{"action": "buy"}], 5, True):
            with self.subTest(order=order):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(order=order))
                self.assertEqual(cm.exception.code, "BAD_JSON")

    def test_secret_removed_from_raw_including_order(self):
        d = payload(passphrase="also-secret", order={"action": "buy", "secret": SECRET, "Passphrase": "p"})
        sig = parse(d)
        self.assertNotIn("secret", sig.raw)
        self.assertNotIn("passphrase", sig.raw)
        self.assertNotIn("secret", sig.raw["order"])
        self.assertNotIn("Passphrase", sig.raw["order"])
        dumped = json.dumps(sig.to_dict())
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("also-secret", dumped)

    def test_secret_removed_from_order_string(self):
        order = json.dumps({"action": "buy", "secret": SECRET})
        sig = parse(payload(order=order))
        self.assertNotIn(SECRET, json.dumps(sig.raw))

    def test_input_not_mutated(self):
        d = payload(order={"action": "sell", "secret": SECRET})
        before = json.dumps(d, sort_keys=True)
        parse(d)
        self.assertEqual(json.dumps(d, sort_keys=True), before)


class SymbolTests(unittest.TestCase):
    def test_normalization_variants(self):
        for given in ("EURUSD", "OANDA:EURUSD", "FX:EURUSD", "EUR/USD", "eurusd", "EURUSD.h",
                      "EURUSD.H", "EUR_USD", "  FX_IDC:eur/usd  "):
            with self.subTest(symbol=given):
                sig = parse(payload(symbol=given))
                self.assertEqual(sig.tv_symbol, "EURUSD")
                self.assertEqual(sig.symbol, "EURUSD.h")

    def test_ticker_alias(self):
        sig = parse(payload(symbol=None, ticker="GBPUSD"))
        self.assertEqual((sig.tv_symbol, sig.symbol), ("GBPUSD", "GBPUSD.h"))

    def test_symbol_preferred_over_ticker(self):
        sig = parse(payload(symbol="EURUSD", ticker="GBPUSD"))
        self.assertEqual(sig.tv_symbol, "EURUSD")

    def test_empty_symbol_falls_back_to_ticker(self):
        sig = parse(payload(symbol="", ticker="USDJPY"))
        self.assertEqual(sig.symbol, "USDJPY.h")

    def test_symbol_map(self):
        cfg = make_cfg(symbols={"map": {"XAUUSD": "GOLD.h"}})
        sig = parse(payload(symbol="OANDA:XAUUSD", price=2400.0, sl=2390.0, tp=None), cfg=cfg)
        self.assertEqual((sig.tv_symbol, sig.symbol), ("XAUUSD", "GOLD.h"))

    def test_missing_symbol(self):
        for action in ("buy", "sell", "close"):
            for d in (payload(action=action, symbol=None), payload(action=action, symbol=""),
                      payload(action=action, symbol="  "), payload(action=action, symbol="OANDA:")):
                with self.subTest(action=action, symbol=d.get("symbol")):
                    with self.assertRaises(SignalError) as cm:
                        parse(d)
                    self.assertEqual(cm.exception.code, "NO_SYMBOL")

    def test_close_all_without_symbol(self):
        sig = parse({"secret": SECRET, "time": T0_ISO, "action": "close_all"})
        self.assertEqual(sig.action, "close_all")
        self.assertEqual(sig.tv_symbol, "")
        self.assertEqual(sig.symbol, "")
        self.assertIsNone(sig.side)

    def test_close_all_ignores_symbol(self):
        sig = parse(payload(action="flatten", symbol="BTCUSD"))
        self.assertEqual((sig.action, sig.tv_symbol, sig.symbol), ("close_all", "", ""))

    def test_symbol_not_allowed(self):
        for action in ("buy", "close"):
            for sym in ("BTCUSD", "OANDA:EURGBP", "US30"):
                with self.subTest(action=action, symbol=sym):
                    with self.assertRaises(SignalError) as cm:
                        parse(payload(action=action, symbol=sym))
                    self.assertEqual(cm.exception.code, "SYMBOL_NOT_ALLOWED")

    def test_allowed_list_restricts(self):
        cfg = make_cfg(symbols={"allowed": ["EURUSD"]})
        self.assertEqual(parse(payload(symbol="EURUSD"), cfg=cfg).symbol, "EURUSD.h")
        with self.assertRaises(SignalError) as cm:
            parse(payload(symbol="GBPUSD"), cfg=cfg)
        self.assertEqual(cm.exception.code, "SYMBOL_NOT_ALLOWED")

    def test_close_for_a_symbol_removed_from_allowed_still_parses(self):
        cfg = make_cfg(symbols={"allowed": ["EURUSD"]})
        sig = parse(payload(action="close", symbol="GBPUSD"), cfg=cfg)     # still in specs
        self.assertEqual((sig.action, sig.symbol), ("close", "GBPUSD.h"))
        with self.assertRaises(SignalError):
            parse(payload(action="buy", symbol="GBPUSD"), cfg=cfg)         # entries stay refused
        # a symbol without a spec: only when a position on it is open (the server asks the ledger)
        with self.assertRaises(SignalError):
            parse(payload(action="close", symbol="BTCUSD"), cfg=cfg)
        sig = parse_payload(payload(action="close", symbol="BTCUSD"), cfg, RECEIVED,
                            exit_symbol_ok=lambda s: s == "BTCUSD.h")
        self.assertEqual(sig.symbol, "BTCUSD.h")

    def test_numeric_symbol_value(self):
        with self.assertRaises(SignalError) as cm:
            parse(payload(symbol=12345))
        self.assertEqual(cm.exception.code, "SYMBOL_NOT_ALLOWED")


class NumberTests(unittest.TestCase):
    def test_numeric_forms(self):
        cases = [(1.0855, 1.0855), (1, 1.0), ("1.0855", 1.0855), (" 1.0855 ", 1.0855),
                 ("1.", 1.0), (".5", 0.5), ("+1.5", 1.5), ("1e-3", 0.001), ("2E2", 200.0)]
        for given, want in cases:
            with self.subTest(price=given):
                self.assertEqual(parse(payload(price=given)).price, want)

    def test_price_alias_close(self):
        self.assertEqual(parse(payload(price=None, close="1.0860")).price, 1.086)

    def test_price_preferred_over_close(self):
        self.assertEqual(parse(payload(price=1.1, close=1.2)).price, 1.1)

    def test_sl_aliases(self):
        for key in ("sl", "stop", "stop_loss"):
            with self.subTest(key=key):
                d = payload(sl=None)
                d[key] = "1.0800"
                self.assertEqual(parse(d).sl, 1.08)

    def test_tp_aliases(self):
        for key in ("tp", "take_profit", "limit"):
            with self.subTest(key=key):
                d = payload(tp=None)
                d[key] = 1.0950
                self.assertEqual(parse(d).tp, 1.095)

    def test_risk_aliases(self):
        self.assertEqual(parse(payload(risk_pct="0.25")).risk_pct, 0.25)
        self.assertEqual(parse(payload(risk=0.4)).risk_pct, 0.4)
        self.assertEqual(parse(payload(risk_pct=0.3, risk=0.9)).risk_pct, 0.3)

    def test_quote_usd(self):
        self.assertEqual(parse(payload(quote_usd="1.27")).quote_usd, 1.27)
        self.assertIsNone(parse(payload()).quote_usd)

    def test_optional_numbers_absent(self):
        sig = parse(payload(price=None, sl=None, tp=None))
        self.assertIsNone(sig.price)
        self.assertIsNone(sig.sl)
        self.assertIsNone(sig.tp)
        self.assertIsNone(sig.risk_pct)

    def test_unset_values_for_sl_tp(self):
        unset = ("", "  ", "nan", "NaN", "NAN", float("nan"), None, "0", 0, 0.0, "0.0", "0.00000", "-0")
        for v in unset:
            with self.subTest(value=v):
                d = payload(sl=None, tp=None)
                d["sl"] = v
                d["tp"] = v
                sig = parse(d)
                self.assertIsNone(sig.sl)
                self.assertIsNone(sig.tp)

    def test_zero_is_kept_for_price_and_risk(self):
        sig = parse(payload(price="0", risk_pct=0))
        self.assertEqual(sig.price, 0.0)
        self.assertEqual(sig.risk_pct, 0.0)

    def test_nan_and_empty_for_price(self):
        for v in ("", "nan", "NaN", float("nan")):
            with self.subTest(value=v):
                self.assertIsNone(parse(payload(price=v)).price)

    def test_bad_numbers_on_entry(self):
        bad = ("abc", "1,0855", "1.08.5", "1 085", True, False, [1.0], {"v": 1}, "inf", "-Infinity",
               float("inf"), "1_000", "0x10", "{{close}}", "1.0855abc", 10 ** 400)
        for key in ("price", "sl", "tp", "risk_pct", "quote_usd"):
            for v in bad:
                with self.subTest(key=key, value=v):
                    d = payload()
                    d[key] = v
                    with self.assertRaises(SignalError) as cm:
                        parse(d)
                    self.assertEqual(cm.exception.code, "BAD_NUMBER")
                    self.assertIn(key, cm.exception.message)

    def test_bad_number_in_alias_key(self):
        for key in ("close", "stop", "stop_loss", "take_profit", "limit", "risk"):
            with self.subTest(key=key):
                d = payload()
                d[key] = "oops"
                with self.assertRaises(SignalError) as cm:
                    parse(d)
                self.assertEqual(cm.exception.code, "BAD_NUMBER")

    def test_unset_alias_falls_through_to_next(self):
        d = payload(sl="")
        d["stop"] = 1.0805
        self.assertEqual(parse(d).sl, 1.0805)
        d = payload(sl=0)
        d["stop_loss"] = "1.0811"
        self.assertEqual(parse(d).sl, 1.0811)

    def test_bad_numbers_do_not_block_exits(self):
        # Fail open for exits: an irrelevant broken field must not prevent a close.
        d = payload(action="close", price="abc", sl="{{plot}}", tp=[1], risk_pct="x", quote_usd="y")
        sig = parse(d)
        self.assertEqual(sig.action, "close")
        self.assertIsNone(sig.price)
        self.assertIsNone(sig.sl)
        self.assertIsNone(sig.tp)
        self.assertEqual(sig.raw["price"], "abc")
        sig = parse({"secret": SECRET, "time": T0_ISO, "action": "close_all", "price": "bad"})
        self.assertEqual(sig.action, "close_all")
        self.assertIsNone(sig.price)
        sig = parse(payload(action="sell", market_position="flat", sl="garbage"))
        self.assertEqual(sig.action, "close")

    def test_exit_still_parses_valid_numbers(self):
        sig = parse(payload(action="close", price="1.0870"))
        self.assertEqual(sig.price, 1.087)


class SideTests(unittest.TestCase):
    def test_close_side_normalized(self):
        cases = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell", " LONG ": "buy", "Sell": "sell"}
        for given, want in cases.items():
            with self.subTest(side=given):
                self.assertEqual(parse(payload(action="close", side=given)).side, want)

    def test_close_side_other_values(self):
        for given in ("both", "", None, "flat", 1):
            with self.subTest(side=given):
                d = payload(action="close")
                d["side"] = given
                self.assertIsNone(parse(d).side)

    def test_side_only_for_close(self):
        self.assertIsNone(parse(payload(action="buy", side="sell")).side)
        self.assertIsNone(parse(payload(action="sell", side="buy")).side)
        self.assertIsNone(parse(payload(action="close_all", side="buy")).side)

    def test_side_from_order(self):
        sig = parse(payload(action=None, order={"action": "exit", "side": "short"}))
        self.assertEqual((sig.action, sig.side), ("close", "sell"))


class TimeTests(unittest.TestCase):
    def test_iso_variants(self):
        for given in ("2026-10-01T09:56:00Z", "2026-10-01T09:56:00.000Z", "2026-10-01T09:56:00+00:00",
                      "2026-10-01 09:56:00", "2026-10-01T12:56:00+03:00"):
            with self.subTest(time=given):
                sig = parse(payload(time=given))
                self.assertEqual(sig.fired_at, T0)
                self.assertEqual(sig.fired_at.utcoffset(), timedelta(0))

    def test_epoch_values(self):
        secs = int(T0.timestamp())
        for given in (secs, secs * 1000, str(secs), str(secs * 1000), float(secs)):
            with self.subTest(time=given):
                self.assertEqual(parse(payload(time=given)).fired_at, T0)

    def test_aliases(self):
        self.assertEqual(parse(payload(time=None, timenow=T0_ISO)).fired_at, T0)
        self.assertEqual(parse(payload(time=None, fired=T0_ISO)).fired_at, T0)

    def test_time_preferred_over_aliases(self):
        sig = parse(payload(time=T0_ISO, timenow="2026-10-01T10:00:00Z"))
        self.assertEqual(sig.fired_at, T0)

    def test_missing_time(self):
        for d in (payload(time=None), payload(time=""), payload(time="   ")):
            with self.assertRaises(SignalError) as cm:
                parse(d)
            self.assertEqual(cm.exception.code, "NO_TIME")

    def test_invalid_time(self):
        for bad in ("{{timenow}}", "yesterday", "2026-13-01T00:00:00Z", "2026-10-01T25:00:00Z", True,
                    [T0_ISO], {"t": 1}):
            with self.subTest(time=bad):
                with self.assertRaises(SignalError) as cm:
                    parse(payload(time=bad))
                self.assertEqual(cm.exception.code, "NO_TIME")

    def test_close_all_needs_time_too(self):
        with self.assertRaises(SignalError) as cm:
            parse({"secret": SECRET, "action": "close_all"})
        self.assertEqual(cm.exception.code, "NO_TIME")

    def test_received_at_is_kept_and_utc(self):
        sig = parse(payload(), received_at=RECEIVED)
        self.assertEqual(sig.received_at, RECEIVED)
        naive = datetime(2026, 10, 1, 9, 56, 5)
        sig = parse(payload(), received_at=naive)
        self.assertEqual(sig.received_at, naive.replace(tzinfo=UTC))


class IdTests(unittest.TestCase):
    def test_provided_id_prefixed_with_action(self):
        self.assertEqual(parse(payload(id="abc-1")).id, "buy:abc-1")
        self.assertEqual(parse(payload(action="short", id="abc-1")).id, "sell:abc-1")
        self.assertEqual(parse(payload(action="close", id="abc-1")).id, "close:abc-1")
        self.assertEqual(parse(payload(action="flatten", id="abc-1")).id, "close_all:abc-1")

    def test_provided_id_uses_resolved_action(self):
        sig = parse(payload(action="sell", market_position="flat", id="x"))
        self.assertEqual(sig.id, "close:x")

    def test_numeric_id(self):
        self.assertEqual(parse(payload(id=123)).id, "buy:123")

    def test_id_from_order(self):
        self.assertEqual(parse(payload(order={"id": "inner"})).id, "buy:inner")

    def test_long_id_truncated(self):
        sig = parse(payload(id="x" * 500))
        self.assertEqual(sig.id, "buy:" + "x" * 128)

    def test_id_whitespace_stripped(self):
        self.assertEqual(parse(payload(id="  abc  ")).id, "buy:abc")

    def test_empty_id_is_hashed(self):
        for given in ("", "   ", None):
            with self.subTest(id=given):
                d = payload()
                d["id"] = given
                sid = parse(d).id
                self.assertRegex(sid, r"^[0-9a-f]{32}$")

    def test_hashed_id_formula(self):
        sig = parse(payload())
        canon = json.dumps(["buy", "EURUSD.h", None, 1.0855, 1.0825, 1.0915, T0_ISO, "s1"],
                           separators=(",", ":"))
        self.assertEqual(sig.id, hashlib.sha256(canon.encode()).hexdigest()[:32])

    def test_same_payload_same_id(self):
        a = parse(payload(), received_at=RECEIVED)
        b = parse(payload(), received_at=RECEIVED + timedelta(seconds=30))
        self.assertEqual(a.id, b.id)

    def test_equivalent_payload_same_id(self):
        a = parse(payload())
        b = parse(payload(price="1.0855", sl="1.08250", symbol="EUR/USD", time="2026-10-01T09:56:00.000+00:00",
                          secret="something-else"))
        self.assertEqual(a.id, b.id)

    def test_different_time_different_id(self):
        a = parse(payload(time="2026-10-01T09:56:00Z"))
        b = parse(payload(time="2026-10-01T09:57:00Z"))
        self.assertNotEqual(a.id, b.id)

    def test_other_fields_change_hash(self):
        base = parse(payload()).id
        variants = [payload(action="sell"), payload(price=1.0856), payload(sl=1.0826), payload(tp=1.0916),
                    payload(tp=None), payload(strategy="s2"), payload(symbol="GBPUSD")]
        ids = {parse(v).id for v in variants}
        self.assertNotIn(base, ids)
        self.assertEqual(len(ids), len(variants))

    def test_close_side_changes_hash(self):
        a = parse(payload(action="close", side="buy")).id
        b = parse(payload(action="close", side="sell")).id
        c = parse(payload(action="close")).id
        self.assertEqual(len({a, b, c}), 3)

    def test_fields_not_in_hash(self):
        a = parse(payload()).id
        self.assertEqual(parse(payload(comment="other")).id, a)
        self.assertEqual(parse(payload(risk_pct=0.25)).id, a)


class StrategyCommentTests(unittest.TestCase):
    def test_strategy_truncated(self):
        self.assertEqual(parse(payload(strategy="s" * 100)).strategy, "s" * 64)

    def test_strategy_default_empty(self):
        self.assertEqual(parse(payload(strategy=None)).strategy, "")

    def test_strategy_non_string(self):
        self.assertEqual(parse(payload(strategy=42)).strategy, "42")

    def test_comment_truncated(self):
        self.assertEqual(parse(payload(comment="c" * 40)).comment, "c" * 24)

    def test_comment_default(self):
        self.assertEqual(parse(payload()).comment, "tvb")
        self.assertEqual(parse(payload(comment="")).comment, "tvb")
        self.assertEqual(parse(payload(comment="my-label")).comment, "my-label")


class ParsePayloadGeneralTests(unittest.TestCase):
    def test_full_buy_signal(self):
        sig = parse(payload(id="t1", risk_pct=0.25, comment="c1"))
        self.assertIsInstance(sig, Signal)
        self.assertEqual(sig.id, "buy:t1")
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.tv_symbol, "EURUSD")
        self.assertEqual(sig.symbol, "EURUSD.h")
        self.assertIsNone(sig.side)
        self.assertEqual((sig.price, sig.sl, sig.tp, sig.risk_pct), (1.0855, 1.0825, 1.0915, 0.25))
        self.assertEqual(sig.fired_at, T0)
        self.assertEqual(sig.received_at, RECEIVED)
        self.assertEqual(sig.strategy, "s1")
        self.assertEqual(sig.comment, "c1")
        self.assertNotIn("secret", sig.raw)
        self.assertEqual(sig.raw["symbol"], "OANDA:EURUSD")

    def test_round_trips_through_to_dict(self):
        sig = parse(payload(order={"action": "buy", "sl": 1.08}))
        again = Signal.from_dict(json.loads(json.dumps(sig.to_dict())))
        self.assertEqual(again, sig)

    def test_not_a_dict(self):
        for bad in ([], "x", None, 5):
            with self.subTest(d=bad):
                with self.assertRaises(SignalError) as cm:
                    parse_payload(bad, CFG, RECEIVED)  # type: ignore[arg-type]
                self.assertEqual(cm.exception.code, "BAD_JSON")

    def test_check_order_action_before_symbol(self):
        # Error precedence follows the spec's rule order.
        with self.assertRaises(SignalError) as cm:
            parse(payload(action="hold", symbol=None, time=None))
        self.assertEqual(cm.exception.code, "BAD_ACTION")
        with self.assertRaises(SignalError) as cm:
            parse(payload(symbol="BTCUSD", sl="bad", time=None))
        self.assertEqual(cm.exception.code, "SYMBOL_NOT_ALLOWED")
        with self.assertRaises(SignalError) as cm:
            parse(payload(sl="bad", time=None))
        self.assertEqual(cm.exception.code, "BAD_NUMBER")


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        clock.set_clock(lambda: T0)
        self.addCleanup(clock.set_clock, None)

    def sig_fired(self, delta_s):
        fired = T0 + timedelta(seconds=delta_s)
        return parse(payload(time=clock.iso(fired)), received_at=clock.utcnow())

    def test_fresh(self):
        for delta in (0, -1, -60, -120, 1, 30):
            with self.subTest(delta=delta):
                self.assertIsNone(check_freshness(self.sig_fired(delta), clock.utcnow(), CFG))

    def test_stale(self):
        for delta in (-120.001, -121, -3600, -86400 * 365):
            with self.subTest(delta=delta):
                self.assertEqual(check_freshness(self.sig_fired(delta), clock.utcnow(), CFG), "STALE")

    def test_future(self):
        for delta in (30.001, 31, 3600):
            with self.subTest(delta=delta):
                self.assertEqual(check_freshness(self.sig_fired(delta), clock.utcnow(), CFG), "FUTURE")

    def test_uses_config_limits(self):
        cfg = make_cfg(server={"max_signal_age_s": 10, "max_future_skew_s": 0})
        self.assertIsNone(check_freshness(self.sig_fired(-10), clock.utcnow(), cfg))
        self.assertEqual(check_freshness(self.sig_fired(-11), clock.utcnow(), cfg), "STALE")
        self.assertIsNone(check_freshness(self.sig_fired(0), clock.utcnow(), cfg))
        self.assertEqual(check_freshness(self.sig_fired(1), clock.utcnow(), cfg), "FUTURE")

    def test_frozen_clock_moves(self):
        sig = self.sig_fired(0)
        clock.set_clock(lambda: T0 + timedelta(seconds=121))
        self.assertEqual(check_freshness(sig, clock.utcnow(), CFG), "STALE")
        clock.set_clock(lambda: T0 - timedelta(seconds=31))
        self.assertEqual(check_freshness(sig, clock.utcnow(), CFG), "FUTURE")

    def test_naive_now_treated_as_utc(self):
        sig = self.sig_fired(0)
        self.assertIsNone(check_freshness(sig, T0.replace(tzinfo=None), CFG))

    def test_epoch_zero_is_stale(self):
        sig = parse(payload(time=0), received_at=clock.utcnow())
        self.assertEqual(check_freshness(sig, clock.utcnow(), CFG), "STALE")


if __name__ == "__main__":
    unittest.main()
