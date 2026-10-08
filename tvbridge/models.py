"""Plain data classes shared by every tvbridge module (no business logic)."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import clock

ACTIONS = ("buy", "sell", "close", "close_all", "sync")
SIDES = ("buy", "sell")


def opposite_side(side: str) -> str:
    """"buy" -> "sell" and vice versa. Raises ValueError for anything else."""
    if side == "buy":
        return "sell"
    if side == "sell":
        return "buy"
    raise ValueError("not a side: %r" % (side,))


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def _dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return clock.ensure_utc(v)
    return clock.parse_tv_time(v)


@dataclass
class Signal:
    """A validated trading signal parsed from a TradingView alert."""

    id: str
    action: str                      # buy|sell|close|close_all|sync
    tv_symbol: str                   # normalized TradingView symbol, e.g. "EURUSD"; "" for close_all
    symbol: str                      # MT5 symbol, e.g. "EURUSD.h"; "" for close_all
    side: Optional[str]              # close only: restrict to "buy"/"sell" positions; None = both
    price: Optional[float]           # alert's reference price ({{close}})
    sl: Optional[float]              # absolute price
    tp: Optional[float]              # absolute price
    risk_pct: Optional[float]        # optional per-signal override, still capped by config
    quote_usd: Optional[float]       # USD value of 1 unit of the quote currency (crosses)
    fired_at: datetime               # UTC, from alert {{timenow}}
    received_at: datetime            # UTC
    strategy: str = ""
    comment: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)   # payload minus secret
    # sync (mirror mode) only: the strategy's resulting position
    target_side: Optional[str] = None      # "buy" (long) / "sell" (short) / None (flat)
    target_units: Optional[float] = None   # strategy units (e.g. ounces), >= 0; None when flat

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict (datetimes as ISO strings with "Z")."""
        return {
            "id": self.id,
            "action": self.action,
            "tv_symbol": self.tv_symbol,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "sl": self.sl,
            "tp": self.tp,
            "risk_pct": self.risk_pct,
            "quote_usd": self.quote_usd,
            "fired_at": clock.iso(self.fired_at),
            "received_at": clock.iso(self.received_at),
            "strategy": self.strategy,
            "comment": self.comment,
            "raw": dict(self.raw or {}),
            "target_side": self.target_side,
            "target_units": self.target_units,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Signal":
        """Inverse of :meth:`to_dict`. Missing optional keys default to None/""."""
        return Signal(
            id=str(d["id"]),
            action=str(d["action"]),
            tv_symbol=str(d.get("tv_symbol") or ""),
            symbol=str(d.get("symbol") or ""),
            side=d.get("side") or None,
            price=_opt_float(d.get("price")),
            sl=_opt_float(d.get("sl")),
            tp=_opt_float(d.get("tp")),
            risk_pct=_opt_float(d.get("risk_pct")),
            quote_usd=_opt_float(d.get("quote_usd")),
            fired_at=_dt(d["fired_at"]),
            received_at=_dt(d["received_at"]),
            strategy=str(d.get("strategy") or ""),
            comment=str(d.get("comment") or ""),
            raw=dict(d.get("raw") or {}),
            target_side=d.get("target_side") or None,
            target_units=_opt_float(d.get("target_units")),
        )

    @property
    def is_entry(self) -> bool:
        return self.action in SIDES


@dataclass
class ObservedPosition:
    """A position as read from the MT5 terminal (or the paper simulator)."""

    symbol: str
    side: str
    lots: float
    ticket: Optional[str] = None
    open_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    profit: Optional[float] = None
    swap: Optional[float] = None       # Swap column, when the Trade tab shows one
    sl_missing: bool = False           # the S/L column was read and shows 0 (no stop-loss on the server)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ObservedPosition":
        return ObservedPosition(
            symbol=str(d["symbol"]),
            side=str(d["side"]),
            lots=float(d["lots"]),
            ticket=None if d.get("ticket") is None else str(d["ticket"]),
            open_price=_opt_float(d.get("open_price")),
            sl=_opt_float(d.get("sl")),
            tp=_opt_float(d.get("tp")),
            profit=_opt_float(d.get("profit")),
            swap=_opt_float(d.get("swap")),
            sl_missing=bool(d.get("sl_missing", False)),
        )


@dataclass
class AccountSnapshot:
    """Balance/equity reading at a point in time."""

    ts: datetime
    balance: float
    equity: float
    margin: Optional[float] = None
    free_margin: Optional[float] = None
    positions: Optional[List[ObservedPosition]] = None   # None = unknown/unparsed
    source: str = ""                                     # "paper" | "mt5gui"
    # Why ``positions`` is None although the account line was read, e.g.
    # "TOOLBOX_INCOMPLETE: ..." (the position list could not be verified as complete).
    positions_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": clock.iso(self.ts),
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "free_margin": self.free_margin,
            "positions": None if self.positions is None else [p.to_dict() for p in self.positions],
            "source": self.source,
            "positions_note": self.positions_note,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AccountSnapshot":
        pos = d.get("positions")
        return AccountSnapshot(
            ts=_dt(d["ts"]),
            balance=float(d["balance"]),
            equity=float(d["equity"]),
            margin=_opt_float(d.get("margin")),
            free_margin=_opt_float(d.get("free_margin")),
            positions=None if pos is None else [ObservedPosition.from_dict(p) for p in pos],
            source=str(d.get("source") or ""),
            positions_note=str(d.get("positions_note") or ""),
        )


@dataclass
class OrderRequest:
    """A market order the executor should place."""

    symbol: str
    side: str
    lots: float
    sl: float
    tp: Optional[float]
    digits: int
    lot_decimals: int = 2
    comment: str = ""
    price_hint: Optional[float] = None
    quote_usd: Optional[float] = None   # signal's quote->USD rate (paper P/L for crosses)
    # Mirror mode: when ``sl_distance`` is set the executor computes the absolute stop from the
    # live quote of its own order ticket (buy: ask - sl_distance, sell: bid + sl_distance), and the
    # take-profit likewise from ``tp_distance`` (buy: ask + tp_distance, sell: bid - tp_distance).
    # ``sl``/``tp`` are then only a fallback for executors without a quote of their own.
    sl_distance: Optional[float] = None
    tp_distance: Optional[float] = None
    # override of mirror.max_price_gap_pct for this order (a re-entry's hint is the stop price of
    # the position it replaces, not a live alert price); None = the configured value
    max_price_gap_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderResult:
    """Outcome of an executor action."""

    status: str        # "filled" | "rejected" | "uncertain" | "rehearsed" | "error" | "no_position"
    message: str = ""
    fill_price: Optional[float] = None
    ticket: Optional[str] = None
    lots: Optional[float] = None
    evidence: List[str] = field(default_factory=list)     # screenshot paths
    sl: Optional[float] = None         # open_market: the stop-loss actually put on the order
    tp: Optional[float] = None         # open_market: the take-profit actually put on the order

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [str(p) for p in self.evidence]
        return d


@dataclass
class TradePlan:
    """Risk-guard decision for an entry signal."""

    approved: bool
    reason: str = ""          # "" when approved, else "CODE: human text"
    lots: float = 0.0
    risk_usd: float = 0.0     # worst-case loss incl. commission and slippage buffer
    per_lot_loss_usd: float = 0.0
    close_first: List[ObservedPosition] = field(default_factory=list)  # opposite positions to close before entry (reversal)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> str:
        """The reason code ("" when approved), e.g. "PAUSED" from "PAUSED: paused by user"."""
        return self.reason.split(":", 1)[0].strip() if self.reason else ""
