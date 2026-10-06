import unittest

from tvbridge.gui.driver import OcrItem
from tvbridge.gui.parse import (
    account_line_y, find_items, find_symbol, find_trade_header, group_rows, nearest_label, parse_account_line,
    parse_number, parse_order_result, parse_position_rows, result_side_volume, row_text, scan_numbers,
    verify_dialog_fields,
)

KNOWN = ["EURUSD.h", "GBPUSD.h", "USDJPY.h", "XAUUSD.h"]

ACCOUNT_LINE = ("Balance: 50 000.00 USD  Equity: 49 812.35  Margin: 1 083.45  "
                "Free Margin: 48 728.90  Margin Level: 4 597.60 %")
EURUSD_ROW = "EURUSD.h   52390671   2026.10.01 10:15:02   buy   0.50   1.08345   1.08100   1.08900   1.08311   -17.00"


def item(text, x=0.0, y=0.0, w=None, h=12.0, conf=0.9):
    """OcrItem whose width defaults to ~6 pt per character."""
    return OcrItem(text=text, conf=conf, x=float(x), y=float(y), w=float(w if w is not None else 6 * len(text)),
                   h=float(h))


class ParseNumberTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "50 000.00": 50000.0,
            "50,000.00": 50000.0,
            "-17.00": -17.0,
            "−17.00": -17.0,          # Unicode minus
            "− 17.00": -17.0,
            "50 000.00": 50000.0,     # no-break space
            "50 000.00": 50000.0,     # thin space
            "50 000.00": 50000.0,     # narrow no-break space
            "4 597.60 %": 4597.6,
            "4 597.60%": 4597.6,
            "50 000.00 USD": 50000.0,
            "1.08345": 1.08345,
            "0.50": 0.5,
            "0.00000": 0.0,
            "+3.5": 3.5,
            "17": 17.0,
            "  1 083.45  ": 1083.45,
            "1 000 000.25": 1000000.25,
        }
        for s, want in cases.items():
            got = parse_number(s)
            self.assertIsNotNone(got, s)
            self.assertAlmostEqual(got, want, places=9, msg=s)

    def test_rejects_garbage_and_comma_decimals(self):
        for s in ("", "   ", "abc", "USD", "50 000,00", "1,5", "1.2.3", "12 34", "--5", None, "Balance: 5.00"):
            self.assertIsNone(parse_number(s), repr(s))

    def test_scan_numbers_skips_dates_and_times(self):
        self.assertEqual(scan_numbers("2026.10.01 10:15:02 buy 0.50 1.08345 -1 017.00"),
                         [0.5, 1.08345, -1017.0])
        self.assertEqual(scan_numbers("52390671 x"), [52390671.0])
        self.assertEqual(scan_numbers("1 017.00", grouped=False), [1.0, 17.0])


class GroupRowsTests(unittest.TestCase):
    def test_clusters_by_center_and_sorts_by_x(self):
        a = item("right", x=200, y=100)
        b = item("left", x=10, y=102)
        c = item("next", x=10, y=120)
        rows = group_rows([a, c, b])
        self.assertEqual([[i.text for i in r] for r in rows], [["left", "right"], ["next"]])

    def test_dedupes_identical_overlapping_observations(self):
        a = item("Buy by Market", x=100, y=50, conf=0.5)
        b = item("Buy by Market", x=101.5, y=51, conf=0.9)
        far = item("Buy by Market", x=300, y=50)
        rows = group_rows([a, b, far])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 2)
        self.assertIn(b, rows[0])           # the more confident duplicate is kept
        self.assertNotIn(a, rows[0])

    def test_row_text_full_line_plus_partials(self):
        full = item(EURUSD_ROW, x=10, y=100, w=700)
        p1 = item("EURUSD.h 52390671 2026.10.01 10:15:02 buy", x=10, y=100.5, w=300)
        p2 = item("0.50 1.08345 1.08100 1.08900 1.08311 -17.00", x=330, y=99.5, w=380)
        rows = group_rows([p1, full, p2])
        self.assertEqual(len(rows), 1)
        self.assertEqual(row_text(rows[0]), " ".join(EURUSD_ROW.split()))

    def test_row_text_merges_overlapping_segments(self):
        p1 = item("EURUSD.h 52390671 buy 0.50", x=10, y=100, w=300)
        p2 = item("0.50 1.08345 -17.00", x=280, y=100, w=200)
        self.assertEqual(row_text(group_rows([p1, p2])[0]), "EURUSD.h 52390671 buy 0.50 1.08345 -17.00")

    def test_row_text_rejoins_split_thousands(self):
        a = item("Equity: 49", x=10, y=10, w=60)
        b = item("812.35", x=74, y=10, w=36)
        self.assertEqual(row_text(group_rows([a, b])[0]), "Equity: 49 812.35")


