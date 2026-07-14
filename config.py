"""
Central configuration for the BTC 5m Up/Down momentum bot.

All strategy thresholds and risk guardrails live here.
Do NOT weaken guardrails in code — change values here deliberately (or ask before changing defaults).

DRY_RUN defaults to True. You must manually set DRY_RUN = False to risk real funds.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / process
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
LEDGER_DB_PATH = DATA_DIR / "trades.sqlite3"
LOG_FILE_PATH = LOG_DIR / "bot.log"

# Rotating file logs
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
LOG_BACKUP_COUNT = 14

# ---------------------------------------------------------------------------
# Safety — MUST be set False manually to place real orders
# ---------------------------------------------------------------------------
# DRY_RUN must be manually set to False to risk real funds.
DRY_RUN: bool = True

# ---------------------------------------------------------------------------
# Market identity
# ---------------------------------------------------------------------------
ASSET_SLUG_PREFIX = "btc-updown-5m"
MARKET_WINDOW_SECONDS = 300  # 5 minutes
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# ---------------------------------------------------------------------------
# Strategy guardrails (intentional defaults — ask before changing)
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 2.0
PRICE_HISTORY_SECONDS = 20.0
TRIGGER_PRICE = 0.80
CONFIRM_POLLS = 3  # consecutive polls at/above TRIGGER_PRICE required
ENTRY_CUTOFF_SECONDS = 10  # no new trades in the last N seconds of the window
MAX_ENTRY_PRICE = 0.95  # never pay more than this (best ask)
STAKE_USD = 1.0  # fixed notional per trade

# ---------------------------------------------------------------------------
# Risk controls
# ---------------------------------------------------------------------------
DAILY_LOSS_CAP_USD = 1.0  # stop new trades after this much realized UTC-day loss
# Optional cap on number of trades per UTC day. None / 0 = disabled.
DAILY_TRADE_COUNT_CAP: int | None = None

# ---------------------------------------------------------------------------
# Resolution & retry
# ---------------------------------------------------------------------------
RESOLUTION_POLL_SECONDS = 15.0
HEARTBEAT_INTERVAL_SECONDS = 180.0  # ~3 minutes
MARKET_FETCH_INITIAL_BACKOFF_SECONDS = 2.0
MARKET_FETCH_MAX_BACKOFF_SECONDS = 60.0
API_RETRY_ATTEMPTS = 5
API_RETRY_BASE_SECONDS = 0.5
API_RETRY_MAX_SECONDS = 30.0
API_REQUEST_TIMEOUT_SECONDS = 15.0

# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------
# Prefer fill-or-kill so we do not leave resting orders near resolution.
ORDER_TYPE = "FOK"
