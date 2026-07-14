"""
Central configuration for the BTC 5m Up/Down momentum bot.

All strategy thresholds and risk guardrails live here.
Do NOT weaken guardrails in code — change values here deliberately (or ask before changing defaults).

DRY_RUN defaults to True. You must manually set DRY_RUN = False to risk real funds.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths / process
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
LEDGER_DB_PATH = Path(
    os.environ.get("LEDGER_DB_PATH") or (DATA_DIR / "trades.sqlite3")
)
LOG_FILE_PATH = LOG_DIR / "bot.log"

# Rotating file logs
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
LOG_BACKUP_COUNT = 14

# ---------------------------------------------------------------------------
# Safety — defaults to dry-run; set env DRY_RUN=false to risk real funds
# ---------------------------------------------------------------------------
# DRY_RUN must be manually set to False (env or below) to risk real funds.
# Prefer Railway / .env: DRY_RUN=false — do not commit False to a public repo.
_dry = os.environ.get("DRY_RUN")
if _dry is None:
    DRY_RUN: bool = True
else:
    DRY_RUN = _dry.strip().lower() in {"1", "true", "yes", "on"}

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
# Heartbeat LOG lines fire on UTC clock marks :00,:05,:10,:15,... (same as market windows).
# status.json refreshes this often so the dashboard stays current.
STATUS_WRITE_SECONDS = 15.0
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
