"""Main 24/7 bot loop for BTC 5m Up/Down momentum trading."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from polymarket_bot.clob_service import ClobService, size_shares_for_stake
from polymarket_bot.ledger import TradeLedger
from polymarket_bot.market_discovery import (
    GammaClient,
    MarketInfo,
    next_window_close_ts,
    window_close_ts,
)
from polymarket_bot.risk import RiskManager
from polymarket_bot.strategy import MomentumStrategy

logger = logging.getLogger(__name__)

STATUS_FILE_PATH = config.PROJECT_ROOT / "status.json"


def write_status(dry_run: bool, extra: dict | None = None) -> None:
    """Write a small status.json for the local dashboard to read."""
    status = {
        "dry_run": dry_run,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        status.update(extra)
    path = Path(STATUS_FILE_PATH)
    path.write_text(json.dumps(status), encoding="utf-8")


class TradingBot:
    def __init__(self) -> None:
        self.gamma = GammaClient()
        self.clob = ClobService()
        self.ledger = TradeLedger()
        self.risk = RiskManager(self.ledger)
        self.strategy = MomentumStrategy()
        self._stop = threading.Event()
        self._last_heartbeat_bucket: int | None = None
        self._last_status_write = 0.0
        self._polls = 0
        self._current_market: MarketInfo | None = None

    def request_shutdown(self, signum: int | None = None, _frame: Any = None) -> None:
        name = signal.Signals(signum).name if signum is not None else "request"
        logger.info("Shutdown signal received (%s); finishing current cycle…", name)
        self._stop.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_shutdown)
        signal.signal(signal.SIGTERM, self.request_shutdown)

    def run(self) -> None:
        self.install_signal_handlers()
        logger.info(
            "Bot starting dry_run=%s stake=$%.2f trigger=%.2f confirm=%s "
            "cutoff=%ss max_entry=%.2f daily_loss_cap=$%.2f",
            config.DRY_RUN,
            config.STAKE_USD,
            config.TRIGGER_PRICE,
            config.CONFIRM_POLLS,
            config.ENTRY_CUTOFF_SECONDS,
            config.MAX_ENTRY_PRICE,
            config.DAILY_LOSS_CAP_USD,
        )
        if config.DRY_RUN:
            logger.warning(
                "DRY_RUN is True — orders will be simulated only. "
                "Set DRY_RUN = False in config.py to risk real funds."
            )

        self._resolve_pending_trades()
        close_ts = window_close_ts()

        while not self._stop.is_set():
            try:
                self._heartbeat_maybe()
                self._resolve_pending_trades()
                market = self._ensure_market(close_ts)
                if market is None:
                    break

                # Window already closed or past cutoff for discovery hop.
                now = time.time()
                if now >= market.close_ts:
                    logger.info(
                        "Window %s closed; advancing to next",
                        market.slug,
                    )
                    close_ts = next_window_close_ts(market.close_ts)
                    self._current_market = None
                    continue

                self._poll_once(market)

                # Sleep until next poll, but wake early for shutdown.
                self._stop.wait(config.POLL_INTERVAL_SECONDS)

                if time.time() >= market.close_ts:
                    logger.info("Window %s ended during poll sleep", market.slug)
                    close_ts = next_window_close_ts(market.close_ts)
                    self._current_market = None
            except InterruptedError:
                break
            except Exception as exc:  # noqa: BLE001 — never crash the 24/7 loop
                logger.exception("Unhandled error in main loop: %s", exc)
                self._stop.wait(min(5.0, config.API_RETRY_BASE_SECONDS * 4))

        logger.info("Bot stopped cleanly")

    def _ensure_market(self, close_ts: int) -> MarketInfo | None:
        if (
            self._current_market is not None
            and self._current_market.close_ts == close_ts
        ):
            return self._current_market

        market = self.gamma.wait_for_market(
            close_ts,
            stop_flag=self._stop.is_set,
        )
        self.strategy.reset_for_market(market.slug)
        if self.ledger.has_trade_for_slug(market.slug):
            self.strategy.mark_traded()
            logger.info(
                "Ledger already has a trade for %s — skipping re-entry this window",
                market.slug,
            )
        self._current_market = market
        return market

    def _poll_once(self, market: MarketInfo) -> None:
        self._polls += 1
        try:
            up_mid = self.clob.get_midpoint(market.up_token_id)
            down_mid = self.clob.get_midpoint(market.down_token_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Price poll failed: %s", exc)
            return

        if up_mid is None and down_mid is None:
            logger.debug("No midpoints for %s", market.slug)
            return

        signal = self.strategy.on_prices(
            up_mid=up_mid,
            down_mid=down_mid,
            close_ts=market.close_ts,
        )
        if signal is None:
            return

        logger.info(
            "Trigger candidate %s mid=%.4f consecutive=%s slug=%s",
            signal.outcome,
            signal.midpoint,
            signal.consecutive,
            market.slug,
        )
        self._try_enter(market, signal.outcome, signal.midpoint)

    def _try_enter(
        self, market: MarketInfo, outcome: str, trigger_price: float
    ) -> None:
        allowed, reason = self.risk.can_open_trade()
        if not allowed:
            logger.info("Trade blocked by risk: %s", reason)
            return

        if self.strategy.in_entry_cutoff(market.close_ts):
            logger.info("Inside entry cutoff; skipping")
            return

        token_id = market.token_ids[outcome]
        try:
            book = self.clob.get_book(token_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Order book fetch failed: %s", exc)
            return

        if book.best_ask is None:
            logger.info("No ask liquidity for %s — skip", outcome)
            return

        if book.best_ask > config.MAX_ENTRY_PRICE:
            logger.info(
                "Best ask %.4f > max entry %.2f — skip",
                book.best_ask,
                config.MAX_ENTRY_PRICE,
            )
            return

        shares = size_shares_for_stake(config.STAKE_USD, book.best_ask)
        if shares <= 0:
            logger.info("Sized shares=0 at ask=%.4f — skip", book.best_ask)
            return

        if book.min_order_size and shares < book.min_order_size:
            logger.warning(
                "Sized shares=%.2f below exchange min_order_size=%.2f "
                "(stake=$%.2f ask=%.4f). Skipping — raise STAKE_USD in config "
                "if you intentionally want larger size (defaults are intentional).",
                shares,
                book.min_order_size,
                config.STAKE_USD,
                book.best_ask,
            )
            return

        # One-trade-per-window lock before placement to avoid double-fire on races.
        self.strategy.mark_traded()

        result = self.clob.place_buy(
            token_id=token_id,
            price=book.best_ask,
            shares=shares,
            tick_size=book.tick_size,
            neg_risk=book.neg_risk,
            dry_run=config.DRY_RUN,
        )

        if not result.success:
            # Allow retry later in the same window if the exchange rejected.
            self.strategy.traded_this_window = False
            logger.error("Order unsuccessful: %s", result.status)
            return

        self.ledger.record_entry(
            market_slug=market.slug,
            market_question=market.question,
            outcome_bought=outcome,
            token_id=token_id,
            trigger_price=trigger_price,
            fill_price=result.fill_price or book.best_ask,
            stake=result.stake,
            shares=result.shares,
            order_id=result.order_id,
            dry_run=result.dry_run,
            status="dry_run" if result.dry_run else "submitted",
        )

    def _resolve_pending_trades(self) -> None:
        pending = self.ledger.unresolved_trades()
        if not pending:
            return

        for trade in pending:
            if self._stop.is_set():
                return
            try:
                # Parse close_ts from slug suffix when possible.
                close_ts = int(trade.market_slug.rsplit("-", 1)[-1])
            except ValueError:
                logger.warning(
                    "Cannot parse close_ts from slug %s", trade.market_slug
                )
                continue

            # Don't bother hitting Gamma until the window is over.
            if time.time() < close_ts:
                continue

            try:
                market = self.gamma.fetch_market_for_close_ts(close_ts)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Resolution fetch failed for %s: %s",
                    trade.market_slug,
                    exc,
                )
                continue

            if not market.is_resolved():
                continue

            winner = market.winning_outcome()
            if winner is None:
                continue

            # Binary payout: $1 per winning share, $0 otherwise.
            if winner == trade.outcome_bought:
                payout = trade.shares * 1.0
            else:
                payout = 0.0
            realized = payout - trade.stake

            self.ledger.mark_resolved(
                trade.id,
                resolution_outcome=winner,
                realized_pnl=realized,
            )
            self.risk.note_resolution()

    def _status_payload(self) -> dict[str, Any]:
        market = self._current_market
        close_ts = market.close_ts if market else window_close_ts()
        close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        secs = close_ts - time.time()
        return {
            "polls": self._polls,
            "market": market.slug if market else None,
            "window_close_ts": close_ts,
            "window_close_utc": close_dt.strftime("%Y-%m-%d %H:%M:%SZ"),
            "secs_to_close": round(secs, 1),
            "traded_window": self.strategy.traded_this_window,
            "daily_pnl": self.risk.daily_realized_pnl(),
            "daily_trades": self.risk.daily_trade_count(),
        }

    def _write_status_file(self) -> None:
        try:
            write_status(config.DRY_RUN, extra=self._status_payload())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write status.json: %s", exc)

    def _heartbeat_maybe(self) -> None:
        """
        Markets and heartbeat logs use UTC wall-clock 5m marks
        (:00, :05, :10, :15, …), not “N minutes since process start”.

        Price polling still runs every POLL_INTERVAL_SECONDS inside each window.
        status.json refreshes every STATUS_WRITE_SECONDS for the dashboard.
        """
        wall = time.time()
        if wall - self._last_status_write >= config.STATUS_WRITE_SECONDS:
            self._last_status_write = wall
            self._write_status_file()

        bucket = int(wall) // int(config.MARKET_WINDOW_SECONDS)
        if self._last_heartbeat_bucket == bucket:
            return
        self._last_heartbeat_bucket = bucket

        payload = self._status_payload()
        logger.info(
            "HEARTBEAT alive polls=%s dry_run=%s market=%s "
            "window_close_utc=%s secs_to_close=%.0f traded_window=%s "
            "daily_pnl=%.4f daily_trades=%s",
            payload["polls"],
            config.DRY_RUN,
            payload["market"] or "-",
            payload["window_close_utc"],
            payload["secs_to_close"],
            payload["traded_window"],
            payload["daily_pnl"],
            payload["daily_trades"],
        )
        self._last_status_write = wall
        self._write_status_file()



def main() -> None:
    from polymarket_bot.logging_setup import setup_logging

    setup_logging()
    bot = TradingBot()
    bot.run()