class AccountLineTests(unittest.TestCase):
    def test_full_line(self):
        acc = parse_account_line([item(ACCOUNT_LINE, x=10, y=300, w=760, conf=0.5)])
        self.assertEqual(acc, {"balance": 50000.0, "equity": 49812.35, "margin": 1083.45, "free_margin": 48728.90})

    def test_segments_and_separate_labels(self):
        items = [
            item("Balance:", x=10, y=300, w=48),
            item("50 000.00 USD", x=62, y=300, w=80),
            item("Equity: 49 812.35", x=160, y=300, w=100),
            item("Margin: 1 083.45", x=280, y=300, w=95),
            item("Free margin: 48 728.90", x=390, y=300, w=130),
            item("Margin Level: 4 597.60 %", x=540, y=300, w=140),
        ]
        acc = parse_account_line(items)
        self.assertEqual(acc, {"balance": 50000.0, "equity": 49812.35, "margin": 1083.45, "free_margin": 48728.90})

    def test_full_line_with_overlapping_partials(self):
        items = [
            item(ACCOUNT_LINE, x=10, y=300, w=760, conf=0.5),
            item("Balance: 50 000.00 USD Equity: 49 812.35", x=10, y=300.5, w=250, conf=0.5),
            item("Margin: 1 083.45 Free Margin: 48 728.90", x=280, y=299.5, w=260, conf=0.5),
        ]
        self.assertEqual(parse_account_line(items)["equity"], 49812.35)

    def test_split_thousands_value(self):
        items = [item("Balance: 50", x=10, y=300, w=70), item("000.00 USD Equity: 49 812.35", x=84, y=300, w=170)]
        acc = parse_account_line(items)
        self.assertEqual((acc["balance"], acc["equity"]), (50000.0, 49812.35))
        self.assertNotIn("margin", acc)

    def test_margin_level_not_mistaken_for_margin(self):
        acc = parse_account_line([item("Balance: 100.00 Equity: 90.00 Margin Level: 4 597.60 %", y=5)])
        self.assertEqual(acc, {"balance": 100.0, "equity": 90.0})

    def test_requires_balance_and_equity(self):
        self.assertIsNone(parse_account_line([item("Balance: 50 000.00 USD", y=5)]))
        self.assertIsNone(parse_account_line([item("Equity: 49 812.35", y=5)]))
        self.assertIsNone(parse_account_line([]))

    def test_truncated_value_is_not_accepted(self):
        # a value cut by OCR before its decimals must not become 49.0
        self.assertIsNone(parse_account_line([item("Balance: 50 000.00 USD Equity: 49", y=5)]))

    def test_ignores_position_rows(self):
        items = [item(EURUSD_ROW, y=100, w=700), item(ACCOUNT_LINE, y=140, w=760)]
        self.assertEqual(parse_account_line(items)["balance"], 50000.0)


