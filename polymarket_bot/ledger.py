"""Persistent SQLite trade ledger with resolution / daily PnL tracking."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TradeRecord:
    id: int
    created_at: str
    market_slug: str
    market_question: str
    outcome_bought: str
    token_id: str
    trigger_price: float
    fill_price: float
    stake: float
    shares: float
    order_id: str | None
    dry_run: int
    status: str
    resolution_outcome: str | None
    realized_pnl: float | None
    resolved_at: str | None
    daily_pnl_after: float | None


class TradeLedger:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or config.LEDGER_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    market_slug TEXT NOT NULL,
                    market_question TEXT NOT NULL,
                    outcome_bought TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    trigger_price REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    stake REAL NOT NULL,
                    shares REAL NOT NULL,
                    order_id TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    resolution_outcome TEXT,
                    realized_pnl REAL,
                    resolved_at TEXT,
                    daily_pnl_after REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_utc_day ON trades(utc_day)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_unresolved "
                "ON trades(status) WHERE resolution_outcome IS NULL"
            )
            conn.commit()

    def record_entry(
        self,
        *,
        market_slug: str,
        market_question: str,
        outcome_bought: str,
        token_id: str,
        trigger_price: float,
        fill_price: float,
        stake: float,
        shares: float,
        order_id: str | None,
        dry_run: bool,
        status: str,
    ) -> int:
        created = _utc_now_iso()
        utc_day = created[:10]
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    created_at, utc_day, market_slug, market_question,
                    outcome_bought, token_id, trigger_price, fill_price,
                    stake, shares, order_id, dry_run, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created,
                    utc_day,
                    market_slug,
                    market_question,
                    outcome_bought,
                    token_id,
                    trigger_price,
                    fill_price,
                    stake,
                    shares,
                    order_id,
                    1 if dry_run else 0,
                    status,
                ),
            )
            conn.commit()
            trade_id = int(cur.lastrowid)
        logger.info(
            "Ledger entry #%s slug=%s outcome=%s fill=%.4f shares=%.2f stake=%.4f dry_run=%s",
            trade_id,
            market_slug,
            outcome_bought,
            fill_price,
            shares,
            stake,
            dry_run,
        )
        return trade_id

    def mark_resolved(
        self,
        trade_id: int,
        *,
        resolution_outcome: str,
        realized_pnl: float,
    ) -> float:
        """Mark trade resolved; returns running cumulative realized PnL for that UTC day."""
        resolved_at = _utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT utc_day FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"trade id {trade_id} not found")
            utc_day = row["utc_day"]
            conn.execute(
                """
                UPDATE trades
                SET resolution_outcome = ?,
                    realized_pnl = ?,
                    resolved_at = ?,
                    status = 'resolved'
                WHERE id = ?
                """,
                (resolution_outcome, realized_pnl, resolved_at, trade_id),
            )
            daily = self._daily_realized_pnl_unlocked(conn, utc_day)
            conn.execute(
                "UPDATE trades SET daily_pnl_after = ? WHERE id = ?",
                (daily, trade_id),
            )
            conn.commit()
        logger.info(
            "Ledger resolved #%s winner=%s pnl=%.4f daily_pnl_after=%.4f",
            trade_id,
            resolution_outcome,
            realized_pnl,
            daily,
        )
        return daily

    def _daily_realized_pnl_unlocked(
        self, conn: sqlite3.Connection, utc_day: str
    ) -> float:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
            FROM trades
            WHERE utc_day = ? AND realized_pnl IS NOT NULL
            """,
            (utc_day,),
        ).fetchone()
        return float(row["pnl"])

    def daily_realized_pnl(self, utc_day: str) -> float:
        with self._lock, self._connect() as conn:
            return self._daily_realized_pnl_unlocked(conn, utc_day)

    def daily_trade_count(self, utc_day: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM trades
                WHERE utc_day = ? AND status IN ('submitted', 'dry_run', 'resolved', 'filled')
                """,
                (utc_day,),
            ).fetchone()
            return int(row["c"])

    def has_trade_for_slug(self, market_slug: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE market_slug = ? LIMIT 1",
                (market_slug,),
            ).fetchone()
            return row is not None

    def unresolved_trades(self) -> list[TradeRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trades
                WHERE resolution_outcome IS NULL
                  AND status IN ('submitted', 'dry_run', 'filled', 'pending_resolution')
                ORDER BY id ASC
                """
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=int(row["id"]),
            created_at=row["created_at"],
            market_slug=row["market_slug"],
            market_question=row["market_question"],
            outcome_bought=row["outcome_bought"],
            token_id=row["token_id"],
            trigger_price=float(row["trigger_price"]),
            fill_price=float(row["fill_price"]),
            stake=float(row["stake"]),
            shares=float(row["shares"]),
            order_id=row["order_id"],
            dry_run=int(row["dry_run"]),
            status=row["status"],
            resolution_outcome=row["resolution_outcome"],
            realized_pnl=(
                float(row["realized_pnl"]) if row["realized_pnl"] is not None else None
            ),
            resolved_at=row["resolved_at"],
            daily_pnl_after=(
                float(row["daily_pnl_after"])
                if row["daily_pnl_after"] is not None
                else None
            ),
        )

    def export_summary(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]
