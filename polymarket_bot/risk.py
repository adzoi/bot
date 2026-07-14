"""Daily risk controls: realized loss cap and optional trade-count cap."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from polymarket_bot.ledger import TradeLedger

logger = logging.getLogger(__name__)


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, ledger: TradeLedger) -> None:
        self._ledger = ledger
        self._halted_day: str | None = None
        self._last_known_day = utc_today()

    def _check_day_rollover(self) -> None:
        today = utc_today()
        if today != self._last_known_day:
            logger.info(
                "UTC day rolled %s -> %s; daily risk controls reset",
                self._last_known_day,
                today,
            )
            self._last_known_day = today
            self._halted_day = None

    def daily_realized_pnl(self) -> float:
        return self._ledger.daily_realized_pnl(utc_today())

    def daily_trade_count(self) -> int:
        return self._ledger.daily_trade_count(utc_today())

    def can_open_trade(self) -> tuple[bool, str]:
        self._check_day_rollover()
        today = utc_today()
        pnl = self.daily_realized_pnl()

        # Loss cap: if cumulative realized PnL <= -DAILY_LOSS_CAP, halt.
        if pnl <= -abs(config.DAILY_LOSS_CAP_USD):
            if self._halted_day != today:
                logger.warning(
                    "DAILY LOSS CAP HIT: realized PnL=$%.4f <= -$%.2f for UTC %s. "
                    "No new trades until UTC midnight.",
                    pnl,
                    abs(config.DAILY_LOSS_CAP_USD),
                    today,
                )
                self._halted_day = today
            return False, f"daily_loss_cap pnl={pnl:.4f}"

        # If we were halted but PnL recovered somehow (shouldn't for same day
        # after cap without new wins), clear flag when under cap.
        if self._halted_day == today and pnl > -abs(config.DAILY_LOSS_CAP_USD):
            self._halted_day = None

        cap = config.DAILY_TRADE_COUNT_CAP
        if cap is not None and cap > 0:
            count = self.daily_trade_count()
            if count >= cap:
                return False, f"daily_trade_count_cap count={count} cap={cap}"

        return True, "ok"

    def note_resolution(self) -> None:
        """Re-evaluate halt state after a trade resolves."""
        self.can_open_trade()
