"""Pure parsers for OCR text read from the MetaTrader 5 terminal.

Inputs are :class:`~tvbridge.gui.driver.OcrItem` lists in global screen points. Apple
Vision is not tidy: it may return a whole line as one observation, the same line split
into segments, *both* at once (overlapping duplicates), or split a number at its thousands
separator. These functions are written for all of those cases:

* :func:`group_rows` clusters observations into physical rows and drops exact duplicates;
* :func:`row_text` rebuilds one clean line per row (a wider observation that covers a
  narrower one wins; overlapping segments are merged on their common tokens);
* numbers use MT5's format: "." decimals, spaces (or commas) as thousands separators.

No I/O and no pyobjc here.
"""

import functools
import math
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..models import ObservedPosition
from .driver import OcrItem

# --------------------------------------------------------------------------- text helpers

_SPACE_CHARS = "            　\t"
_MINUS_CHARS = "−‒–—﹣－"
_TRANS = {ord(c): " " for c in _SPACE_CHARS}
_TRANS.update({ord(c): "-" for c in _MINUS_CHARS})
_WS = re.compile(r"\s+")


def normalize_text(s: Optional[str]) -> str:
    """Unicode spaces -> " ", Unicode minus/dashes -> "-", whitespace collapsed, stripped."""
    if not s:
        return ""
    return _WS.sub(" ", str(s).translate(_TRANS)).strip()


# --------------------------------------------------------------------------- numbers

# One number: optional sign, integer part (plain, or grouped by "," or " "), optional decimals.
_NUM_BODY = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+|\d+)(?:\.\d+)?"
# Standalone number inside free text: not glued to letters, dates ("2026.10.01") or times.
_NUM_SCAN = re.compile(r"(?<![\w.,])(?<!\d:)" + _NUM_BODY + r"(?![\w]|[.,:]\d)")
# Same, without thousands grouping (used where a group could swallow the next column).
_SIMPLE_NUM_SCAN = re.compile(r"(?<![\w.,])(?<!\d:)[-+]?\d+(?:\.\d+)?(?![\w]|[.,:]\d)")
_NUMBER_FULL = re.compile(r"([+-]?)(\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+|\d+)(\.\d+)?")
_TRAILING_UNIT = re.compile(r"\s*(?:%|[A-Za-z]{3})$")
# Account money value: decimals required, so a value cut by OCR ("49") is never accepted.
_MONEY = r"([-+]?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)\.\d+)(?!\d)"


def parse_number(s: Optional[str]) -> Optional[float]:
    """Parse an MT5-formatted number; None if ``s`` is not exactly one number.

    Accepts "50 000.00", "50,000.00", "-17.00", "−17.00" (Unicode minus), non-breaking or
    thin spaces as separators, and a trailing "%" or currency code ("50 000.00 USD").
    Decimals are always "." in MT5, so "50 000,00" is rejected rather than guessed.
    """
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s) if math.isfinite(float(s)) else None
    t = normalize_text(str(s))
    t = _TRAILING_UNIT.sub("", t).strip()
    if t.startswith("$"):
        t = t[1:].strip()
    t = re.sub(r"^([+-]) +", r"\1", t)
    m = _NUMBER_FULL.fullmatch(t)
    if not m:
        return None
    sign, int_part, frac = m.groups()
    value = float(int_part.replace(",", "").replace(" ", "") + (frac or ""))
    return -value if sign == "-" else value


def scan_numbers(text: str, grouped: bool = True) -> List[float]:
    """Every standalone number in ``text`` (dates and clock times are skipped).

    With ``grouped`` thousands separators are honoured ("1 017.00" -> 1017.0).
    """
    pat = _NUM_SCAN if grouped else _SIMPLE_NUM_SCAN
    out = []  # type: List[float]
    for m in pat.finditer(normalize_text(text)):
        v = parse_number(m.group(0))
        if v is not None:
            out.append(v)
    return out


def _is_zero(v: Optional[float]) -> bool:
    return v is not None and abs(v) < 1e-12


# --------------------------------------------------------------------------- rows


def _same_text(a: str, b: str) -> bool:
    return normalize_text(a).lower() == normalize_text(b).lower()


