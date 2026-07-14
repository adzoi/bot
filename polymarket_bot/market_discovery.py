"""Gamma API market discovery for BTC 5m Up/Down windows."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

import requests

import config
from polymarket_bot.retries import with_retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketInfo:
    slug: str
    question: str
    condition_id: str
    close_ts: int
    end_date: str | None
    outcomes: list[str]
    token_ids: dict[str, str]  # outcome name -> token id
    closed: bool
    accepting_orders: bool
    outcome_prices: dict[str, float] | None
    raw: dict[str, Any]

    @property
    def up_token_id(self) -> str:
        return self.token_ids["Up"]

    @property
    def down_token_id(self) -> str:
        return self.token_ids["Down"]

    def is_resolved(self) -> bool:
        if not self.closed or not self.outcome_prices:
            return False
        prices = list(self.outcome_prices.values())
        # Resolved binary markets settle to ~0 / ~1.
        return any(p >= 0.99 for p in prices) and any(p <= 0.01 for p in prices)

    def winning_outcome(self) -> str | None:
        if not self.is_resolved() or not self.outcome_prices:
            return None
        return max(self.outcome_prices.items(), key=lambda kv: kv[1])[0]


def window_close_ts(now: float | None = None) -> int:
    """Unix timestamp of the currently trading 5m window's close."""
    t = int(now if now is not None else time.time())
    w = config.MARKET_WINDOW_SECONDS
    return ((t // w) + 1) * w


def next_window_close_ts(close_ts: int) -> int:
    return close_ts + config.MARKET_WINDOW_SECONDS


def slug_for_close_ts(close_ts: int) -> str:
    return f"{config.ASSET_SLUG_PREFIX}-{close_ts}"


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _parse_market(event: dict[str, Any], close_ts: int) -> MarketInfo:
    markets = event.get("markets") or []
    if not markets:
        raise ValueError(f"Event has no markets: {event.get('slug')}")

    m = markets[0]
    outcomes = _parse_json_field(m.get("outcomes")) or []
    token_ids_list = _parse_json_field(m.get("clobTokenIds")) or []
    prices_list = _parse_json_field(m.get("outcomePrices"))

    if len(outcomes) != len(token_ids_list):
        raise ValueError(
            f"outcomes/token length mismatch for {event.get('slug')}: "
            f"{outcomes!r} vs {token_ids_list!r}"
        )

    token_ids = {str(o): str(t) for o, t in zip(outcomes, token_ids_list, strict=True)}
    if "Up" not in token_ids or "Down" not in token_ids:
        raise ValueError(f"Expected Up/Down outcomes, got {list(token_ids)}")

    outcome_prices: dict[str, float] | None = None
    if prices_list is not None and len(prices_list) == len(outcomes):
        outcome_prices = {
            str(o): float(p) for o, p in zip(outcomes, prices_list, strict=True)
        }

    question = m.get("question") or event.get("title") or event.get("slug") or ""
    return MarketInfo(
        slug=str(event.get("slug") or slug_for_close_ts(close_ts)),
        question=str(question),
        condition_id=str(m.get("conditionId") or ""),
        close_ts=close_ts,
        end_date=m.get("endDate"),
        outcomes=[str(o) for o in outcomes],
        token_ids=token_ids,
        closed=bool(m.get("closed")),
        accepting_orders=bool(m.get("acceptingOrders")),
        outcome_prices=outcome_prices,
        raw=m,
    )


class GammaClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "polymarket-btc-5m-bot/1.0")

    def fetch_event_by_slug(self, slug: str) -> dict[str, Any]:
        url = f"{config.GAMMA_API_BASE}/events/slug/{slug}"

        def _do() -> dict[str, Any]:
            resp = self._session.get(url, timeout=config.API_REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 404:
                raise LookupError(f"Market not found: {slug}")
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(f"Unexpected Gamma response for {slug}")
            return data

        return with_retry(_do, op_name=f"gamma.fetch({slug})")

    def fetch_market_for_close_ts(self, close_ts: int) -> MarketInfo:
        slug = slug_for_close_ts(close_ts)
        event = self.fetch_event_by_slug(slug)
        return _parse_market(event, close_ts)

    def wait_for_market(
        self,
        close_ts: int,
        *,
        stop_flag: Callable[[], bool] | None = None,
    ) -> MarketInfo:
        """
        Fetch market metadata, retrying with backoff if not published yet.

        ``stop_flag`` is an optional zero-arg callable returning True to abort.
        """
        backoff = config.MARKET_FETCH_INITIAL_BACKOFF_SECONDS
        slug = slug_for_close_ts(close_ts)
        while True:
            if stop_flag and stop_flag():
                raise InterruptedError("shutdown requested while waiting for market")
            try:
                market = self.fetch_market_for_close_ts(close_ts)
                logger.info(
                    "Loaded market %s close_ts=%s accepting=%s closed=%s",
                    market.slug,
                    market.close_ts,
                    market.accepting_orders,
                    market.closed,
                )
                return market
            except LookupError:
                logger.info(
                    "Market %s not published yet; retrying in %.1fs",
                    slug,
                    backoff,
                )
            except Exception as exc:  # noqa: BLE001 — keep process alive
                logger.warning(
                    "Error fetching %s: %s; retrying in %.1fs",
                    slug,
                    exc,
                    backoff,
                )
            # Sleep in short slices so SIGTERM can interrupt promptly.
            remaining = backoff
            while remaining > 0:
                if stop_flag and stop_flag():
                    raise InterruptedError("shutdown requested while waiting for market")
                step = min(0.5, remaining)
                time.sleep(step)
                remaining -= step
            backoff = min(
                config.MARKET_FETCH_MAX_BACKOFF_SECONDS,
                backoff * 1.5,
            )

    def refresh_market(self, market: MarketInfo) -> MarketInfo:
        return self.fetch_market_for_close_ts(market.close_ts)