class PositionRowTests(unittest.TestCase):
    def rows(self, *lines, **kw):
        return [item(t, x=10, y=100 + 20 * i, w=700) for i, t in enumerate(lines)]

    def test_full_row(self):
        res = parse_position_rows(self.rows(EURUSD_ROW), KNOWN)
        self.assertEqual(len(res), 1)
        pos, anchor = res[0]
        self.assertEqual((pos.symbol, pos.side, pos.lots, pos.ticket), ("EURUSD.h", "buy", 0.5, "52390671"))
        self.assertAlmostEqual(pos.open_price, 1.08345)
        self.assertAlmostEqual(pos.sl, 1.081)
        self.assertAlmostEqual(pos.tp, 1.089)
        self.assertAlmostEqual(pos.profit, -17.0)
        self.assertIn("EURUSD.h", anchor.text)

    def test_split_segments(self):
        p1 = item("EURUSD.h 52390671 2026.10.01 10:15:02 buy", x=10, y=100, w=300)
        p2 = item("0.50 1.08345 1.08100 1.08900 1.08311 -17.00", x=330, y=100, w=380)
        res = parse_position_rows([p2, p1], KNOWN)
        self.assertEqual(len(res), 1)
        pos, anchor = res[0]
        self.assertEqual((pos.symbol, pos.side, pos.lots), ("EURUSD.h", "buy", 0.5))
        self.assertAlmostEqual(pos.profit, -17.0)
        self.assertIs(anchor, p1)

    def test_full_line_and_partials_not_double_counted(self):
        full = item(EURUSD_ROW, x=10, y=100, w=700, conf=0.5)
        p1 = item("EURUSD.h 52390671 2026.10.01 10:15:02 buy", x=10, y=100.5, w=300, conf=0.5)
        p2 = item("0.50 1.08345 1.08100 1.08900 1.08311 -17.00", x=330, y=99.5, w=380, conf=0.5)
        res = parse_position_rows([full, p1, p2], KNOWN)
        self.assertEqual(len(res), 1)
        pos, anchor = res[0]
        self.assertEqual(pos.lots, 0.5)
        self.assertIs(anchor, p1)   # narrowest observation holding the symbol

    def test_same_ticket_on_two_rows_counted_once(self):
        res = parse_position_rows(self.rows(EURUSD_ROW, EURUSD_ROW), KNOWN)
        self.assertEqual(len(res), 1)

    def test_several_rows_sell_and_jpy(self):
        lines = [
            "Symbol Ticket Time Type Volume Price S / L T / P Price Profit",
            EURUSD_ROW,
            "XAUUSD.h 52390690 2026.10.01 11:02:40 sell 0.10 2345.67 2360.00 2320.00 2341.20 − 44.70",
            "USDJPY.h 52390702 2026.10.01 11:30:00 buy 1.00 149.123 0.000 0.000 149.200 51.60",
            ACCOUNT_LINE,
        ]
        res = parse_position_rows(self.rows(*lines), KNOWN)
        self.assertEqual([(p.symbol, p.side, p.lots) for p, _ in res],
                         [("EURUSD.h", "buy", 0.5), ("XAUUSD.h", "sell", 0.1), ("USDJPY.h", "buy", 1.0)])
        xau = res[1][0]
        self.assertAlmostEqual(xau.open_price, 2345.67)
        self.assertAlmostEqual(xau.sl, 2360.0)
        self.assertAlmostEqual(xau.tp, 2320.0)
        jpy = res[2][0]
        self.assertIsNone(jpy.sl)              # 0.000 = not set
        self.assertIsNone(jpy.tp)
        self.assertAlmostEqual(jpy.profit, 51.6)
        self.assertEqual(jpy.ticket, "52390702")

    def test_empty_sl_tp_cells(self):
        row = "GBPUSD.h 52390800 2026.10.01 12:00:00 sell 0.20 1.26500 1.26420 12.40"
        pos, _ = parse_position_rows(self.rows(row), KNOWN)[0]
        self.assertEqual((pos.side, pos.lots), ("sell", 0.2))
        self.assertAlmostEqual(pos.open_price, 1.265)
        self.assertIsNone(pos.sl)
        self.assertIsNone(pos.tp)
        self.assertAlmostEqual(pos.profit, 12.4)

    def test_profit_with_thousands_separator(self):
        row = "XAUUSD.h 52390690 2026.10.01 11:02:40 sell 2.00 2345.67 2360.00 2320.00 2351.00 -1 066.00"
        pos, _ = parse_position_rows(self.rows(row), KNOWN)[0]
        self.assertEqual(pos.lots, 2.0)
        self.assertAlmostEqual(pos.profit, -1066.0)

    def test_pending_orders_ignored(self):
        lines = [
            "EURUSD.h 52390900 2026.10.01 12:00:00 buy limit 0.50 / 0.00 1.08000 1.07500 1.09000 1.08311 placed",
            "GBPUSD.h 52390901 2026.10.01 12:00:00 sell stop 0.50 / 0.00 1.26000 0.00000 0.00000 1.26450 placed",
        ]
        self.assertEqual(parse_position_rows(self.rows(*lines), KNOWN), [])

    def test_unknown_symbols_are_read_as_generic_positions(self):
        # D3: a position on a symbol without a spec is still a position (untracked, closed by
        # flatten); the symbol is kept as read.
        lines = [
            "AUDUSD.h 52390671 2026.10.01 10:15:02 buy 0.50 0.65000 0 0 0.65010 5.00",
            "EURUSD 52390672 2026.10.01 10:15:02 buy 0.50 1.08345 0 0 1.08311 -17.00",
            "EURUSD.hx 52390673 2026.10.01 10:15:02 sell 0.20 1.08345 0 0 1.08311 -17.00",
            "#AAPL 52390674 2026.10.01 10:15:02 buy 3 190.12 0.00 0.00 191.00 2.64",
        ]
        res = parse_position_rows(self.rows(*lines), KNOWN)
        self.assertEqual([(p.symbol, p.side, p.lots, p.ticket) for p, _ in res],
                         [("AUDUSD.h", "buy", 0.5, "52390671"), ("EURUSD", "buy", 0.5, "52390672"),
                          ("EURUSD.hx", "sell", 0.2, "52390673"), ("#AAPL", "buy", 3.0, "52390674")])
        self.assertAlmostEqual(res[0][0].profit, 5.0)
        self.assertIn("AUDUSD.h", res[0][1].text)

    def test_generic_rows_need_a_ticket_and_a_symbol_first(self):
        lines = [
            "BTCUSD.h 2026.10.01 10:15:02 buy 0.50 60000.00 59000.00 0 60100.00 50.00",   # no ticket
            "2026.10.01 10:15:02 Trades '12345678': market buy 0.50 EURUSD.h sl: 1.08100",  # journal line
            "Symbol Ticket Time Type Volume Price S / L T / P Price Profit",
            "Balance: 50 000.00 USD Equity: 49 812.35",
            "GER40.cash 52390900 2026.10.01 12:00:00 buy limit 0.50 18000.0 0 0 18010.0 placed",
        ]
        self.assertEqual(parse_position_rows(self.rows(*lines), KNOWN), [])

    def test_known_symbol_wins_over_generic(self):
        res = parse_position_rows(self.rows("EURUSDh 52390671 2026.10.01 10:15:02 sell 0.30 1.08345"), KNOWN)
        self.assertEqual([p.symbol for p, _ in res], ["EURUSD.h"])

    def test_swap_column_from_header(self):
        header = "Symbol Ticket Time Type Volume Price S / L T / P Price Swap Profit"
        row = "EURUSD.h 52390671 2026.10.01 10:15:02 buy 0.50 1.08345 1.08100 1.08900 1.08311 -3.20 -17.00"
        pos, _ = parse_position_rows(self.rows(header, row), KNOWN)[0]
        self.assertAlmostEqual(pos.swap, -3.2)
        self.assertAlmostEqual(pos.profit, -17.0)
        # without a Swap column in the header nothing is guessed
        pos, _ = parse_position_rows(self.rows(EURUSD_ROW), KNOWN)[0]
        self.assertIsNone(pos.swap)

    def test_sl_missing_only_when_the_column_is_known(self):
        row = "EURUSD.h 52390671 2026.10.01 10:15:02 buy 0.50 1.08345 0.00000 1.08900 1.08311 -17.00"
        pos, _ = parse_position_rows(self.rows(row), KNOWN)[0]
        self.assertIsNone(pos.sl)
        self.assertTrue(pos.sl_missing)
        pos, _ = parse_position_rows(self.rows(EURUSD_ROW), KNOWN)[0]
        self.assertFalse(pos.sl_missing)
        # empty cells: the columns are ambiguous, so the SL is unknown, not missing
        pos, _ = parse_position_rows(self.rows("GBPUSD.h 52390800 2026.10.01 12:00:00 sell 0.20 1.26500 "
                                               "1.26420 12.40"), KNOWN)[0]
        self.assertFalse(pos.sl_missing)

    def test_header_and_account_line_helpers(self):
        items = self.rows("Symbol Ticket Time Type Volume Price S / L T / P Price Swap Profit", EURUSD_ROW,
                          ACCOUNT_LINE)
        hdr = find_trade_header(items)
        self.assertIsNotNone(hdr)
        self.assertTrue(hdr["swap"])
        self.assertLess(hdr["cy"], account_line_y(items))
        self.assertIsNone(find_trade_header(self.rows(EURUSD_ROW, ACCOUNT_LINE)))
        self.assertIsNone(account_line_y(self.rows(EURUSD_ROW)))

    def test_symbol_case_and_punctuation_tolerance(self):
        res = parse_position_rows(self.rows("eurusd.H 1 2026.10.01 10:15:02 BUY 0.50 1.08345"), KNOWN)
        self.assertEqual([(p.symbol, p.side, p.lots) for p, _ in res], [("EURUSD.h", "buy", 0.5)])
        res = parse_position_rows(self.rows("EURUSDh 52390671 2026.10.01 10:15:02 sell 0.30 1.08345"), KNOWN)
        self.assertEqual([(p.symbol, p.side, p.lots, p.ticket) for p, _ in res],
                         [("EURUSD.h", "sell", 0.3, "52390671")])

    def test_requires_side_and_lots(self):
        lines = ["EURUSD.h 52390671 2026.10.01 10:15:02", "EURUSD.h 52390672 2026.10.01 10:15:02 buy"]
        self.assertEqual(parse_position_rows(self.rows(*lines), KNOWN), [])

    def test_integer_lots_do_not_swallow_price(self):
        row = "USDJPY.h 52390702 2026.10.01 11:30:00 sell 1 149.123 0.000 0.000 149.200 -51.60"
        pos, _ = parse_position_rows(self.rows(row), KNOWN)[0]
        self.assertEqual(pos.lots, 1.0)
        self.assertAlmostEqual(pos.open_price, 149.123)

    def test_find_symbol(self):
        self.assertEqual(find_symbol("x EURUSD.h, Euro vs US Dollar", KNOWN), ("EURUSD.h", 2, 10))
        self.assertIsNone(find_symbol("EURUSD.hx", KNOWN))
        self.assertIsNone(find_symbol("", KNOWN))


