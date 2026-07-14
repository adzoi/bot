"""Lightweight self-checks (no network required for strategy/sizing)."""

from __future__ import annotations

import math
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.clob_service import size_shares_for_stake
from polymarket_bot.ledger import TradeLedger
from polymarket_bot.strategy import MomentumStrategy


class SizeTests(unittest.TestCase):
    def test_never_exceeds_stake(self) -> None:
        for ask in (0.80, 0.85, 0.90, 0.95, 0.99):
            shares = size_shares_for_stake(1.0, ask)
            self.assertLessEqual(shares * ask, 1.0 + 1e-9)
            expected = math.floor(1.0 / ask * 100) / 100
            self.assertEqual(shares, expected)


class StrategyTests(unittest.TestCase):
    def test_requires_three_consecutive(self) -> None:
        s = MomentumStrategy()
        s.reset_for_market("btc-updown-5m-1")
        close_ts = int(time.time()) + 120
        # Two polls at 0.81 — not enough
        self.assertIsNone(s.on_prices(up_mid=0.81, down_mid=0.19, close_ts=close_ts))
        self.assertIsNone(s.on_prices(up_mid=0.82, down_mid=0.18, close_ts=close_ts))
        sig = s.on_prices(up_mid=0.83, down_mid=0.17, close_ts=close_ts)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.outcome, "Up")
        self.assertGreaterEqual(sig.consecutive, 3)

    def test_spike_then_drop_does_not_fire(self) -> None:
        s = MomentumStrategy()
        s.reset_for_market("btc-updown-5m-2")
        close_ts = int(time.time()) + 120
        self.assertIsNone(s.on_prices(up_mid=0.90, down_mid=0.10, close_ts=close_ts))
        self.assertIsNone(s.on_prices(up_mid=0.50, down_mid=0.50, close_ts=close_ts))
        self.assertIsNone(s.on_prices(up_mid=0.90, down_mid=0.10, close_ts=close_ts))
        # Only 1 consecutive at end
        self.assertIsNone(s.on_prices(up_mid=0.91, down_mid=0.09, close_ts=close_ts))

    def test_cutoff_blocks(self) -> None:
        s = MomentumStrategy()
        s.reset_for_market("btc-updown-5m-3")
        close_ts = int(time.time()) + 5  # inside 10s cutoff
        for p in (0.85, 0.86, 0.87):
            self.assertIsNone(s.on_prices(up_mid=p, down_mid=0.15, close_ts=close_ts))

    def test_one_trade_per_window(self) -> None:
        s = MomentumStrategy()
        s.reset_for_market("btc-updown-5m-4")
        close_ts = int(time.time()) + 120
        for p in (0.85, 0.86, 0.87):
            sig = s.on_prices(up_mid=p, down_mid=0.15, close_ts=close_ts)
        self.assertIsNotNone(sig)
        s.mark_traded()
        self.assertIsNone(s.on_prices(up_mid=0.90, down_mid=0.10, close_ts=close_ts))


class LedgerTests(unittest.TestCase):
    def test_pnl_and_daily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TradeLedger(Path(tmp) / "t.sqlite3")
            tid = ledger.record_entry(
                market_slug="btc-updown-5m-100",
                market_question="test",
                outcome_bought="Up",
                token_id="1",
                trigger_price=0.85,
                fill_price=0.85,
                stake=0.85,
                shares=1.0,
                order_id="x",
                dry_run=True,
                status="dry_run",
            )
            daily = ledger.mark_resolved(tid, resolution_outcome="Down", realized_pnl=-0.85)
            self.assertAlmostEqual(daily, -0.85)
            self.assertTrue(ledger.has_trade_for_slug("btc-updown-5m-100"))
            self.assertEqual(len(ledger.unresolved_trades()), 0)


if __name__ == "__main__":
    unittest.main()
