"""
Simple local dashboard for the Polymarket BTC 5m bot.

Drop this file into the same folder as config.py (your bot's root, e.g.
/Users/gstore/Desktop/bot/dashboard.py), then run:

    python dashboard.py

and open http://localhost:5050 in your browser.

It doesn't assume an exact ledger schema -- it introspects whatever table(s)
and columns exist in data/trades.sqlite3 (this project's default) and displays
them, plus computes best-effort summary stats (win rate, total P&L, today's P&L)
by pattern-matching common column names (realized_pnl, created_at/utc_day, etc).
If your ledger uses different names, tell me the schema and I'll tailor the
queries exactly.

Optional: if your bot writes a status.json heartbeat file, this also shows
live status (last heartbeat, dry-run/live). If that file doesn't exist, the
dashboard just skips that section.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

# ---------------------------------------------------------------------------
# Config -- adjust these two paths if your files live elsewhere
# ---------------------------------------------------------------------------
# Defaults match this project: data/trades.sqlite3 (override via env if needed)
_ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER_DB_PATH = os.environ.get(
    "LEDGER_DB_PATH",
    os.path.join(_ROOT, "data", "trades.sqlite3"),
)
STATUS_FILE_PATH = os.environ.get(
    "STATUS_FILE_PATH",
    os.path.join(_ROOT, "status.json"),
)
PORT = int(os.environ.get("DASHBOARD_PORT", "5050"))

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Ledger introspection
# ---------------------------------------------------------------------------

def get_ledger_data():
    if not os.path.isfile(LEDGER_DB_PATH):
        return {"error": f"No ledger found at {LEDGER_DB_PATH}", "rows": [], "columns": []}

    conn = sqlite3.connect(LEDGER_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Find the first user table (skip sqlite internals)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r["name"] for r in cur.fetchall()]
    if not tables:
        conn.close()
        return {"error": "Ledger DB has no tables yet.", "rows": [], "columns": []}

    # Prefer the known trades table when present
    table = "trades" if "trades" in tables else tables[0]
    cur.execute(f"PRAGMA table_info({table})")
    columns = [r["name"] for r in cur.fetchall()]

    cur.execute(f"SELECT * FROM {table} ORDER BY ROWID DESC LIMIT 500")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {"table": table, "columns": columns, "rows": rows, "error": None}


def find_column(columns, *candidates):
    """Case-insensitive best-effort match for a column name from a list of guesses."""
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    return None


def compute_summary(data):
    rows, columns = data.get("rows", []), data.get("columns", [])
    empty = {
        "total_trades": 0,
        "resolved_trades": 0,
        "win_rate": None,
        "total_pnl": None,
        "today_pnl": 0.0,
        "pnl_column_used": None,
        "timestamp_column_used": None,
        "resolution_column_used": None,
    }
    if not rows:
        return empty

    pnl_col = find_column(columns, "realized_pnl", "pnl", "profit", "realized_profit")
    ts_col = find_column(columns, "utc_day", "created_at", "timestamp", "time", "date")
    resolved_col = find_column(
        columns, "resolution_outcome", "resolution", "resolved_outcome", "status", "result"
    )

    total_trades = len(rows)
    total_pnl = None
    win_count = 0
    resolved_count = 0
    today_pnl = 0.0
    today_str = datetime.now(timezone.utc).date().isoformat()

    if pnl_col:
        pnl_values = []
        for r in rows:
            v = r.get(pnl_col)
            if v is None or v == "":
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            pnl_values.append(v)
            resolved_count += 1
            if v > 0:
                win_count += 1
            if ts_col and str(r.get(ts_col, "")).startswith(today_str):
                today_pnl += v
        if pnl_values:
            total_pnl = sum(pnl_values)

    return {
        "total_trades": total_trades,
        "resolved_trades": resolved_count,
        "win_rate": (round(100 * win_count / resolved_count, 1) if resolved_count else None),
        "total_pnl": (round(total_pnl, 4) if total_pnl is not None else None),
        "today_pnl": round(today_pnl, 4),
        "pnl_column_used": pnl_col,
        "timestamp_column_used": ts_col,
        "resolution_column_used": resolved_col,
    }


def get_status():
    if not os.path.isfile(STATUS_FILE_PATH):
        return None
    try:
        with open(STATUS_FILE_PATH) as f:
            return json.load(f)
    except Exception as exc:
        return {"error": f"Couldn't parse status file: {exc}"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Polymarket BTC Bot Dashboard</title>
  <meta http-equiv="refresh" content="15">
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #0f1115; color: #e6e6e6; margin: 0; padding: 24px; }
    h1 { font-size: 20px; margin-bottom: 4px; }
    .sub { color: #888; font-size: 13px; margin-bottom: 24px; }
    .cards { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
    .card { background: #1a1d24; border: 1px solid #2a2d35; border-radius: 10px; padding: 16px 20px; min-width: 140px; }
    .card .label { font-size: 12px; color: #999; text-transform: uppercase; letter-spacing: 0.05em; }
    .card .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
    .pos { color: #4ade80; } .neg { color: #f87171; } .neutral { color: #e6e6e6; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2d35; }
    th { color: #999; font-weight: 500; }
    .status-live { color: #4ade80; } .status-dry { color: #fbbf24; }
    .warn { color: #fbbf24; font-size: 13px; margin-bottom: 12px; }
  </style>
</head>
<body>
  <h1>Polymarket BTC 5m Bot</h1>
  <div class="sub">Auto-refreshes every 15s &middot; {{ now }}</div>

  {% if status %}
  <div class="cards">
    <div class="card">
      <div class="label">Mode</div>
      <div class="value {{ 'status-dry' if status.get('dry_run', True) else 'status-live' }}">
        {{ 'DRY RUN' if status.get('dry_run', True) else 'LIVE' }}
      </div>
    </div>
    <div class="card">
      <div class="label">Last heartbeat</div>
      <div class="value">{{ status.get('last_heartbeat', 'n/a') }}</div>
    </div>
  </div>
  {% else %}
  <div class="warn">No status.json found -- add the heartbeat snippet to bot.py to see live mode/heartbeat here.</div>
  {% endif %}

  <div class="cards">
    <div class="card">
      <div class="label">Total trades</div>
      <div class="value">{{ summary.total_trades }}</div>
    </div>
    <div class="card">
      <div class="label">Resolved</div>
      <div class="value">{{ summary.resolved_trades or 0 }}</div>
    </div>
    <div class="card">
      <div class="label">Win rate</div>
      <div class="value">{{ (summary.win_rate ~ '%') if summary.win_rate is not none else 'n/a' }}</div>
    </div>
    <div class="card">
      <div class="label">Total P&amp;L</div>
      <div class="value {{ 'pos' if (summary.total_pnl or 0) > 0 else ('neg' if (summary.total_pnl or 0) < 0 else 'neutral') }}">
        {{ '$%.2f' % summary.total_pnl if summary.total_pnl is not none else 'n/a' }}
      </div>
    </div>
    <div class="card">
      <div class="label">Today's P&amp;L</div>
      <div class="value {{ 'pos' if summary.today_pnl > 0 else ('neg' if summary.today_pnl < 0 else 'neutral') }}">
        ${{ '%.2f' % summary.today_pnl }}
      </div>
    </div>
  </div>

  {% if ledger.error %}
  <div class="warn">{{ ledger.error }}</div>
  {% else %}
  <table>
    <thead><tr>{% for c in ledger.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead>
    <tbody>
      {% for row in ledger.rows %}
      <tr>{% for c in ledger.columns %}<td>{{ row.get(c, '') }}</td>{% endfor %}</tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    ledger = get_ledger_data()
    summary = compute_summary(ledger)
    status = get_status()
    return render_template_string(
        PAGE,
        ledger=ledger,
        summary=summary,
        status=status,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.route("/api/summary")
def api_summary():
    ledger = get_ledger_data()
    return jsonify({"summary": compute_summary(ledger), "status": get_status()})


if __name__ == "__main__":
    print(f"Dashboard running at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