class OrderResultTests(unittest.TestCase):
    def test_filled(self):
        status, msg, ticket, price = parse_order_result("Order #52390672 buy 0.5 EURUSD.h at 1.08345 done")
        self.assertEqual((status, ticket, price), ("filled", "52390672", 1.08345))
        self.assertIn("done", msg)

    def test_filled_variants(self):
        for text in ("Request executed", "Order placed", "Position filled", "DONE"):
            self.assertEqual(parse_order_result(text)[0], "filled", text)

    def test_rejected(self):
        for text in ("Not enough money", "No money", "Market closed", "Market is closed", "Trade disabled",
                     "Requote", "Off quotes", "No prices", "Too many requests", "Too frequent requests",
                     "Order rejected", "Invalid stops", "Request rejected: invalid volume"):
            self.assertEqual(parse_order_result(text)[0], "rejected", text)

    def test_ambiguous_answers_are_uncertain(self):
        # the terminal did not get the server's answer: the order may have executed
        for text in ("Request canceled by timeout", "Trade timeout", "Timed out", "Time out",
                     "No connection with the trade server", "Connection lost", "Failed", "Error 10006",
                     "Request done with error: invalid stops"):
            self.assertEqual(parse_order_result(text)[0], "uncertain", text)
        # ambiguous wins over a fill word
        self.assertEqual(parse_order_result("done? connection lost #12345678")[0], "uncertain")

    def test_word_boundaries(self):
        self.assertEqual(parse_order_result("Order placed: buy 0.5 EURUSD.h at 1.08 #52390672 (errorless)")[0],
                         "filled")

    def test_result_side_volume(self):
        self.assertEqual(result_side_volume("Done: buy 0.50 EURUSD.h at 1.08345 #52390671"), ("buy", 0.5))
        self.assertEqual(result_side_volume("order #1234 SELL 1 XAUUSD.h"), ("sell", 1.0))
        self.assertIsNone(result_side_volume("Done"))

    def test_unknown(self):
        status, msg, ticket, price = parse_order_result("Market Execution  Volume: 0.50  Sell by Market")
        self.assertEqual((status, ticket, price), ("unknown", None, None))
        self.assertEqual(parse_order_result("")[0], "unknown")

    def test_ticket_and_price_patterns(self):
        _, _, ticket, price = parse_order_result("order # 12345678 sell 0.10 XAUUSD.h at 2 345.67 done")
        self.assertEqual((ticket, price), ("12345678", 2345.67))
        _, _, ticket, _ = parse_order_result("#123 done")
        self.assertIsNone(ticket)


