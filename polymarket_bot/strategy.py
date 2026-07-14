"""Momentum trigger logic for BTC 5m Up/Down markets."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import config


@dataclass
class OutcomeSeries:
    name: str
    # (monotonic_ts, midpoint)
    samples: deque[tuple[float, float]] = field(default_factory=deque)

    def add(self, price: float, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        self.samples.append((ts, price))
        self._trim(ts)

    def _trim(self, now: float) -> None:
        cutoff = now - config.PRICE_HISTORY_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def consecutive_above(self, threshold: float) -> int:
        """Count how many samples from the end are >= threshold (stops at first miss)."""
        count = 0
        for _, price in reversed(self.samples):
            if price >= threshold:
                count += 1
            else:
                break
        return count

    def ready(self) -> bool:
        return len(self.samples) >= config.CONFIRM_POLLS

    def last_price(self) -> float | None:
        return self.samples[-1][1] if self.samples else None


@dataclass
class TriggerSignal:
    outcome: str
    midpoint: float
    consecutive: int


class MomentumStrategy:
    """
    Triggers a buy only when an outcome midpoint stays >= TRIGGER_PRICE
    for CONFIRM_POLLS consecutive polls within the rolling PRICE_HISTORY window.
    """

    def __init__(self) -> None:
        self.up = OutcomeSeries("Up")
        self.down = OutcomeSeries("Down")
        self.traded_this_window = False
        self.window_slug: str | None = None

    def reset_for_market(self, slug: str) -> None:
        self.up = OutcomeSeries("Up")
        self.down = OutcomeSeries("Down")
        self.traded_this_window = False
        self.window_slug = slug

    def mark_traded(self) -> None:
        self.traded_this_window = True

    def seconds_to_close(self, close_ts: int, now: float | None = None) -> float:
        t = now if now is not None else time.time()
        return close_ts - t

    def in_entry_cutoff(self, close_ts: int, now: float | None = None) -> bool:
        return self.seconds_to_close(close_ts, now) <= config.ENTRY_CUTOFF_SECONDS

    def on_prices(
        self,
        *,
        up_mid: float | None,
        down_mid: float | None,
        close_ts: int,
        now: float | None = None,
    ) -> TriggerSignal | None:
        """
        Ingest a poll. Returns a TriggerSignal when guardrails allow an entry,
        otherwise None.
        """
        mono = time.monotonic()
        wall = now if now is not None else time.time()

        if up_mid is not None:
            self.up.add(up_mid, mono)
        if down_mid is not None:
            self.down.add(down_mid, mono)

        if self.traded_this_window:
            return None
        if self.in_entry_cutoff(close_ts, wall):
            return None

        candidates: list[TriggerSignal] = []
        for series in (self.up, self.down):
            if not series.ready():
                continue
            consecutive = series.consecutive_above(config.TRIGGER_PRICE)
            if consecutive >= config.CONFIRM_POLLS:
                last = series.last_price()
                assert last is not None
                candidates.append(
                    TriggerSignal(
                        outcome=series.name,
                        midpoint=last,
                        consecutive=consecutive,
                    )
                )

        if not candidates:
            return None

        # If both somehow sustain ≥80% (shouldn't happen long), take the stronger mid.
        candidates.sort(key=lambda s: (s.midpoint, s.consecutive), reverse=True)
        return candidates[0]
