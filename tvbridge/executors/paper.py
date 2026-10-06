"""Paper executor: a simulated account persisted in the store (kv ``paper_state``).

The simulation is deliberately simple and deterministic:

* Market orders fill at ``OrderRequest.price_hint`` (the alert's price). No hint -> error.
  With ``sl_distance`` (mirror mode) the hint is also the quote the stop is measured from.
* A partial close realizes P/L and commission pro rata and leaves the rest open.
* Prices only move when :meth:`PaperExecutor.set_price_hint` is called (the engine calls it
  with each alert's price). A new price triggers stop-loss / take-profit hits, which are
  realized at the SL/TP price itself.
* P/L in USD = (exit - entry) x contract_size x lots x quote_to_usd (negated for sells),
  minus ``commission_per_lot_usd x lots`` charged when the position is closed.
* Equity = balance + floating P/L of open positions at their last known prices.

Because several processes (engine, CLI) may create a PaperExecutor over the same database,
the state is re-read from the store at the start of every operation and written back after
every change.
"""

import json
import logging
import math
import threading
from typing import Any, Dict, List, Optional, Tuple

from .. import clock
from ..config import Config, SymbolSpec
from ..models import SIDES, AccountSnapshot, ObservedPosition, OrderRequest, OrderResult
from ..store import Store
from .base import Executor, ExecutorError

log = logging.getLogger("tvbridge.executors.paper")

STATE_KEY = "paper_state"
STATE_VERSION = 1
HISTORY_LIMIT = 200


def _finite_positive(x: Any) -> bool:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


