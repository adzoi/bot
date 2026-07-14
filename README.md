# Polymarket BTC 5m Up/Down Momentum Bot

Production-oriented Python bot that watches Polymarket's recurring Bitcoin
"Up or Down - 5m" markets and places a small automated buy when one outcome's
midpoint stays elevated with confirmation — then tracks real resolution P&L.

**Default mode is dry-run.** Real orders are never sent until you manually set
`DRY_RUN = False` in `config.py`.

> **SDK note:** Polymarket CLOB V2 is live (April 2026). The legacy
> `py-clob-client` package no longer works against production. This project uses
> `py-clob-client-v2` against `https://clob.polymarket.com` (Polygon, chain 137).

## Strategy guardrails (do not weaken casually)

All tunables live in [`config.py`](config.py):

| Control | Default |
| --- | --- |
| Poll interval | 2s |
| Rolling price history | 20s |
| Trigger price | ≥ 0.80 |
| Confirmation | 3 consecutive polls |
| Entry cutoff | no trades in last 10s of window |
| Max entry (best ask) | 0.95 |
| Stake | $1.00 |
| One trade per window | enforced |
| Daily realized loss cap (UTC) | $1.00 |
| Daily trade count cap | disabled (`None`) |

Ask before changing these defaults — they are intentional.

## Project layout

```
config.py                 # all tunables
main.py                   # process entrypoint
polymarket_bot/
  market_discovery.py     # Gamma API slug → token IDs
  clob_service.py         # CLOB prices, book, orders
  strategy.py             # momentum + confirmation trigger
  risk.py                 # daily loss / trade caps
  ledger.py               # SQLite trade ledger + resolution PnL
  bot.py                  # 24/7 run loop, signals, heartbeat
  retries.py              # exponential backoff
  logging_setup.py        # rotating file + console logs
deploy/polymarket-bot.service
data/trades.sqlite3       # created at runtime (gitignored)
logs/bot.log              # rotating logs (gitignored)
```

## One-time setup

### 1. Python env

```bash
cd /opt/polymarket-bot   # or your clone path
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Wallet / funding (live trading only)

1. Create or use a Polygon wallet that will trade.
2. Fund it with collateral accepted by Polymarket CLOB V2 (**pUSD**, backed by USDC)
   plus a little POL/MATIC for gas if your flow needs it.
3. On [polymarket.com](https://polymarket.com), complete the usual enable-trading /
   allowance flow for your account type so the exchange can pull collateral.
4. Put secrets in `.env` only (never commit them):

```bash
PK=0x...
SIGNATURE_TYPE=0          # 0=EOA, 1=Magic/email, 2=browser proxy/Safe
FUNDER_ADDRESS=0x...      # required for types 1/2 — the address holding funds
```

Private keys and funder addresses are never logged.

### 3. Dry-run (default)

```bash
# Ensure config.py has: DRY_RUN = True
python main.py
```

You should see heartbeat lines every ~3 minutes and `DRY_RUN buy …` lines if a
trigger fires. No real orders are placed.

### 4. Go live

1. Confirm wallet funding + allowances.
2. In `config.py`, set **`DRY_RUN = False`** (this must be a conscious edit).
3. Restart the process / systemd unit.
4. Start with the default $1 stake and watch `logs/bot.log` + the ledger.

## Reading the trade ledger

SQLite DB path: `data/trades.sqlite3` (override via `config.LEDGER_DB_PATH`).

```bash
sqlite3 data/trades.sqlite3
```

Useful queries:

```sql
-- Recent trades
SELECT id, created_at, market_slug, outcome_bought, fill_price, stake, shares,
       resolution_outcome, realized_pnl, daily_pnl_after, dry_run, status
FROM trades
ORDER BY id DESC
LIMIT 20;

-- Today's realized PnL (UTC day)
SELECT COALESCE(SUM(realized_pnl), 0) AS daily_pnl
FROM trades
WHERE utc_day = (strftime('%Y-%m-%d', 'now'))
  AND realized_pnl IS NOT NULL;
```

Columns: timestamp, market question/slug, outcome bought, trigger price, fill
price, stake, shares, resolution outcome, realized P&L, running daily P&L
after resolution.

## Railway

This app must serve HTTP on Railway’s `$PORT` or the public URL returns
“Application failed to respond”. Use the bundled entrypoint:

```bash
python start.py   # starts the bot + dashboard on $PORT
```

`Procfile` / `railway.json` already set this as the start command. After
deploy, open your Railway URL (e.g. `https://….up.railway.app`) for the
live dashboard. Set secrets (`PK`, etc.) in the Railway Variables UI —
do not commit `.env`.

Note: the container filesystem is ephemeral unless you attach a Railway
volume to `/app/data` (or set `LEDGER_DB_PATH` to a mounted path).

## systemd (24/7)

1. Copy the project to `/opt/polymarket-bot` (or edit paths in the unit).
2. Create a dedicated user and install the unit:

```bash
sudo useradd --system --home /opt/polymarket-bot --shell /usr/sbin/nologin polymarket
sudo cp deploy/polymarket-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot
```

Useful commands:

```bash
sudo systemctl status polymarket-bot
journalctl -u polymarket-bot -f
# Application also writes rotating files:
tail -f /opt/polymarket-bot/logs/bot.log
```

The unit restarts on crash (`Restart=always`) and on reboot (`WantedBy=multi-user.target`).
SIGTERM triggers a clean shutdown.

## Operational notes

- **Windows:** after each 5m close, the bot fetches `btc-updown-5m-<unix_close_ts>`
  from Gamma and continues indefinitely. If the next market is not published yet,
  it retries with backoff instead of exiting.
- **Heartbeats:** look for `HEARTBEAT alive` in logs.
- **Daily loss cap:** when UTC-day realized PnL ≤ −$1, new entries stop until
  UTC midnight; resets are logged.
- **Min order size:** Polymarket books often require ≥ 5 shares. At a $1 stake and
  ~$0.80–$0.95 prices, sized shares may be below the exchange minimum — the bot
  skips and logs clearly. Raising `STAKE_USD` changes risk; ask before changing
  strategy defaults if you want a different policy.
- **API errors:** all network calls use retry + exponential backoff; transient
  failures are logged and the loop continues.

## Disclaimer

This software can lose money. Polymarket markets, APIs, and settlement rules can
change. You are solely responsible for keys, funds, compliance, and any live
trading. Defaults keep `DRY_RUN = True` on purpose.