def _dedupe(items: Sequence[OcrItem], center_tol: float = 3.0) -> List[OcrItem]:
    """Drop observations with the same text whose centres are within ``center_tol`` points
    (keeping the more confident one)."""
    kept = []  # type: List[OcrItem]
    for it in sorted(items, key=lambda i: -float(i.conf)):
        dup = False
        for k in kept:
            if (abs(k.cx - it.cx) <= center_tol and abs(k.cy - it.cy) <= center_tol
                    and _same_text(k.text, it.text)):
                dup = True
                break
        if not dup:
            kept.append(it)
    return kept


def group_rows(items: List[OcrItem], y_tol: float = 6.0) -> List[List[OcrItem]]:
    """Cluster observations into physical rows by vertical centre.

    Rows are returned top to bottom, each sorted left to right. Overlapping duplicates
    (same text, centres within 3 pt) are removed first.
    """
    rows = []  # type: List[List[OcrItem]]
    means = []  # type: List[float]
    for it in sorted(_dedupe([i for i in items if (i.text or "").strip()]), key=lambda i: (i.cy, i.x)):
        if rows and abs(it.cy - means[-1]) <= y_tol:
            rows[-1].append(it)
            means[-1] = sum(i.cy for i in rows[-1]) / len(rows[-1])
        else:
            rows.append([it])
            means.append(it.cy)
    return [sorted(r, key=lambda i: (i.x, -i.w)) for r in rows]


def _x_overlap(a: OcrItem, b: OcrItem) -> float:
    return min(a.x + a.w, b.x + b.w) - max(a.x, b.x)


def _covers(b: OcrItem, a: OcrItem) -> bool:
    """True if observation ``b`` makes ``a`` redundant (b spans >= 80 % of a, and is wider,
    or of similar width but more confident)."""
    if a.w <= 0:
        return b.x <= a.x <= b.x + b.w and b.w > 0
    if _x_overlap(a, b) / a.w < 0.8:
        return False
    if b.w > a.w * 1.05:
        return True
    if a.w > b.w * 1.05:
        return False
    return (float(b.conf), len(b.text or "")) > (float(a.conf), len(a.text or ""))


def _is_sublist(small: List[str], big: List[str]) -> bool:
    n = len(small)
    if n == 0:
        return True
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def _suffix_prefix_overlap(left: List[str], right: List[str]) -> int:
    for k in range(min(len(left), len(right)), 0, -1):
        if left[-k:] == right[:k]:
            return k
    return 0


def row_text(row: List[OcrItem]) -> str:
    """One clean line of text for a row of observations (as returned by :func:`group_rows`).

    Observations covered by a wider one (a full-line observation plus its segments) are
    dropped; remaining overlapping neighbours are merged on their shared tokens, so nothing
    is counted twice. Non-overlapping observations are joined with a space (which also
    re-joins a number that Vision split at its thousands separator).
    """
    items = [i for i in row if normalize_text(i.text)]
    kept = [a for a in items if not any(b is not a and _covers(b, a) for b in items)]
    kept.sort(key=lambda i: (i.x, -i.w))
    tokens = []  # type: List[str]
    right_edge = None  # type: Optional[float]
    for it in kept:
        toks = normalize_text(it.text).split(" ")
        if right_edge is not None and it.x < right_edge - 0.5:   # overlaps what we have
            if _is_sublist(toks, tokens):
                right_edge = max(right_edge, it.x + it.w)
                continue
            k = _suffix_prefix_overlap(tokens, toks)
            tokens.extend(toks[k:])
        else:
            tokens.extend(toks)
        right_edge = it.x + it.w if right_edge is None else max(right_edge, it.x + it.w)
    return " ".join(tokens)


# --------------------------------------------------------------------------- symbols


@functools.lru_cache(maxsize=256)
def _symbol_pattern(sym: str) -> "re.Pattern":
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9]|\.[A-Za-z0-9])", re.I)


