"""CLOB client wrapper for prices, books, and order execution."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

import config
from polymarket_bot.retries import with_retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookSnapshot:
    best_bid: float | None
    best_ask: float | None
    min_order_size: float
    tick_size: str
    neg_risk: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class OrderResult:
    dry_run: bool
    success: bool
    order_id: str | None
    fill_price: float | None
    shares: float
    stake: float
    status: str
    raw: dict[str, Any] | None


def size_shares_for_stake(stake_usd: float, best_ask: float) -> float:
    """
    Buy floor(stake/ask * 100) / 100 shares so cost is as close as possible
    to stake without exceeding it (cost = shares * ask <= stake).
    """
    if best_ask <= 0:
        return 0.0
    return math.floor(stake_usd / best_ask * 100) / 100


def _best_levels(book: dict[str, Any]) -> tuple[float | None, float | None]:
    """
    Polymarket often returns bids ascending and asks descending.
    Always derive best levels via max/min for safety.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max((float(b["price"]) for b in bids), default=None)
    best_ask = min((float(a["price"]) for a in asks), default=None)
    return best_bid, best_ask


class ClobService:
    """Read + write access to Polymarket CLOB (py-clob-client-v2)."""

    def __init__(self) -> None:
        load_dotenv()
        self._client = None
        self._authenticated = False

    def _ensure_read_client(self) -> Any:
        if self._client is not None:
            return self._client
        from py_clob_client_v2 import ClobClient

        self._client = ClobClient(
            host=config.CLOB_API_HOST,
            chain_id=config.CHAIN_ID,
        )
        return self._client

    def _ensure_trade_client(self) -> Any:
        if self._authenticated and self._client is not None:
            return self._client

        from py_clob_client_v2 import ClobClient

        pk = os.environ.get("PK", "").strip()
        if not pk:
            raise RuntimeError("PK environment variable is required for live trading")

        sig_raw = os.environ.get("SIGNATURE_TYPE", "0").strip() or "0"
        try:
            signature_type = int(sig_raw)
        except ValueError as exc:
            raise RuntimeError("SIGNATURE_TYPE must be an integer (0, 1, or 2)") from exc

        funder = os.environ.get("FUNDER_ADDRESS", "").strip() or None
        kwargs: dict[str, Any] = {
            "host": config.CLOB_API_HOST,
            "chain_id": config.CHAIN_ID,
            "key": pk,
            "signature_type": signature_type,
        }
        if funder:
            kwargs["funder"] = funder

        client = ClobClient(**kwargs)
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        self._client = client
        self._authenticated = True
        # Never log PK / funder / creds.
        logger.info("CLOB trade client authenticated (signature_type=%s)", signature_type)
        return self._client

    def get_midpoint(self, token_id: str) -> float | None:
        client = self._ensure_read_client()

        def _do() -> float | None:
            resp = client.get_midpoint(token_id)
            if resp is None:
                return None
            if isinstance(resp, dict):
                mid = resp.get("mid")
            else:
                mid = resp
            if mid is None or mid == "":
                return None
            return float(mid)

        return with_retry(_do, op_name=f"clob.midpoint({token_id[:12]}…)")

    def get_book(self, token_id: str) -> BookSnapshot:
        client = self._ensure_read_client()

        def _do() -> BookSnapshot:
            book = client.get_order_book(token_id)
            if not isinstance(book, dict):
                # Older object-style responses
                book = {
                    "bids": [
                        {"price": getattr(b, "price", b[0]), "size": getattr(b, "size", b[1])}
                        for b in (getattr(book, "bids", None) or [])
                    ],
                    "asks": [
                        {"price": getattr(a, "price", a[0]), "size": getattr(a, "size", a[1])}
                        for a in (getattr(book, "asks", None) or [])
                    ],
                    "min_order_size": getattr(book, "min_order_size", "0"),
                    "tick_size": getattr(book, "tick_size", "0.01"),
                    "neg_risk": getattr(book, "neg_risk", False),
                }
            best_bid, best_ask = _best_levels(book)
            return BookSnapshot(
                best_bid=best_bid,
                best_ask=best_ask,
                min_order_size=float(book.get("min_order_size") or 0),
                tick_size=str(book.get("tick_size") or "0.01"),
                neg_risk=bool(book.get("neg_risk", False)),
                raw=book,
            )

        return with_retry(_do, op_name=f"clob.book({token_id[:12]}…)")

    def place_buy(
        self,
        *,
        token_id: str,
        price: float,
        shares: float,
        tick_size: str,
        neg_risk: bool,
        dry_run: bool,
    ) -> OrderResult:
        stake = round(shares * price, 6)
        if dry_run:
            logger.info(
                "DRY_RUN buy token=%s… price=%.4f shares=%.2f stake~%.4f",
                token_id[:16],
                price,
                shares,
                stake,
            )
            return OrderResult(
                dry_run=True,
                success=True,
                order_id="dry-run",
                fill_price=price,
                shares=shares,
                stake=stake,
                status="dry_run",
                raw=None,
            )

        from py_clob_client_v2 import (
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        client = self._ensure_trade_client()
        order_type = OrderType.FOK if config.ORDER_TYPE.upper() == "FOK" else OrderType.FAK

        def _do() -> dict[str, Any]:
            return client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=shares,
                    side=Side.BUY,
                ),
                options=PartialCreateOrderOptions(
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                ),
                order_type=order_type,
            )

        try:
            resp = with_retry(_do, op_name="clob.place_buy")
        except Exception as exc:  # noqa: BLE001
            logger.error("Order placement failed: %s", exc)
            return OrderResult(
                dry_run=False,
                success=False,
                order_id=None,
                fill_price=None,
                shares=shares,
                stake=stake,
                status=f"error:{exc}",
                raw=None,
            )

        order_id = None
        status = "submitted"
        fill_price = price
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
            status = str(resp.get("status") or status)
            # Some responses include average fill price
            for key in ("average_price", "avgPrice", "price"):
                if resp.get(key) is not None:
                    try:
                        fill_price = float(resp[key])
                    except (TypeError, ValueError):
                        pass
                    break

        ok = status.lower() not in {"rejected", "canceled", "cancelled", "failed"}
        return OrderResult(
            dry_run=False,
            success=ok,
            order_id=str(order_id) if order_id else None,
            fill_price=fill_price,
            shares=shares,
            stake=stake,
            status=status,
            raw=resp if isinstance(resp, dict) else {"resp": resp},
        )
