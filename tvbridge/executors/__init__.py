"""Executors: where approved orders go (paper simulator or the MT5 GUI clicker)."""

from typing import Any, Optional

from .base import Executor, ExecutorError

__all__ = ["Executor", "ExecutorError", "make_executor"]


def make_executor(cfg: Any, store: Any, driver: Optional[Any] = None) -> Executor:
    """Build the executor for ``cfg.executor.mode``.

    * ``paper``     -> :class:`~tvbridge.executors.paper.PaperExecutor` (``driver`` unused)
    * ``rehearsal`` -> :class:`~tvbridge.executors.mt5gui.Mt5GuiExecutor` that never clicks Buy/Sell/Close
    * ``live``      -> :class:`~tvbridge.executors.mt5gui.Mt5GuiExecutor` that does

    Live mode needs ``account.account_login`` (``ExecutorError("ACCOUNT_LOGIN_REQUIRED")``).
    For the GUI modes the calibration is loaded first (``CalibrationError`` propagates with a
    "run `tvbridge calibrate`" message), then ``driver`` defaults to a real ``MacDriver``.
    """
    mode = (cfg.executor.mode or "").strip().lower()
    if mode == "paper":
        from .paper import PaperExecutor

        return PaperExecutor(cfg, store)
    if mode in ("rehearsal", "live"):
        from ..gui.calibration import load_calibration
        from .mt5gui import Mt5GuiExecutor

        if mode == "live" and not (cfg.account.account_login or "").strip():
            # The login in the window title is what tells the challenge terminal apart from
            # MetaEditor, MT4 or a second terminal; live trading never guesses.
            raise ExecutorError("ACCOUNT_LOGIN_REQUIRED", "executor.mode is live but account.account_login is "
                                "empty: set it to your MT5 login number in config.json")

        calib = load_calibration(cfg.calibration_path)
        if driver is None:
            from ..gui.driver import default_driver

            driver = default_driver()
        return Mt5GuiExecutor(cfg, driver, calib, rehearsal=(mode == "rehearsal"), shots_dir=cfg.shots_dir)
    raise ExecutorError("BAD_MODE", "executor.mode must be paper, rehearsal or live (got %r)" % (mode,))