class LookupTests(unittest.TestCase):
    def test_find_items(self):
        a, b = item("Sell by Market"), item("Buy by Market", x=200)
        self.assertEqual(find_items([a, b], "MARKET"), [a, b])
        self.assertEqual(find_items([a, b], "buy"), [b])
        self.assertEqual(find_items([a, b], "close"), [])

    def test_nearest_label(self):
        sell = item("Sell by Market", x=20, y=200, w=84)
        buy = item("Buy by Market", x=220, y=200, w=78)
        items = [sell, buy, item("Market Execution", x=20, y=40)]
        self.assertEqual(nearest_label(items, (sell.cx + 5, sell.cy), ["buy", "sell"], 80), "sell")
        self.assertEqual(nearest_label(items, (buy.cx - 10, buy.cy + 3), ["buy", "sell"], 80), "buy")
        self.assertIsNone(nearest_label(items, (buy.cx + 200, buy.cy), ["buy", "sell"], 80))
        self.assertIsNone(nearest_label([], (0, 0), ["buy", "sell"], 80))

    def test_nearest_label_word_bounded(self):
        items = [item("Buyer seller", x=0, y=0, w=72)]
        self.assertIsNone(nearest_label(items, (36, 6), ["buy", "sell"], 80))

    def test_nearest_label_single_item_with_both_labels(self):
        # Vision read both buttons as one observation: each label gets its own half
        both = item("Sell by Market Buy by Market", x=0, y=200, w=300)
        self.assertEqual(nearest_label([both], (75, 206), ["buy", "sell"], 80), "sell")
        self.assertEqual(nearest_label([both], (225, 206), ["buy", "sell"], 80), "buy")

    def test_nearest_label_exact_tie_is_none(self):
        a = item("buy", x=0, y=0, w=20)
        b = item("sell", x=100, y=0, w=20)
        self.assertIsNone(nearest_label([a, b], (60, 6), ["buy", "sell"], 80))


