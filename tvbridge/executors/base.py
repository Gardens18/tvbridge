"""Executor interface shared by the paper simulator and the MT5 GUI clicker.

An executor is owned by exactly one thread (the engine's executor thread). It turns
approved :class:`~tvbridge.models.OrderRequest` objects into orders and reports what it
observed. The contract every implementation follows:

* ``open_market`` returns an :class:`~tvbridge.models.OrderResult` whose ``status`` is
  "filled", "rejected", "uncertain", "rehearsed" or "error". It may raise
  :class:`ExecutorError` only *before* anything was sent to the broker; once an order may
  have been sent, problems are reported as ``"uncertain"`` instead of raised.
* ``open_market`` with ``OrderRequest.sl_distance`` computes the absolute stop (and the
  take-profit from ``tp_distance``) from the live quote of its own order ticket and reports
  the values it used in ``OrderResult.sl`` / ``OrderResult.tp``.
* ``close_positions`` returns one result per position it acted on and ``[]`` when no
  position matched. ``close_partial`` closes a number of lots instead of whole positions.
* ``read_account`` returns an :class:`~tvbridge.models.AccountSnapshot` or raises
  :class:`ExecutorError`.
"""

from typing import Any, Dict, List, Optional

from ..models import AccountSnapshot, OrderRequest, OrderResult


class ExecutorError(Exception):
    """A failure with a stable reason code (e.g. ``"MT5_NOT_FOUND"``) and a human message."""

    def __init__(self, code: str, message: str = ""):
        self.code = str(code)
        self.message = str(message or "")
        super().__init__("%s: %s" % (self.code, self.message) if self.message else self.code)

    @property
    def reason(self) -> str:
        """``"CODE: message"`` (or just ``"CODE"``), the format used for signal reasons."""
        return str(self)


class Executor:
    """Abstract executor. Subclasses implement every method except :meth:`set_price_hint`."""

    name = "base"

    def read_account(self) -> AccountSnapshot:
        """Current balance/equity (and open positions when they can be read)."""
        raise NotImplementedError

    def open_market(self, req: OrderRequest) -> OrderResult:
        """Place one market order. Never retried by the caller."""
        raise NotImplementedError

    def close_positions(self, symbol: str, side: Optional[str] = None) -> List[OrderResult]:
        """Close every open position on ``symbol`` (restricted to ``side`` if given).

        Returns one result per position acted on, ``[]`` when nothing matched.
        """
        raise NotImplementedError

    def close_partial(self, symbol: str, side: str, lots: float) -> List[OrderResult]:
        """Close ``lots`` of the position(s) on ``symbol``/``side``, largest position first.

        Positions the remaining amount covers are closed whole; the last one is reduced.
        Returns one result per position acted on (``lots`` = what was closed there; statuses
        as :meth:`close_positions`, plus "rehearsed"), ``[]`` when nothing matched.
        """
        raise NotImplementedError

    def close_all(self) -> List[OrderResult]:
        """Close every open position on every symbol."""
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        """``{"ok": bool, "detail": str}``."""
        raise NotImplementedError

    def set_price_hint(self, symbol: str, price: float) -> None:
        """Tell the executor about a recent market price (used by the paper simulator)."""
        return None