class PaperExecutor(Executor):
    """Simulated executor for ``executor.mode = "paper"``."""

    name = "paper"

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ state

    def _start_balance(self) -> float:
        start = self.cfg.executor.paper_start_balance
        return float(start) if start else float(self.cfg.account.initial_balance)

    def _fresh_state(self) -> Dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "balance": self._start_balance(),
            "next_ticket": 1,
            "positions": [],
            "prices": {},
            "history": [],
            "created_at": clock.iso(clock.utcnow()),
        }

    def _load(self) -> Dict[str, Any]:
        raw = self.store.get_kv(STATE_KEY)
        if raw is None:
            return self._fresh_state()
        try:
            state = json.loads(raw)
        except ValueError as e:
            raise ExecutorError("PAPER_STATE_INVALID", "kv %r is not valid JSON: %s" % (STATE_KEY, e))
        if not isinstance(state, dict) or not isinstance(state.get("positions"), list):
            raise ExecutorError("PAPER_STATE_INVALID", "kv %r has an unexpected shape" % STATE_KEY)
        state.setdefault("version", STATE_VERSION)
        state.setdefault("balance", self._start_balance())
        state.setdefault("next_ticket", 1)
        state.setdefault("prices", {})
        state.setdefault("history", [])
        return state

    def _save(self, state: Dict[str, Any]) -> None:
        state["history"] = state.get("history", [])[-HISTORY_LIMIT:]
        self.store.set_kv(STATE_KEY, json.dumps(state, sort_keys=True))

    # ------------------------------------------------------------------ symbols & money

    def _key(self, symbol: str) -> str:
        """Canonical (MT5) name for a TV or MT5 symbol; unknown symbols are kept as given."""
        tv = self.cfg.tv_symbol_for(symbol)
        if tv is None:
            return (symbol or "").strip()
        return self.cfg.mt5_symbol(tv)

    def _spec(self, symbol: str) -> Optional[SymbolSpec]:
        return self.cfg.spec_for(symbol)

    def _quote_to_usd(self, key: str, spec: SymbolSpec, price: float,
                      prices: Dict[str, float]) -> Optional[float]:
        """USD value of one unit of the symbol's quote currency, or None if unknown.

        USD quote -> 1; USD base (e.g. USDJPY) -> 1/price; otherwise derived from another
        known price ("<QUOTE>USD" -> price, "USD<QUOTE>" -> 1/price).
        """
        quote = (spec.quote or "").upper()
        if quote == "USD":
            return 1.0
        tv = self.cfg.tv_symbol_for(key) or self.cfg.normalize_tv_symbol(key)
        if tv.startswith("USD") and tv[3:6] == quote and _finite_positive(price):
            return 1.0 / float(price)
        for sym, px in prices.items():
            if not _finite_positive(px):
                continue
            norm = self.cfg.normalize_tv_symbol(sym)
            if norm == quote + "USD":
                return float(px)
            if norm == "USD" + quote:
                return 1.0 / float(px)
        return None

    def _pnl(self, pos: Dict[str, Any], exit_price: float, prices: Dict[str, float]) -> Tuple[float, bool]:
        """Gross P/L in USD of ``pos`` at ``exit_price`` and whether it was converted to USD."""
        spec = self._spec(pos["symbol"])
        contract = float(spec.contract_size) if spec else float(pos.get("contract_size") or 100000.0)
        q2usd = self._quote_to_usd(pos["symbol"], spec, exit_price, prices) if spec else None
        if q2usd is None:
            q2usd = pos.get("q2usd")
        converted = q2usd is not None
        if q2usd is None:
            q2usd = 1.0
        sign = 1.0 if pos["side"] == "buy" else -1.0
        gross = sign * (float(exit_price) - float(pos["entry"])) * contract * float(pos["lots"]) * float(q2usd)
        return gross, converted

    def _commission(self, lots: float, symbol: Optional[str] = None) -> float:
        return self.cfg.commission_per_lot(symbol) * float(lots)

    # ------------------------------------------------------------------ core operations

    def _close_position(self, state: Dict[str, Any], pos: Dict[str, Any], exit_price: float,
                        reason: str) -> OrderResult:
        """Realize ``pos`` at ``exit_price`` (removes it from state; caller saves)."""
        gross, converted = self._pnl(pos, exit_price, state["prices"])
        commission = self._commission(pos["lots"], pos["symbol"])
        net = gross - commission
        state["balance"] = float(state["balance"]) + net
        state["positions"] = [p for p in state["positions"] if p["ticket"] != pos["ticket"]]
        now = clock.iso(clock.utcnow())
        state["history"].append({
            "ticket": pos["ticket"], "symbol": pos["symbol"], "side": pos["side"], "lots": pos["lots"],
            "entry": pos["entry"], "exit": exit_price, "opened_at": pos.get("opened_at"), "closed_at": now,
            "reason": reason, "gross": round(gross, 2), "commission": round(commission, 2),
            "net": round(net, 2),
        })
        msg = "paper %s: closed %s %s %s at %s, P/L %.2f USD (commission %.2f)" % (
            reason, pos["side"], _fmt_lots(pos["lots"]), pos["symbol"], exit_price, net, commission)
        if not converted:
            msg += " [P/L not converted to USD: no %s rate known]" % (self._quote_name(pos["symbol"]),)
        log.info("%s #%s", msg, pos["ticket"])
        return OrderResult("filled", msg, fill_price=float(exit_price), ticket=pos["ticket"],
                           lots=float(pos["lots"]))

    def _quote_name(self, symbol: str) -> str:
        spec = self._spec(symbol)
        return spec.quote if spec else "quote currency"

    def _apply_price(self, state: Dict[str, Any], key: str, price: float) -> List[OrderResult]:
        """Record a price and realize any stop-loss / take-profit it hits."""
        state["prices"][key] = float(price)
        hits = []  # type: List[OrderResult]
        for pos in list(state["positions"]):
            if pos["symbol"] != key:
                continue
            sl, tp = pos.get("sl"), pos.get("tp")
            if pos["side"] == "buy":
                if sl and price <= sl:
                    hits.append(self._close_position(state, pos, float(sl), "sl"))
                elif tp and price >= tp:
                    hits.append(self._close_position(state, pos, float(tp), "tp"))
            else:
                if sl and price >= sl:
                    hits.append(self._close_position(state, pos, float(sl), "sl"))
                elif tp and price <= tp:
                    hits.append(self._close_position(state, pos, float(tp), "tp"))
        return hits

    def _floating(self, state: Dict[str, Any], pos: Dict[str, Any]) -> float:
        price = state["prices"].get(pos["symbol"])
        if not _finite_positive(price):
            return 0.0
        gross, _ = self._pnl(pos, float(price), state["prices"])
        return gross

    # ------------------------------------------------------------------ Executor API

    def set_price_hint(self, symbol: str, price: float) -> None:
        if not _finite_positive(price):
            log.warning("ignoring invalid price hint %r for %s", price, symbol)
            return
        with self._lock:
            state = self._load()
            self._apply_price(state, self._key(symbol), float(price))
            self._save(state)

    def read_account(self) -> AccountSnapshot:
        with self._lock:
            state = self._load()
            positions = []  # type: List[ObservedPosition]
            floating_total = 0.0
            for pos in state["positions"]:
                fl = self._floating(state, pos)
                floating_total += fl
                positions.append(ObservedPosition(
                    symbol=pos["symbol"], side=pos["side"], lots=float(pos["lots"]), ticket=pos["ticket"],
                    open_price=float(pos["entry"]), sl=pos.get("sl"), tp=pos.get("tp"), profit=round(fl, 2),
                ))
            balance = round(float(state["balance"]), 2)
            return AccountSnapshot(
                ts=clock.utcnow(), balance=balance, equity=round(balance + floating_total, 2),
                margin=None, free_margin=None, positions=positions, source="paper",
            )

    def open_market(self, req: OrderRequest) -> OrderResult:
        if req.side not in SIDES:
            return OrderResult("error", "BAD_REQUEST: side must be buy or sell, got %r" % (req.side,))
        if not _finite_positive(req.lots):
            return OrderResult("error", "BAD_REQUEST: lots must be > 0, got %r" % (req.lots,))
        if not _finite_positive(req.price_hint):
            return OrderResult("error", "NO_PRICE: paper fills need OrderRequest.price_hint")
        spec = self._spec(req.symbol)
        if spec is None:
            return OrderResult("error", "NO_SPEC: no symbol spec for %s" % req.symbol)
        price = float(req.price_hint)  # type: ignore[arg-type]
        lots = round(float(req.lots), max(0, int(req.lot_decimals)))
        sl = float(req.sl) if req.sl else None
        tp = float(req.tp) if req.tp else None
        if req.sl_distance is not None:
            # mirror mode: stop/target at a fixed distance from the quote (here: the fill price)
            if not _finite_positive(req.sl_distance):
                return OrderResult("error", "BAD_REQUEST: sl_distance must be > 0, got %r" % (req.sl_distance,))
            sign = 1.0 if req.side == "buy" else -1.0
            digits = max(0, int(req.digits))
            sl = round(price - sign * float(req.sl_distance), digits)
            tp = round(price + sign * float(req.tp_distance), digits) if _finite_positive(req.tp_distance) else None
            if sl <= 0:
                return OrderResult("error", "BAD_REQUEST: sl_distance %r is not below the price %s"
                                   % (req.sl_distance, price))
        with self._lock:
            state = self._load()
            key = self._key(req.symbol)
            # The fill price is also the latest market price for the symbol.
            self._apply_price(state, key, price)
            q2usd = self._quote_to_usd(key, spec, price, state["prices"])
            if q2usd is None and _finite_positive(req.quote_usd):
                q2usd = float(req.quote_usd)  # type: ignore[arg-type]  # the alert's own rate
            ticket = "P%d" % int(state["next_ticket"])
            state["next_ticket"] = int(state["next_ticket"]) + 1
            state["positions"].append({
                "ticket": ticket, "symbol": key, "side": req.side, "lots": lots, "entry": price,
                "sl": sl, "tp": tp,
                "opened_at": clock.iso(clock.utcnow()), "q2usd": q2usd,
                "contract_size": float(spec.contract_size), "comment": req.comment,
            })
            self._save(state)
        msg = "paper fill: %s %s %s at %s #%s" % (req.side, _fmt_lots(lots, req.lot_decimals), key, price, ticket)
        if q2usd is None:
            msg += " [warning: no USD rate for %s; P/L will not be converted]" % spec.quote
            log.warning("paper position %s on %s has no %s->USD rate", ticket, key, spec.quote)
        log.info(msg)
        return OrderResult("filled", msg, fill_price=price, ticket=ticket, lots=lots, sl=sl, tp=tp)

    def close_positions(self, symbol: str, side: Optional[str] = None) -> List[OrderResult]:
        with self._lock:
            state = self._load()
            key = self._key(symbol)
            matches = [p for p in state["positions"]
                       if p["symbol"].lower() == key.lower() and (side is None or p["side"] == side)]
            results = []
            for pos in matches:
                price = state["prices"].get(pos["symbol"])
                exit_price = float(price) if _finite_positive(price) else float(pos["entry"])
                results.append(self._close_position(state, pos, exit_price, "close"))
            if results:
                self._save(state)
            return results

    def _reduce_position(self, state: Dict[str, Any], pos: Dict[str, Any], lots: float,
                         exit_price: float) -> OrderResult:
        """Realize ``lots`` of ``pos`` at ``exit_price`` pro rata; the rest stays open (caller saves)."""
        part = dict(pos)
        part["lots"] = float(lots)
        gross, converted = self._pnl(part, exit_price, state["prices"])
        commission = self._commission(lots, pos["symbol"])
        net = gross - commission
        state["balance"] = float(state["balance"]) + net
        pos["lots"] = round(float(pos["lots"]) - float(lots), 8)
        now = clock.iso(clock.utcnow())
        state["history"].append({
            "ticket": pos["ticket"], "symbol": pos["symbol"], "side": pos["side"], "lots": float(lots),
            "entry": pos["entry"], "exit": exit_price, "opened_at": pos.get("opened_at"), "closed_at": now,
            "reason": "partial", "gross": round(gross, 2), "commission": round(commission, 2),
            "net": round(net, 2),
        })
        msg = "paper partial close: closed %s of %s %s at %s, P/L %.2f USD (commission %.2f), %s left" % (
            _fmt_lots(lots), pos["side"], pos["symbol"], exit_price, net, commission, _fmt_lots(pos["lots"]))
        if not converted:
            msg += " [P/L not converted to USD: no %s rate known]" % (self._quote_name(pos["symbol"]),)
        log.info("%s #%s", msg, pos["ticket"])
        return OrderResult("filled", msg, fill_price=float(exit_price), ticket=pos["ticket"], lots=float(lots))

    def close_partial(self, symbol: str, side: str, lots: float) -> List[OrderResult]:
        if side not in SIDES:
            return [OrderResult("error", "BAD_REQUEST: side must be buy or sell, got %r" % (side,))]
        if not _finite_positive(lots):
            return [OrderResult("error", "BAD_REQUEST: lots must be > 0, got %r" % (lots,))]
        with self._lock:
            state = self._load()
            key = self._key(symbol)
            matches = [p for p in state["positions"] if p["symbol"].lower() == key.lower() and p["side"] == side]
            matches.sort(key=lambda p: -float(p["lots"]))
            remaining = float(lots)
            results = []  # type: List[OrderResult]
            for pos in matches:
                if remaining <= 1e-9:
                    break
                price = state["prices"].get(pos["symbol"])
                exit_price = float(price) if _finite_positive(price) else float(pos["entry"])
                if remaining >= float(pos["lots"]) - 1e-9:
                    remaining = round(remaining - float(pos["lots"]), 8)
                    results.append(self._close_position(state, pos, exit_price, "close"))
                else:
                    results.append(self._reduce_position(state, pos, remaining, exit_price))
                    remaining = 0.0
            if results:
                self._save(state)
            return results

    def close_all(self) -> List[OrderResult]:
        with self._lock:
            state = self._load()
            symbols = []  # type: List[str]
            for pos in state["positions"]:
                if pos["symbol"] not in symbols:
                    symbols.append(pos["symbol"])
        results = []  # type: List[OrderResult]
        for sym in symbols:
            results.extend(self.close_positions(sym))
        return results

    def health(self) -> Dict[str, Any]:
        try:
            snap = self.read_account()
        except ExecutorError as e:
            return {"ok": False, "detail": str(e)}
        return {
            "ok": True,
            "detail": "paper account: balance %.2f, equity %.2f, %d open position(s)"
                      % (snap.balance, snap.equity, len(snap.positions or [])),
        }


def _fmt_lots(lots: float, decimals: int = 2) -> str:
    return "%.*f" % (max(0, int(decimals)), float(lots))