class VerifyDialogTests(unittest.TestCase):
    def dialog(self, symbol="EURUSD.h, Euro vs US Dollar", volume="0.50", sl="1.08100", tp="1.08900"):
        return [
            item(symbol, x=100, y=30),
            item("Type: Market Execution", x=100, y=60),
            item("Volume: %s" % volume, x=100, y=90),
            item("Stop Loss: %s" % sl, x=100, y=120),
            item("Take Profit: %s" % tp, x=300, y=120),
            item("Sell by Market", x=100, y=300),
            item("Buy by Market", x=300, y=300),
        ]

    def test_ok(self):
        ok, problems = verify_dialog_fields(self.dialog(), "EURUSD.h", "0.50", "1.08100", "1.08900", ["Market"])
        self.assertTrue(ok, problems)
        self.assertEqual(problems, [])

    def test_numeric_equality_not_string(self):
        ok, problems = verify_dialog_fields(self.dialog(volume="0.5", sl="1.081"), "EURUSD.h", "0.50", "1.08100",
                                            "1.08900", [])
        self.assertTrue(ok, problems)

    def test_tp_none_is_not_checked_for_presence(self):
        ok, problems = verify_dialog_fields(self.dialog(tp="0.00000"), "EURUSD.h", "0.50", "1.08100", None, ["Market"])
        self.assertTrue(ok, problems)

    def test_tp_none_but_field_has_value(self):
        ok, problems = verify_dialog_fields(self.dialog(), "EURUSD.h", "0.50", "1.08100", None, [])
        self.assertFalse(ok)
        self.assertTrue(any("Take Profit" in p for p in problems), problems)

    def test_wrong_symbol(self):
        ok, problems = verify_dialog_fields(self.dialog(symbol="GBPUSD.h, Pound vs US Dollar"), "EURUSD.h",
                                            "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any("symbol" in p for p in problems), problems)

    def test_wrong_volume(self):
        ok, problems = verify_dialog_fields(self.dialog(volume="5.00"), "EURUSD.h", "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any("volume" in p.lower() for p in problems), problems)

    def test_swapped_sl_and_tp_caught_by_labels(self):
        items = self.dialog(sl="1.08900", tp="1.08100")
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any("Stop Loss shows 1.08900" in p for p in problems), problems)

    def test_missing_required_text(self):
        ok, problems = verify_dialog_fields(self.dialog(), "EURUSD.h", "0.50", "1.08100", "1.08900",
                                            ["Market", "Instant"])
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)
        self.assertIn("Instant", problems[0])

    def test_values_without_labels_fail(self):
        # a number somewhere in the window never stands in for a labelled field
        items = [item("EURUSD.h", y=10), item("Market Execution", y=40), item("0.50", y=70), item("1.08100", y=100),
                 item("1.08900", x=200, y=100)]
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", ["market"])
        self.assertFalse(ok)
        self.assertEqual(sum("label not found" in p for p in problems), 3, problems)

    def split_dialog(self, volume="0.50", sl="1.08100", tp="1.08900", label_dy=0.0):
        """Labels and field values as separate observations, labels ``label_dy`` pt off the values."""
        return [
            item("EURUSD.h, Euro vs US Dollar", x=100, y=30),
            item("Market Execution", x=100, y=60),
            item("Volume:", x=20, y=90 + label_dy, w=42), item(volume, x=100, y=90, w=40),
            item("Stop Loss:", x=20, y=130 + label_dy, w=60), item(sl, x=100, y=130, w=50),
            item("Take Profit:", x=200, y=130 + label_dy, w=66), item(tp, x=290, y=130, w=50),
            item("1.08340 / 1.08345", x=100, y=200),
            item("Sell by Market", x=100, y=300), item("Buy by Market", x=300, y=300),
        ]

    def test_separate_label_and_value_observations(self):
        for dy in (0.0, 7.0, -7.0):
            ok, problems = verify_dialog_fields(self.split_dialog(label_dy=dy), "EURUSD.h", "0.50", "1.08100",
                                                "1.08900", ["Market"])
            self.assertTrue(ok, (dy, problems))

    def test_swapped_fields_caught_even_with_offset_labels(self):
        # volume and SL typed into each other's fields; labels 7 pt above their values
        items = self.split_dialog(volume="1.08100", sl="0.50", label_dy=-7.0)
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any(p.startswith("Volume shows 1.08100") for p in problems), problems)
        self.assertTrue(any(p.startswith("Stop Loss shows 0.50") for p in problems), problems)
        items = self.split_dialog(sl="1.08900", tp="1.08100", label_dy=7.0)
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)

    def test_label_too_far_from_its_value_is_not_paired(self):
        ok, problems = verify_dialog_fields(self.split_dialog(label_dy=20.0), "EURUSD.h", "0.50", "1.08100",
                                            "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any("no value readable" in p for p in problems), problems)

    def test_dropped_label_fails(self):
        items = [i for i in self.split_dialog() if i.text != "Volume:"]
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertEqual(problems, ["Volume label not found in the order window"])

    def test_grouped_number_elsewhere_does_not_count(self):
        # volume field shows 0.01 (typing failed), "1 000.00" elsewhere must not satisfy 1.00
        items = self.split_dialog(volume="0.01") + [item("1 000.00", x=400, y=250)]
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "1.00", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any("Volume shows 0.01" in p for p in problems), problems)

    def test_merged_labels_are_not_paired(self):
        items = [item("EURUSD.h", x=100, y=30), item("Volume: 0.50", x=20, y=90),
                 item("Stop Loss: Take Profit:", x=20, y=130, w=140), item("1.08100", x=170, y=130, w=50),
                 item("1.08900", x=240, y=130, w=50)]
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", "1.08900", [])
        self.assertFalse(ok)
        self.assertTrue(any(p.startswith("Stop Loss: no value") for p in problems), problems)

    def test_tp_none_requires_a_zero_take_profit_field(self):
        items = [i for i in self.split_dialog(tp="0.00000")]
        self.assertTrue(verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", None, [])[0])
        items = [i for i in items if i.text != "Take Profit:"]
        ok, problems = verify_dialog_fields(items, "EURUSD.h", "0.50", "1.08100", None, [])
        self.assertFalse(ok)
        self.assertIn("Take Profit label not found in the order window", problems)

    def test_empty_items(self):
        ok, problems = verify_dialog_fields([], "EURUSD.h", "0.50", "1.08100", None, ["Market"])
        self.assertFalse(ok)
        self.assertEqual(len(problems), 5)    # symbol, volume, stop loss, take profit (must read 0), "Market"

if __name__ == "__main__":
    unittest.main()