def _canon_symbol(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_symbol(text: str, known_symbols: Iterable[str]) -> Optional[Tuple[str, int, int]]:
    """Earliest known symbol in ``text`` as ``(known_symbol, start, end)``, or None.

    Matching is case-insensitive and word-bounded ("EURUSD.h" never matches "EURUSD.hx" or
    a bare "EURUSD"). If nothing matches exactly, a token equal to a known symbol after
    removing punctuation ("EURUSDh", "EURUSD,h" for "EURUSD.h") is accepted when that
    form is unambiguous. The returned symbol is spelled as in ``known_symbols``.
    """
    syms = [s for s in known_symbols if s and s.strip()]
    best = None  # type: Optional[Tuple[str, int, int]]
    for sym in syms:
        m = _symbol_pattern(sym.strip()).search(text)
        if m is None:
            continue
        if best is None or (m.start(), -(m.end() - m.start())) < (best[1], -(best[2] - best[1])):
            best = (sym, m.start(), m.end())
    if best is not None:
        return best
    canon = {}  # type: Dict[str, List[str]]
    for sym in syms:
        c = _canon_symbol(sym)
        if c:
            canon.setdefault(c, []).append(sym)
    for tm in re.finditer(r"\S+", text):
        matches = canon.get(_canon_symbol(tm.group(0).strip(",;:|()[]{}'\"")))
        if matches and len(set(matches)) == 1:
            return matches[0], tm.start(), tm.end()
    return None


# --------------------------------------------------------------------------- account line

_BALANCE_RE = re.compile(r"\bbalance\s*[:;.]?\s*" + _MONEY, re.I)
_EQUITY_RE = re.compile(r"\bequity\s*[:;.]?\s*" + _MONEY, re.I)
_MARGIN_RE = re.compile(r"\b(free\s*)?margin(\s*level)?\s*[:;.]?\s*" + _MONEY, re.I)
_ACCOUNT_LINE_RE = re.compile(r"\b(?:balance|equity|free\s*margin|margin\s*level)\s*[:;]", re.I)


def _scan_account(text: str) -> Dict[str, float]:
    found = {}  # type: Dict[str, float]
    for key, pat in (("balance", _BALANCE_RE), ("equity", _EQUITY_RE)):
        m = pat.search(text)
        if m:
            v = parse_number(m.group(1))
            if v is not None:
                found[key] = v
    for m in _MARGIN_RE.finditer(text):
        if m.group(2):          # "Margin Level: ... %" is not money
            continue
        key = "free_margin" if m.group(1) else "margin"
        if key not in found:
            v = parse_number(m.group(3))
            if v is not None:
                found[key] = v
    return found


def parse_account_line(items: List[OcrItem]) -> Optional[Dict[str, float]]:
    """Read the Toolbox account line ("Balance: 50 000.00 USD  Equity: ...  Margin: ...
    Free Margin: ...  Margin Level: ... %").

    Looks at each rebuilt row, each raw observation and the whole text in reading order, so
    it works whether Vision returns the line whole, in segments, or label and value apart.
    Returns ``{"balance", "equity"[, "margin"][, "free_margin"]}`` or None unless both
    balance and equity were found. Values must carry decimals (MT5 always shows them), so a
    truncated read never produces a wrong number.
    """
    rows = group_rows(items)
    row_texts = [row_text(r) for r in rows]
    sources = row_texts + [normalize_text(i.text) for i in items] + [" ".join(row_texts)]
    out = {}  # type: Dict[str, float]
    for text in sources:
        if not text:
            continue
        for key, value in _scan_account(text).items():
            out.setdefault(key, value)
        if len(out) == 4:
            break
    if "balance" not in out or "equity" not in out:
        return None
    return out


# --------------------------------------------------------------------------- positions

_SIDE_RE = re.compile(r"(?<![A-Za-z])(buy|sell)(?![A-Za-z])", re.I)
_PENDING_RE = re.compile(r"\s*(?:limit|stop)\b", re.I)
_TICKET_RE = re.compile(r"(?<![\w.:])#?(\d{4,})(?![\w.:])")
# A symbol-like token for positions on symbols that are not in symbols.specs (D3): starts
# with a letter or "#", then letters/digits and the usual suffix punctuation ("BTCUSD.h",
# "US30.cash", "#AAPL", "GER40").
_GENERIC_SYMBOL_RE = re.compile(r"^#?[A-Za-z][A-Za-z0-9._#&+-]{1,31}$")
_TOKEN_STRIP = ",;:|()[]{}'\""
#: header and other Toolbox words that are never a symbol
_NOT_SYMBOLS = frozenset(w.lower() for w in (
    "symbol", "ticket", "time", "type", "volume", "price", "profit", "swap", "comment", "balance",
    "equity", "margin", "free", "level", "trade", "exposure", "history", "journal", "buy", "sell",
))
_WORD_CACHE = {}  # type: Dict[str, "re.Pattern"]


def _word_re(word: str) -> "re.Pattern":
    pat = _WORD_CACHE.get(word)
    if pat is None:
        pat = re.compile(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", re.I)
        _WORD_CACHE[word] = pat
    return pat


def _decimals(num_text: str) -> int:
    t = normalize_text(num_text)
    return len(t.split(".", 1)[1]) if "." in t else 0


def _anchor_for(row: List[OcrItem], sym: str) -> OcrItem:
    """The narrowest observation of the row that contains the symbol (leftmost item if the
    symbol only appears after merging)."""
    holders = [i for i in row if find_symbol(normalize_text(i.text), [sym]) is not None]
    if holders:
        return min(holders, key=lambda i: (i.w, i.x))
    return min(row, key=lambda i: i.x)


def find_trade_header(items: List[OcrItem]) -> Optional[Dict[str, object]]:
    """The Toolbox Trade-list header row ("Symbol ... Profit"), or None.

    Returns ``{"cy": centre y, "text": row text, "swap": True if a Swap column is shown
    before Profit, "h": text height}``. The topmost matching row wins.
    """
    for row in group_rows(items):
        text = row_text(row)
        sym_m = re.search(r"\bsymbol", text, re.I)   # MT5 draws an edit icon right after "Symbol"
        prof_m = _word_re("profit").search(text)
        if sym_m is None or prof_m is None or prof_m.start() < sym_m.start():
            continue
        swap_m = _word_re("swap").search(text)
        cy = sum(i.cy for i in row) / float(len(row))
        h = max(float(i.h) for i in row)
        return {"cy": cy, "text": text, "swap": bool(swap_m and swap_m.start() < prof_m.start()), "h": h}
    return None


def account_line_y(items: List[OcrItem]) -> Optional[float]:
    """Centre y of the Toolbox row holding "Balance:"/"Equity:", or None."""
    for row in group_rows(items):
        text = row_text(row)
        if _BALANCE_RE.search(text) or _EQUITY_RE.search(text):
            return sum(i.cy for i in row) / float(len(row))
    return None


def _generic_symbol(text: str) -> Optional[Tuple[str, int, int]]:
    """``(token, start, end)`` of the first word of the row if it looks like a symbol."""
    for tm in re.finditer(r"\S+", text):
        raw = tm.group(0)
        tok = raw.strip(_TOKEN_STRIP)
        if not tok or not any(c.isalpha() for c in tok):
            if any(c.isdigit() for c in tok):
                return None      # the row starts with a number (date, time): not a symbol column
            continue             # an icon or punctuation glyph before the symbol
        if (not _GENERIC_SYMBOL_RE.match(tok) or tok.lower() in _NOT_SYMBOLS
                or sum(1 for c in tok if c.isalpha()) < 2):
            return None
        start = tm.start() + raw.index(tok)
        return tok, start, start + len(tok)
    return None


def parse_position_rows(items: List[OcrItem], known_symbols: List[str]) -> List[Tuple[ObservedPosition, OcrItem]]:
    """Open positions in the Toolbox Trade tab, one per physical row.

    A row is a position if its text has a known symbol (word-bounded, case-insensitive), a
    standalone "buy"/"sell" after it (not "buy limit"/"sell stop": pending orders are
    skipped) and a lot size: the first number after the side. Rows on symbols that are not
    in ``known_symbols`` are recognised too (D3): the row's first word must look like a
    symbol and be followed by a ticket number (4+ digits) before the side; such positions
    keep the symbol as read. A known symbol always wins (it normalises the spelling). The
    account line is ignored.

    MT5 columns after the side: Volume, Price, S/L, T/P, Price, [Swap], Profit. Besides
    lots, ``open_price`` (2nd number) and ``profit`` (last number, if the row has at least 4)
    are filled; ``swap`` (the number before Profit) when the header shows a Swap column;
    ``sl``/``tp`` only when all four price columns are present with the same number of
    decimals (an empty S/L or T/P cell makes the columns ambiguous), and a shown value of 0
    means "not set" (None; ``sl_missing`` is then True). ``ticket`` is the first 4+ digit
    number between the symbol and the side. Rows with a ticket already seen are not counted
    twice.

    Returns ``(position, anchor)`` pairs; anchor is the narrowest observation holding the
    symbol (double-click target for the row).
    """
    header = find_trade_header(items)
    swap_column = bool(header and header.get("swap"))
    out = []  # type: List[Tuple[ObservedPosition, OcrItem]]
    seen_tickets = set()
    for row in group_rows(items):
        text = row_text(row)
        if not text or _ACCOUNT_LINE_RE.search(text):
            continue
        found = find_symbol(text, known_symbols)
        generic = False
        if found is None:
            found = _generic_symbol(text)
            generic = True
            if found is None:
                continue
        sym, _s0, s_end = found
        side_m = _SIDE_RE.search(text, s_end)
        if side_m is None:
            continue
        rest = text[side_m.end():]
        if _PENDING_RE.match(rest):
            continue
        lots_m = _SIMPLE_NUM_SCAN.search(rest)
        if lots_m is None:
            continue
        lots = parse_number(lots_m.group(0))
        if lots is None or lots <= 0:
            continue
        ticket_m = _TICKET_RE.search(text[s_end:side_m.start()])
        ticket = ticket_m.group(1) if ticket_m else None
        if generic and ticket is None:
            continue   # an unknown symbol needs the ticket column to be believed
        num_texts = [lots_m.group(0)] + [m.group(0) for m in _NUM_SCAN.finditer(rest, lots_m.end())]
        nums = [parse_number(t) for t in num_texts]
        if any(v is None for v in nums):   # cannot happen with the scan regexes; be safe
            nums = [v for v in nums if v is not None]
        n = len(nums)

        open_price = nums[1] if n >= 2 else None
        sl = tp = None
        sl_missing = False
        if n >= 6:
            price_dec = _decimals(num_texts[1])
            if all(_decimals(t) == price_dec for t in num_texts[1:5]):
                sl = None if _is_zero(nums[2]) else nums[2]
                tp = None if _is_zero(nums[3]) else nums[3]
                sl_missing = _is_zero(nums[2])
        profit = nums[-1] if n >= 4 else None
        swap = nums[-2] if swap_column and n >= 5 else None

        if ticket is not None:
            if ticket in seen_tickets:
                continue
            seen_tickets.add(ticket)

        pos = ObservedPosition(symbol=sym, side=side_m.group(1).lower(), lots=lots, ticket=ticket,
                               open_price=open_price, sl=sl, tp=tp, profit=profit, swap=swap,
                               sl_missing=sl_missing)
        out.append((pos, _anchor_for(row, sym)))
    return out


# --------------------------------------------------------------------------- order result

# The terminal did not get the server's answer: the order may or may not have executed.
# These are checked first and give "uncertain" (MetaQuotes: check before retrying).
_AMBIGUOUS_RES = (
    re.compile(r"\btime\s?out\b|\btimed\s+out\b", re.I),
    re.compile(r"\bno\s+connection\b", re.I),
    re.compile(r"\bconnection\b", re.I),
    re.compile(r"\berror\b", re.I),
    re.compile(r"\bfailed\b", re.I),
)
# Definitive rejections: the server refused the request, nothing was executed.
_REJECT_RES = (
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\brejected\b", re.I),
    re.compile(r"\bnot\s+enough\b", re.I),
    re.compile(r"\bno\s+money\b", re.I),
    re.compile(r"\bmarket\s+(?:is\s+)?closed\b", re.I),
    re.compile(r"\bdisabled\b", re.I),
    re.compile(r"\brequote\b", re.I),
    re.compile(r"\boff\s+quotes\b", re.I),
    re.compile(r"\bno\s+prices\b", re.I),
    re.compile(r"\btoo\s+(?:many|frequent)\b", re.I),
)
_FILL_RE = re.compile(r"\b(done|executed|placed|filled)\b", re.I)
_RESULT_TICKET_RE = re.compile(r"#\s?(\d{4,})")
_RESULT_PRICE_RE = re.compile(r"\bat\s+(\d{1,3}(?: \d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", re.I)
_RESULT_SIDE_VOLUME_RE = re.compile(r"(?<![A-Za-z])(buy|sell)\s+(\d+(?:\.\d+)?)(?![\w]|[.,]\d)", re.I)


def _snippet(text: str, start: int, end: int, before: int = 60, after: int = 120) -> str:
    a = max(0, start - before)
    b = min(len(text), end + after)
    return ("..." if a > 0 else "") + text[a:b] + ("..." if b < len(text) else "")


def parse_order_result(text: str) -> Tuple[str, str, Optional[str], Optional[float]]:
    """Classify the order window text after a click.

    Returns ``(status, message, ticket, price)``. Status, checked in this order:

    * "uncertain" if an ambiguous phrase appears (timeout, no connection, connection,
      error, failed): the terminal may not have received the server's answer, so the
      order may have executed;
    * "rejected" if a definitive rejection appears (invalid, rejected, not enough (money),
      no money, market (is) closed, (trade) disabled, requote, off quotes, no prices, too
      many/too frequent requests);
    * "filled" if "done"/"executed"/"placed"/"filled" appears;
    * else "unknown".

    ``ticket`` from "#12345678", ``price`` from "at 1.08345".
    """
    t = normalize_text(text)
    tm = _RESULT_TICKET_RE.search(t)
    ticket = tm.group(1) if tm else None
    pm = _RESULT_PRICE_RE.search(t)
    price = parse_number(pm.group(1)) if pm else None

    for pat in _AMBIGUOUS_RES:
        m = pat.search(t)
        if m:
            return "uncertain", _snippet(t, m.start(), m.end()), ticket, price
    for pat in _REJECT_RES:
        m = pat.search(t)
        if m:
            return "rejected", _snippet(t, m.start(), m.end()), ticket, price
    fm = _FILL_RE.search(t)
    if fm:
        return "filled", _snippet(t, fm.start(), fm.end()), ticket, price
    return "unknown", t[:200], ticket, price


def result_side_volume(text: str) -> Optional[Tuple[str, float]]:
    """``("buy"|"sell", volume)`` from a result text such as "buy 0.50 EURUSD.h at ...", or None."""
    m = _RESULT_SIDE_VOLUME_RE.search(normalize_text(text))
    if m is None:
        return None
    v = parse_number(m.group(2))
    return (m.group(1).lower(), v) if v is not None else None


# --------------------------------------------------------------------------- order ticket quote

_QUOTE_LINE_RE = re.compile(r"^\s*(\d[\d ]*\.\d+)\s*/\s*(\d[\d ]*\.\d+)\s*$")
#: bid/ask further apart than this fraction of the price are not one instrument's quote
QUOTE_MAX_SPREAD_REL = 0.01


def _quote_from_text(text: str) -> Optional[Tuple[float, float]]:
    m = _QUOTE_LINE_RE.match(normalize_text(text))
    if m is None:
        return None
    bid, ask = parse_number(m.group(1)), parse_number(m.group(2))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    if (ask - bid) > QUOTE_MAX_SPREAD_REL * bid:
        return None
    return bid, ask


def parse_ticket_quote(items: List[OcrItem]) -> Optional[Tuple[float, float]]:
    """``(bid, ask)`` from the New Order ticket's big quote line ("4 176.77 / 4 176.97").

    An observation (or a whole row rebuilt from its segments) must consist of exactly two
    decimal numbers separated by "/" (spaces as thousands separators), with ask >= bid and a
    spread of at most 1 % of the price. If several different quotes are read, the tallest
    text wins; a tie between different values is unreadable (None).
    """
    found = []  # type: List[Tuple[float, Tuple[float, float]]]
    for it in items:
        q = _quote_from_text(it.text)
        if q is not None:
            found.append((float(it.h), q))
    if not found:
        for row in group_rows(list(items)):
            q = _quote_from_text(row_text(row))
            if q is not None:
                found.append((max(float(i.h) for i in row), q))
    if not found:
        return None
    best_h = max(h for h, _ in found)
    best = {q for h, q in found if h >= best_h - 1e-9}
    if len(best) != 1:
        return None
    return next(iter(best))


# --------------------------------------------------------------------------- lookup helpers


def find_items(items: List[OcrItem], needle: str) -> List[OcrItem]:
    """Observations whose text contains ``needle`` (case-insensitive)."""
    n = normalize_text(needle).lower()
    if not n:
        return []
    return [i for i in items if n in normalize_text(i.text).lower()]


@functools.lru_cache(maxsize=64)
def _word_pattern(word: str) -> "re.Pattern":
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.I)


def nearest_label(items: List[OcrItem], point: Tuple[float, float], labels: List[str],
                  max_dist: float) -> Optional[str]:
    """The label (as spelled in ``labels``) whose matching observation is closest to ``point``.

    Matching is word-bounded and case-insensitive. If one observation contains several
    different labels (e.g. "Sell by Market ... Buy by Market" read as one line), each label
    is placed at the centre of its own part of that text instead of the shared centre.
    Returns None if nothing is within ``max_dist`` points or the two nearest different
    labels are exactly equidistant.
    """
    px, py = float(point[0]), float(point[1])
    pats = [(lab, _word_pattern(lab)) for lab in labels if lab]
    cands = []  # type: List[Tuple[float, str]]
    for it in items:
        text = normalize_text(it.text)
        hits = sorted((m.start(), lab) for lab, pat in pats for m in pat.finditer(text))
        if not hits:
            continue
        if len({lab for _, lab in hits}) == 1:
            cands.append((math.hypot(it.cx - px, it.cy - py), hits[0][1]))
            continue
        n = float(max(1, len(text)))
        for idx, (start, lab) in enumerate(hits):
            end = hits[idx + 1][0] if idx + 1 < len(hits) else len(text)
            x = it.x + it.w * ((start + end) / 2.0) / n
            cands.append((math.hypot(x - px, it.cy - py), lab))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    best_d, best_lab = cands[0]
    if best_d > max_dist:
        return None
    for d, lab in cands[1:]:
        if d - best_d > 1e-6:
            break
        if lab != best_lab:
            return None
    return best_lab


# --------------------------------------------------------------------------- dialog verification

_FIELD_LABELS = (
    # (key, label shown in messages, label word pattern, "label value" in one observation)
    # lot sizes are never thousands-grouped, so a following column cannot be swallowed
    ("volume", "Volume", re.compile(r"\bvolume\b", re.I),
     re.compile(r"\bvolume\s*[:;.]?\s*([-+]?\d+(?:\.\d+)?)(?![\w]|[.,]\d)", re.I)),
    ("sl", "Stop Loss", re.compile(r"\bstop\s*loss\b", re.I),
     re.compile(r"\bstop\s*loss\s*[:;.]?\s*(" + _NUM_BODY + r")(?![\w]|[.,]\d)", re.I)),
    ("tp", "Take Profit", re.compile(r"\btake\s*profit\b", re.I),
     re.compile(r"\btake\s*profit\s*[:;.]?\s*(" + _NUM_BODY + r")(?![\w]|[.,]\d)", re.I)),
)
_LEADING_NUM_RE = re.compile(r"^(" + _NUM_BODY + r")(?![\w]|[.,]\d)")
#: a value belongs to a label if its centre is within this many item heights of the label's
PAIR_DY_FACTOR = 0.75


def _fmt(v: float) -> str:
    return ("%.10f" % v).rstrip("0").rstrip(".")


def _leading_number(text: str) -> Optional[str]:
    m = _LEADING_NUM_RE.match(normalize_text(text))
    return m.group(1) if m else None


def paired_field_values(items: List[OcrItem], label_re: "re.Pattern", inline_re: "re.Pattern"
                        ) -> Tuple[bool, List[str]]:
    """``(label_found, value texts)`` for one order-window field label.

    A value is either inside the label's own observation ("Stop Loss: 1.08100") or the
    nearest observation that starts with a number, begins right of the label and whose
    vertical centre is within ``PAIR_DY_FACTOR`` x the taller item's height of the label's.
    A label observation followed by other words (e.g. two labels read as one) is not paired.
    """
    found = False
    values = []  # type: List[str]
    for lab in items:
        text = normalize_text(lab.text)
        lm = label_re.search(text)
        if lm is None:
            continue
        found = True
        m = inline_re.search(text)
        if m:
            values.append(m.group(1))
            continue
        if text[lm.end():].strip(" :;.-"):
            continue
        right = float(lab.x) + float(lab.w)
        best = None  # type: Optional[Tuple[float, str]]
        for c in items:
            if c is lab:
                continue
            num = _leading_number(c.text)
            if num is None:
                continue
            if float(c.x) < right - 2.0:
                continue
            if abs(c.cy - lab.cy) > PAIR_DY_FACTOR * max(float(c.h), float(lab.h)):
                continue
            gap = float(c.x) - right
            if best is None or gap < best[0]:
                best = (gap, num)
        if best is not None:
            values.append(best[1])
    return found, values


def verify_dialog_fields(items: List[OcrItem], symbol: str, lots_str: str, sl_str: str,
                         tp_str: Optional[str], require_text: List[str]) -> Tuple[bool, List[str]]:
    """Check that the order window shows what was typed, field by field.

    * ``symbol`` present (case-insensitive, word-bounded);
    * each of Volume, Stop Loss and Take Profit must have its label read by OCR and a value
      paired with that label (same observation, or the nearest number right of the label on
      the same line, see :func:`paired_field_values`); the value must equal ``lots_str`` /
      ``sl_str`` / ``tp_str`` within 1e-9 ("0.50" matches "0.5"); with ``tp_str`` None the
      Take Profit value must be 0. A number elsewhere in the window never counts, so values
      typed into each other's fields (or a missed label) fail verification;
    * every ``require_text`` string present (case-insensitive).

    Returns ``(ok, problems)``.
    """
    rows = group_rows(items)
    texts = [row_text(r) for r in rows] + [normalize_text(i.text) for i in items]
    texts = [t for t in texts if t]
    joined = " ".join(texts)
    problems = []  # type: List[str]

    if not symbol or find_symbol(joined, [symbol]) is None:
        problems.append("symbol %r not found in the order window" % (symbol,))

    wanted = {"volume": lots_str, "sl": sl_str, "tp": tp_str}
    clean = [i for i in items if normalize_text(i.text)]
    for key, label, label_re, inline_re in _FIELD_LABELS:
        value = wanted[key]
        if value is None:
            target = 0.0            # TP not wanted: the field must read 0
        else:
            target = parse_number(value)
            if target is None:
                problems.append("expected %s %r is not a number" % (label, value))
                continue
        found, shown = paired_field_values(clean, label_re, inline_re)
        if not found:
            problems.append("%s label not found in the order window" % label)
            continue
        nums = []  # type: List[Tuple[float, str]]
        for t in shown:
            v = parse_number(t)
            if v is not None and not any(abs(v - x) <= 1e-9 for x, _ in nums):
                nums.append((v, t))
        if not nums:
            problems.append("%s: no value readable next to its label" % label)
            continue
        if len(nums) > 1:
            problems.append("%s reads several values (%s)" % (label, ", ".join(t for _, t in nums)))
            continue
        got, got_text = nums[0]
        if abs(got - target) > 1e-9:
            if value is None:
                problems.append("%s shows %s, expected none (0)" % (label, got_text))
            else:
                problems.append("%s shows %s, expected %s" % (label, got_text, value))

    low = joined.lower()
    for req in require_text or []:
        if req and normalize_text(req).lower() not in low:
            problems.append("required text %r not found in the order window" % (req,))

    return (not problems), problems
