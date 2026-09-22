"""Stock Trading Journal - single-file Flask app.

Tracks buy/sell trades with dynamic scaled entries and exits (multiple
"legs") for both long and short positions, records the exact date/time of
each leg, allows attaching a screenshot to each leg, tracks stop loss /
target / setup ("strategy") per trade, auto-calculates brokerage fees,
tracks deposits/withdrawals, and computes trading performance (win rate,
profit factor, expectancy, R-multiple, payoff ratio, streaks, monthly
performance, per-strategy breakdown) plus a portfolio growth curve,
drawdown, CAGR and a compounding-growth simulator. Data can be backed up
to / restored from JSON, exported to CSV, or synced to Google Sheets.
"""

import csv
import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, date

from flask import (
    Flask,
    g,
    redirect,
    render_template_string,
    request,
    send_file,
    send_from_directory,
    url_for,
)

try:
    import gspread
    from google.oauth2.service_account import Credentials as GoogleServiceCredentials

    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "trades.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_STRATEGIES = [
    "Breakout", "Pullback", "Reversal", "Harmonic Pattern",
    "TD Sequential", "Trend Following", "อื่นๆ",
]

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _safe_alter(db, sql):
    try:
        db.execute(sql)
    except sqlite3.OperationalError:
        pass


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('long', 'short')),
            strategy TEXT DEFAULT '',
            stop_loss REAL,
            target_price REAL,
            followed_plan TEXT DEFAULT 'unspecified',
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('open', 'close')),
            qty REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL DEFAULT 0,
            executed INTEGER DEFAULT 1,
            exec_time TEXT NOT NULL,
            note TEXT,
            image_filename TEXT,
            FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cash_flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_date TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('deposit', 'withdraw')),
            amount REAL NOT NULL,
            note TEXT
        );
        """
    )
    # Migrations for databases created by earlier versions of this app.
    _safe_alter(db, "ALTER TABLE orders ADD COLUMN strategy TEXT DEFAULT ''")
    _safe_alter(db, "ALTER TABLE orders ADD COLUMN stop_loss REAL")
    _safe_alter(db, "ALTER TABLE orders ADD COLUMN target_price REAL")
    _safe_alter(db, "ALTER TABLE orders ADD COLUMN followed_plan TEXT DEFAULT 'unspecified'")
    _safe_alter(db, "ALTER TABLE executions ADD COLUMN fee REAL DEFAULT 0")
    _safe_alter(db, "ALTER TABLE executions ADD COLUMN executed INTEGER DEFAULT 1")

    if db.execute("SELECT value FROM settings WHERE key = 'initial_capital'").fetchone() is None:
        db.execute("INSERT INTO settings (key, value) VALUES ('initial_capital', '100000')")
    if db.execute("SELECT value FROM settings WHERE key = 'fee_rate'").fetchone() is None:
        db.execute("INSERT INTO settings (key, value) VALUES ('fee_rate', '0.157')")
    if db.execute("SELECT value FROM settings WHERE key = 'strategies'").fetchone() is None:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('strategies', ?)",
            (json.dumps(DEFAULT_STRATEGIES, ensure_ascii=False),),
        )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def get_strategies():
    raw = get_setting("strategies")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    set_setting("strategies", json.dumps(DEFAULT_STRATEGIES, ensure_ascii=False))
    return list(DEFAULT_STRATEGIES)


def set_strategies(items):
    set_setting("strategies", json.dumps(items, ensure_ascii=False))


def get_fee_rate():
    try:
        return float(get_setting("fee_rate", "0.157") or 0)
    except ValueError:
        return 0.0


def get_initial_capital():
    try:
        return float(get_setting("initial_capital", "100000") or 0)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def fetch_orders():
    return get_db().execute("SELECT * FROM orders ORDER BY created_at DESC, id DESC").fetchall()


def fetch_order(order_id):
    return get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def fetch_executions(order_id):
    return get_db().execute(
        "SELECT * FROM executions WHERE order_id = ? ORDER BY exec_time ASC, id ASC",
        (order_id,),
    ).fetchall()


def fetch_cash_flows():
    return get_db().execute(
        "SELECT * FROM cash_flows ORDER BY flow_date ASC, id ASC"
    ).fetchall()


# ---------------------------------------------------------------------------
# Domain calculations
# ---------------------------------------------------------------------------

def compute_order_stats(order, executions):
    """Weighted-average cost model, using only executed ('done') legs.

    Legs left unticked ("ทำแล้ว") are treated as a plan only and excluded
    from cost / P&L math, but are still stored and shown for editing.
    """
    opens = [e for e in executions if e["action"] == "open" and e["executed"]]
    closes = [e for e in executions if e["action"] == "close" and e["executed"]]

    opened_qty = sum(e["qty"] for e in opens)
    closed_qty = sum(e["qty"] for e in closes)
    avg_open = (sum(e["qty"] * e["price"] for e in opens) / opened_qty) if opened_qty else 0.0
    avg_close = (sum(e["qty"] * e["price"] for e in closes) / closed_qty) if closed_qty else 0.0

    sign = 1 if order["direction"] == "long" else -1
    raw_pnl = sum(sign * e["qty"] * (e["price"] - avg_open) for e in closes)

    open_fees = sum(e["fee"] or 0 for e in opens)
    close_fees = sum(e["fee"] or 0 for e in closes)
    open_fee_alloc = (open_fees * (closed_qty / opened_qty)) if opened_qty else 0.0
    total_fees = open_fee_alloc + close_fees
    realized_pnl = raw_pnl - total_fees

    remaining_qty = round(opened_qty - closed_qty, 6)
    if opened_qty <= 0:
        status = "planned"
    elif remaining_qty <= 0:
        status = "closed"
    else:
        status = "open"

    r_multiple = None
    if order["stop_loss"] and closed_qty > 0:
        risk_per_share = abs(avg_open - order["stop_loss"])
        if risk_per_share > 0:
            r_multiple = realized_pnl / (risk_per_share * closed_qty)

    first_open_time = opens[0]["exec_time"] if opens else None
    last_close_time = closes[-1]["exec_time"] if closes else None
    holding_days = None
    if first_open_time and last_close_time and status == "closed":
        try:
            d1 = datetime.strptime(first_open_time[:10], "%Y-%m-%d")
            d2 = datetime.strptime(last_close_time[:10], "%Y-%m-%d")
            holding_days = (d2 - d1).days
        except ValueError:
            holding_days = None

    roi_pct = (realized_pnl / (avg_open * closed_qty) * 100) if (avg_open and closed_qty) else None

    return {
        "opened_qty": opened_qty,
        "closed_qty": closed_qty,
        "remaining_qty": remaining_qty,
        "avg_open": avg_open,
        "avg_close": avg_close,
        "realized_pnl": realized_pnl,
        "total_fees": total_fees,
        "status": status,
        "r_multiple": r_multiple,
        "first_open_time": first_open_time,
        "last_close_time": last_close_time,
        "holding_days": holding_days,
        "roi_pct": roi_pct,
    }


def compute_dashboard():
    """Aggregate everything the Overview and Portfolio pages need."""
    orders = fetch_orders()
    cash_flows = fetch_cash_flows()
    initial_capital = get_initial_capital()

    net_deposits = sum(
        cf["amount"] if cf["type"] == "deposit" else -cf["amount"] for cf in cash_flows
    )
    capital_base = initial_capital + net_deposits

    order_stats = {}
    closed_orders = []
    events = []  # (date_str, 'cash'|'pnl', value)

    for cf in cash_flows:
        events.append((cf["flow_date"], "cash", cf["amount"] if cf["type"] == "deposit" else -cf["amount"]))

    for order in orders:
        executions = fetch_executions(order["id"])
        stats = compute_order_stats(order, executions)
        order_stats[order["id"]] = stats
        if stats["status"] == "closed":
            closed_orders.append((order, stats))
            events.append((stats["last_close_time"], "pnl", stats["realized_pnl"]))

    events.sort(key=lambda e: e[0])

    equity_curve = [{"time": "start", "equity": round(initial_capital, 2)}]
    running = initial_capital
    peak = initial_capital
    max_drawdown = 0.0
    for t, kind, val in events:
        running += val
        peak = max(peak, running)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - running) / peak * 100)
        equity_curve.append({"time": t, "equity": round(running, 2)})

    current_value = running
    total_realized = sum(s["realized_pnl"] for _, s in closed_orders)
    trade_count = len(closed_orders)
    wins = [s["realized_pnl"] for _, s in closed_orders if s["realized_pnl"] > 0]
    losses = [s["realized_pnl"] for _, s in closed_orders if s["realized_pnl"] < 0]
    win_rate = (len(wins) / trade_count * 100) if trade_count else None
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else None)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = (total_realized / trade_count) if trade_count else None
    payoff_ratio = (avg_win / abs(avg_loss)) if avg_loss else None

    r_values = [s["r_multiple"] for _, s in closed_orders if s["r_multiple"] is not None]
    avg_r = (sum(r_values) / len(r_values)) if r_values else None

    best_trade = max(closed_orders, key=lambda os_: os_[1]["realized_pnl"], default=None)
    worst_trade = min(closed_orders, key=lambda os_: os_[1]["realized_pnl"], default=None)

    ordered_by_close = sorted(closed_orders, key=lambda os_: os_[1]["last_close_time"] or "")
    best_streak = streak = 0
    for _, s in ordered_by_close:
        if s["realized_pnl"] > 0:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0

    holding = [s["holding_days"] for _, s in closed_orders if s["holding_days"] is not None]
    avg_holding_days = (sum(holding) / len(holding)) if holding else None

    monthly = {}
    for order, s in closed_orders:
        month = (s["last_close_time"] or "")[:7]
        bucket = monthly.setdefault(month, {"count": 0, "wins": 0, "pnl": 0.0})
        bucket["count"] += 1
        bucket["pnl"] += s["realized_pnl"]
        if s["realized_pnl"] > 0:
            bucket["wins"] += 1
    monthly_rows = []
    for month in sorted(monthly.keys()):
        b = monthly[month]
        monthly_rows.append(
            {
                "month": month,
                "count": b["count"],
                "win_pct": (b["wins"] / b["count"] * 100) if b["count"] else 0,
                "pnl": b["pnl"],
                "pct_of_portfolio": (b["pnl"] / capital_base * 100) if capital_base else 0,
            }
        )

    strategy_stats = {}
    for order, s in closed_orders:
        key = order["strategy"] or "ไม่ระบุ"
        bucket = strategy_stats.setdefault(key, {"count": 0, "wins": 0, "pnl": 0.0})
        bucket["count"] += 1
        bucket["pnl"] += s["realized_pnl"]
        if s["realized_pnl"] > 0:
            bucket["wins"] += 1
    strategy_rows = []
    for name, b in sorted(strategy_stats.items(), key=lambda kv: -kv[1]["pnl"]):
        strategy_rows.append(
            {
                "name": name,
                "count": b["count"],
                "win_pct": (b["wins"] / b["count"] * 100) if b["count"] else 0,
                "pnl": b["pnl"],
            }
        )

    open_orders = [(o, order_stats[o["id"]]) for o in orders if order_stats[o["id"]]["status"] == "open"]
    open_cost_value = sum(s["remaining_qty"] * s["avg_open"] for _, s in open_orders)
    invested_ratio = (open_cost_value / current_value * 100) if current_value else 0.0

    cagr = None
    dated_events = [e for e in events if e[0] and e[0] != "start"]
    if dated_events:
        try:
            first_date = datetime.strptime(dated_events[0][0][:10], "%Y-%m-%d").date()
            span_days = (date.today() - first_date).days
            if span_days >= 30 and capital_base > 0 and current_value > 0:
                years = span_days / 365.0
                cagr = ((current_value / capital_base) ** (1 / years) - 1) * 100
        except ValueError:
            pass

    total_return_pct = (total_realized / capital_base * 100) if capital_base else 0.0

    return {
        "orders": orders,
        "order_stats": order_stats,
        "initial_capital": initial_capital,
        "net_deposits": net_deposits,
        "capital_base": capital_base,
        "current_value": current_value,
        "total_return_pct": total_return_pct,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "open_position_count": len(open_orders),
        "open_cost_value": open_cost_value,
        "invested_ratio": invested_ratio,
        "equity_curve": equity_curve,
        "total_realized": total_realized,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "payoff_ratio": payoff_ratio,
        "avg_r": avg_r,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "best_streak": best_streak,
        "avg_holding_days": avg_holding_days,
        "monthly_rows": monthly_rows,
        "strategy_rows": strategy_rows,
        "cash_flows": cash_flows,
    }


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def save_image(file_storage):
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        return None
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    fname = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, fname))
    return fname


# ---------------------------------------------------------------------------
# Google Sheets backup
# ---------------------------------------------------------------------------

GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def get_gspread_client():
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("ยังไม่ได้ติดตั้งไลบรารีที่จำเป็น กรุณารัน: pip install gspread google-auth")
    creds_raw = get_setting("google_credentials_json", "")
    if not creds_raw:
        raise RuntimeError("ยังไม่ได้ตั้งค่า Google Service Account credentials ในหน้า Settings")
    try:
        info = json.loads(creds_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Credentials JSON ไม่ถูกต้อง: {exc}") from exc
    credentials = GoogleServiceCredentials.from_service_account_info(info, scopes=GOOGLE_SHEETS_SCOPES)
    return gspread.authorize(credentials)


def _write_sheet(spreadsheet, title, rows):
    try:
        ws = spreadsheet.worksheet(title)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=max(len(rows) + 10, 100), cols=26)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def backup_to_google_sheets():
    client = get_gspread_client()

    spreadsheet_id = (get_setting("google_spreadsheet_id", "") or "").strip()
    if spreadsheet_id:
        try:
            sh = client.open_by_key(spreadsheet_id)
        except gspread.exceptions.APIError as exc:
            raise RuntimeError(f"เปิด Spreadsheet ไม่สำเร็จ: {exc}") from exc
    else:
        sh = client.create("Trading Journal Backup")
        set_setting("google_spreadsheet_id", sh.id)
        share_email = (get_setting("google_share_email", "") or "").strip()
        if share_email:
            sh.share(share_email, perm_type="user", role="writer")

    orders = fetch_orders()
    order_rows = [
        [
            "ID", "Symbol", "Direction", "Strategy", "Stop Loss", "Target", "Followed Plan",
            "Note", "Created At", "Status", "Avg Open", "Avg Close", "Opened Qty",
            "Closed Qty", "Remaining Qty", "Fees", "Realized P/L", "R Multiple",
        ]
    ]
    exec_rows = [
        ["Order ID", "Symbol", "Action", "Qty", "Price", "Fee", "Executed", "Exec Time", "Note", "Image Filename"]
    ]
    for o in orders:
        executions = fetch_executions(o["id"])
        stats = compute_order_stats(o, executions)
        order_rows.append(
            [
                o["id"], o["symbol"], o["direction"], o["strategy"] or "", o["stop_loss"] or "",
                o["target_price"] or "", o["followed_plan"] or "", o["note"] or "", o["created_at"],
                stats["status"], round(stats["avg_open"], 4), round(stats["avg_close"], 4),
                stats["opened_qty"], stats["closed_qty"], stats["remaining_qty"],
                round(stats["total_fees"], 2), round(stats["realized_pnl"], 2),
                round(stats["r_multiple"], 2) if stats["r_multiple"] is not None else "",
            ]
        )
        for e in executions:
            exec_rows.append(
                [
                    o["id"], o["symbol"], e["action"], e["qty"], e["price"], e["fee"] or 0,
                    "yes" if e["executed"] else "no", e["exec_time"], e["note"] or "",
                    e["image_filename"] or "",
                ]
            )

    cash_rows = [["Date", "Type", "Amount", "Note"]]
    for cf in fetch_cash_flows():
        cash_rows.append([cf["flow_date"], cf["type"], cf["amount"], cf["note"] or ""])

    d = compute_dashboard()
    summary_rows = [
        ["Metric", "Value"],
        ["Initial Capital", d["initial_capital"]],
        ["Net Deposits", round(d["net_deposits"], 2)],
        ["Capital Base", round(d["capital_base"], 2)],
        ["Current Value", round(d["current_value"], 2)],
        ["Total Return %", round(d["total_return_pct"], 2)],
        ["CAGR %", round(d["cagr"], 2) if d["cagr"] is not None else ""],
        ["Max Drawdown %", round(d["max_drawdown"], 2)],
        ["Total Realized P/L", round(d["total_realized"], 2)],
        ["Win Rate %", round(d["win_rate"], 2) if d["win_rate"] is not None else ""],
        ["Profit Factor", d["profit_factor"] if d["profit_factor"] not in (None, float("inf")) else ("inf" if d["profit_factor"] == float("inf") else "")],
        ["Expectancy", round(d["expectancy"], 2) if d["expectancy"] is not None else ""],
        ["Payoff Ratio", round(d["payoff_ratio"], 2) if d["payoff_ratio"] is not None else ""],
        ["Avg R", round(d["avg_r"], 2) if d["avg_r"] is not None else ""],
        ["Backup Time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]

    _write_sheet(sh, "Orders", order_rows)
    _write_sheet(sh, "Executions", exec_rows)
    _write_sheet(sh, "CashFlows", cash_rows)
    _write_sheet(sh, "Summary", summary_rows)

    url = f"https://docs.google.com/spreadsheets/d/{sh.id}"
    set_setting("google_spreadsheet_url", url)
    set_setting("google_last_backup", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return url


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BASE_HEAD = """
<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title|default('Trading Journal') }}</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
<nav class="bg-slate-900 border-b border-slate-800 px-6 py-4 flex flex-wrap items-center justify-between gap-3">
  <a href="{{ url_for('overview') }}" class="text-xl font-bold text-emerald-400">📈 สมุดบันทึกเทรดหุ้น</a>
  <div class="flex gap-2 text-sm">
    <a href="{{ url_for('overview') }}" class="px-3 py-1.5 rounded-lg {{ 'bg-emerald-600 text-white' if active=='overview' else 'hover:bg-slate-800' }}">ภาพรวม</a>
    <a href="{{ url_for('trade_page') }}" class="px-3 py-1.5 rounded-lg {{ 'bg-emerald-600 text-white' if active=='trade' else 'hover:bg-slate-800' }}">บันทึกเทรด</a>
    <a href="{{ url_for('portfolio_page') }}" class="px-3 py-1.5 rounded-lg {{ 'bg-emerald-600 text-white' if active=='portfolio' else 'hover:bg-slate-800' }}">พอร์ต</a>
    <a href="{{ url_for('settings_page') }}" class="px-3 py-1.5 rounded-lg {{ 'bg-emerald-600 text-white' if active=='settings' else 'hover:bg-slate-800' }}">ตั้งค่า</a>
  </div>
</nav>
<main class="max-w-6xl mx-auto p-6">
{% with messages = get_flashed() %}
{% if messages %}
<div class="mb-4 space-y-2">
{% for m in messages %}
<div class="bg-emerald-900/40 border border-emerald-700 text-emerald-200 px-4 py-2 rounded">{{ m }}</div>
{% endfor %}
</div>
{% endif %}
{% endwith %}
"""

BASE_TAIL = """
</main>
</body>
</html>
"""


def stat_card(label, value_expr, extra_class=""):
    return (
        '<div class="bg-slate-900 rounded-xl p-4 border border-slate-800">'
        f'<div class="text-xs text-slate-400">{label}</div>'
        f'<div class="text-lg font-semibold {extra_class}">{value_expr}</div>'
        "</div>"
    )


OVERVIEW_TEMPLATE = BASE_HEAD + """
<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">มูลค่าพอร์ตปัจจุบัน</div>
    <div class="text-lg font-semibold">฿{{ '{:,.2f}'.format(d.current_value) }}</div>
    <div class="text-xs {{ 'text-emerald-400' if d.total_return_pct >= 0 else 'text-rose-400' }}">{{ '{:+.2f}'.format(d.total_return_pct) }}% ผลตอบแทนรวม</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ทุนสะสม</div>
    <div class="text-lg font-semibold">฿{{ '{:,.2f}'.format(d.capital_base) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">กำไรสุทธิ (ที่รับรู้แล้ว)</div>
    <div class="text-lg font-semibold {{ 'text-emerald-400' if d.total_realized >= 0 else 'text-rose-400' }}">{{ '{:,.2f}'.format(d.total_realized) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Max Drawdown</div>
    <div class="text-lg font-semibold text-rose-400">{{ '{:.2f}'.format(d.max_drawdown) }}%</div>
  </div>
</div>

<div class="bg-slate-900 rounded-xl p-4 border border-slate-800 mb-8">
  <h2 class="text-sm text-slate-400 mb-2">กราฟการเติบโตของพอร์ต (Equity Curve)</h2>
  <canvas id="equityChart" height="90"></canvas>
</div>

<div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 mb-2">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">จำนวนเทรด (ปิดครบแล้ว)</div>
    <div class="text-lg font-semibold">{{ d.trade_count }}</div>
    <div class="text-xs text-slate-500">ชนะ {{ d.win_rate is not none and (d.win_rate/100*d.trade_count)|round|int or 0 }} · แพ้ {{ d.trade_count - ((d.win_rate/100*d.trade_count)|round|int if d.win_rate is not none else 0) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Win Rate</div>
    <div class="text-lg font-semibold">{{ '{:.1f}%'.format(d.win_rate) if d.win_rate is not none else '–' }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Profit Factor</div>
    <div class="text-lg font-semibold">{{ '%.2f'|format(d.profit_factor) if d.profit_factor not in (None,) and d.profit_factor != float('inf') else ('∞' if d.profit_factor == float('inf') else '–') }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Expectancy / เทรด</div>
    <div class="text-lg font-semibold {{ 'text-emerald-400' if d.expectancy and d.expectancy>=0 else 'text-rose-400' if d.expectancy else '' }}">{{ '{:,.2f}'.format(d.expectancy) if d.expectancy is not none else '–' }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">กำไรเฉลี่ย</div>
    <div class="text-lg font-semibold text-emerald-400">{{ '{:,.2f}'.format(d.avg_win) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ขาดทุนเฉลี่ย</div>
    <div class="text-lg font-semibold text-rose-400">{{ '{:,.2f}'.format(d.avg_loss) }}</div>
  </div>
</div>

<div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4 mb-8">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Payoff Ratio</div>
    <div class="text-lg font-semibold">{{ '%.2f'|format(d.payoff_ratio) if d.payoff_ratio is not none else '–' }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">R เฉลี่ย</div>
    <div class="text-lg font-semibold">{{ '%.2fR'|format(d.avg_r) if d.avg_r is not none else '–' }}</div>
    <div class="text-xs text-slate-500">ต้องกรอก Stop Loss</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">เทรดที่ดีที่สุด</div>
    <div class="text-lg font-semibold text-emerald-400">{{ d.best_trade[0]['symbol'] + ' +{:,.0f}'.format(d.best_trade[1]['realized_pnl']) if d.best_trade else '–' }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">เทรดที่แย่ที่สุด</div>
    <div class="text-lg font-semibold text-rose-400">{{ d.worst_trade[0]['symbol'] + ' {:,.0f}'.format(d.worst_trade[1]['realized_pnl']) if d.worst_trade else '–' }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ชนะติดต่อกันสูงสุด</div>
    <div class="text-lg font-semibold">{{ d.best_streak }}</div>
  </div>
</div>

<div class="grid md:grid-cols-2 gap-6 mb-8">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <h2 class="font-semibold mb-3">ประสิทธิภาพแยกตามหน้าเทรด</h2>
    {% if d.strategy_rows %}
    <table class="w-full text-sm">
      <thead class="text-slate-400 text-left"><tr><th class="py-1">หน้าเทรด</th><th class="text-right">เทรด</th><th class="text-right">Win%</th><th class="text-right">กำไรสุทธิ</th></tr></thead>
      <tbody>
      {% for r in d.strategy_rows %}
      <tr class="border-t border-slate-800">
        <td class="py-1.5">{{ r.name }}</td>
        <td class="text-right">{{ r.count }}</td>
        <td class="text-right">{{ '{:.0f}%'.format(r.win_pct) }}</td>
        <td class="text-right {{ 'text-emerald-400' if r.pnl>=0 else 'text-rose-400' }}">{{ '{:,.0f}'.format(r.pnl) }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="text-slate-500 text-center py-6">ยังไม่มีเทรดที่ปิดครบแล้ว</p>
    {% endif %}
  </div>

  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <h2 class="font-semibold mb-3">ผลรายเดือน</h2>
    {% if d.monthly_rows %}
    <table class="w-full text-sm">
      <thead class="text-slate-400 text-left"><tr><th class="py-1">เดือน</th><th class="text-right">ปิดเทรด</th><th class="text-right">Win%</th><th class="text-right">กำไรสุทธิ</th><th class="text-right">% พอร์ต</th></tr></thead>
      <tbody>
      {% for r in d.monthly_rows %}
      <tr class="border-t border-slate-800">
        <td class="py-1.5">{{ r.month }}</td>
        <td class="text-right">{{ r.count }}</td>
        <td class="text-right">{{ '{:.0f}%'.format(r.win_pct) }}</td>
        <td class="text-right {{ 'text-emerald-400' if r.pnl>=0 else 'text-rose-400' }}">{{ '{:,.0f}'.format(r.pnl) }}</td>
        <td class="text-right">{{ '{:+.2f}%'.format(r.pct_of_portfolio) }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="text-slate-500 text-center py-6">ยังไม่มีเทรดที่ปิดครบแล้ว</p>
    {% endif %}
  </div>
</div>

<div class="flex items-center justify-between mb-4">
  <h2 class="text-lg font-semibold">เทรดล่าสุด</h2>
  <a href="{{ url_for('trade_page') }}" class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg text-sm font-medium">+ บันทึกเทรดใหม่</a>
</div>
<div class="overflow-x-auto bg-slate-900 rounded-xl border border-slate-800">
<table class="w-full text-sm">
  <thead class="text-slate-400 border-b border-slate-800">
    <tr>
      <th class="text-left p-3">หุ้น</th><th class="text-left p-3">ฝั่ง</th>
      <th class="text-right p-3">กำไรสุทธิ</th><th class="text-left p-3">สถานะ</th><th class="p-3"></th>
    </tr>
  </thead>
  <tbody>
  {% for o in d.orders[:8] %}
  {% set s = d.order_stats[o['id']] %}
  <tr class="border-b border-slate-800/60 hover:bg-slate-800/40">
    <td class="p-3 font-semibold">{{ o['symbol'] }}</td>
    <td class="p-3"><span class="px-2 py-0.5 rounded text-xs {{ 'bg-blue-900 text-blue-300' if o['direction']=='long' else 'bg-orange-900 text-orange-300' }}">{{ 'Long' if o['direction']=='long' else 'Short' }}</span></td>
    <td class="p-3 text-right {{ 'text-emerald-400' if s.realized_pnl>=0 else 'text-rose-400' }}">{{ '{:,.2f}'.format(s.realized_pnl) }}</td>
    <td class="p-3"><span class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300">{{ {'open':'เปิดอยู่','closed':'ปิดแล้ว','planned':'วางแผน'}[s.status] }}</span></td>
    <td class="p-3 text-right"><a href="{{ url_for('trade_page', edit=o['id']) }}" class="text-emerald-400 hover:underline">แก้ไข →</a></td>
  </tr>
  {% else %}
  <tr><td colspan="5" class="p-6 text-center text-slate-500">ยังไม่มีเทรด</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>

<script>
const curve = {{ equity_json|safe }};
new Chart(document.getElementById('equityChart'), {
  type: 'line',
  data: { labels: curve.map(p => p.time), datasets: [{
    label: 'Equity', data: curve.map(p => p.equity),
    borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.15)',
    fill: true, tension: 0.25, pointRadius: 2,
  }] },
  options: {
    scales: { x: { ticks: { color: '#94a3b8', maxRotation: 0, autoSkip: true } }, y: { ticks: { color: '#94a3b8' } } },
    plugins: { legend: { labels: { color: '#e2e8f0' } } }
  }
});
</script>
""" + BASE_TAIL


LEG_ROW_JS = """
function pad2(n) { return String(n).padStart(2, '0'); }
function hourOptions(selected) {
  let out = '';
  for (let h = 0; h < 24; h++) {
    const v = pad2(h);
    out += `<option value="${v}" ${v === selected ? 'selected' : ''}>${v}</option>`;
  }
  return out;
}
function minuteOptions(selected) {
  let out = '';
  for (let m = 0; m < 60; m++) {
    const v = pad2(m);
    out += `<option value="${v}" ${v === selected ? 'selected' : ''}>${v}</option>`;
  }
  return out;
}
function legRow(side, data) {
  data = data || {};
  const row = document.createElement('div');
  row.className = 'flex flex-wrap items-center gap-2 bg-slate-800/60 rounded-lg p-2 leg-row';
  const checkedAttr = data.executed === false ? '' : 'checked';
  const hiddenVal = data.executed === false ? '0' : '1';
  const timeParts = (data.time || nowStr()).split(':');
  const hour = timeParts[0] || '00';
  const minute = timeParts[1] || '00';
  row.innerHTML = `
    <input type="hidden" name="${side}_row_id[]" class="row-id" value="${data.id || ''}">
    <input type="date" name="${side}_date[]" required value="${data.date || todayStr()}" class="w-32 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm date-input">
    <select name="${side}_hour[]" class="w-16 bg-slate-800 border border-slate-700 rounded px-1 py-1.5 text-sm hour-select">${hourOptions(hour)}</select>
    <span class="text-slate-500">:</span>
    <select name="${side}_minute[]" class="w-16 bg-slate-800 border border-slate-700 rounded px-1 py-1.5 text-sm minute-select">${minuteOptions(minute)}</select>
    <button type="button" class="now-btn text-xs bg-slate-700 hover:bg-slate-600 px-2 py-1.5 rounded" title="ใส่วันเวลาปัจจุบัน">ตอนนี้</button>
    <input type="number" step="any" name="${side}_qty[]" required placeholder="จำนวน" value="${data.qty || ''}" list="qty-presets" class="w-24 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm qty-input">
    <input type="number" step="any" name="${side}_price[]" required placeholder="ราคา" value="${data.price || ''}" class="w-24 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
    <input type="number" step="any" name="${side}_fee[]" placeholder="อัตโนมัติ" value="${data.fee || ''}" class="w-20 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
    <label class="flex items-center gap-1 text-xs text-slate-400 whitespace-nowrap">
      <input type="checkbox" ${checkedAttr} class="done-check" onchange="this.parentElement.querySelector('.done-hidden').value = this.checked ? '1' : '0'">
      ทำแล้ว
      <input type="hidden" name="${side}_done[]" class="done-hidden" value="${hiddenVal}">
    </label>
    <button type="button" class="text-rose-400 hover:text-rose-300 text-sm remove-row">ลบ</button>
    <input type="text" name="${side}_note[]" placeholder="หมายเหตุ" value="${data.note || ''}" class="flex-1 min-w-[10rem] bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
    <div class="flex items-center gap-2 w-full">
      <input type="file" name="${side}_image[]" accept="image/*" class="text-xs flex-1">
      ${data.image ? `<a href="/uploads/${data.image}" target="_blank" class="text-xs text-emerald-400 hover:underline">ภาพเดิม</a>` : ''}
    </div>
  `;
  row.querySelector('.remove-row').addEventListener('click', () => row.remove());
  row.querySelector('.now-btn').addEventListener('click', () => {
    row.querySelector('.date-input').value = todayStr();
    const now = nowStr().split(':');
    row.querySelector('.hour-select').value = now[0];
    row.querySelector('.minute-select').value = now[1];
  });
  return row;
}
function todayStr() { return new Date().toISOString().slice(0,10); }
function nowStr() { return new Date().toTimeString().slice(0,5); }
function addRow(side, data) {
  document.getElementById(side + '-rows').appendChild(legRow(side, data));
}
function splitEvenly(side) {
  const container = document.getElementById(side + '-rows');
  const inputs = [...container.querySelectorAll('.qty-input')];
  if (!inputs.length) return;
  const total = inputs.reduce((sum, i) => sum + (parseFloat(i.value) || 0), 0);
  if (!total) { alert('กรอกจำนวนหุ้นอย่างน้อย 1 ไม้ก่อน'); return; }
  const each = Math.round((total / inputs.length) * 100) / 100;
  inputs.forEach(i => i.value = each);
}
"""

TRADE_TEMPLATE = BASE_HEAD + """
<h1 class="text-xl font-bold mb-4">{{ 'แก้ไขเทรด' if edit_trade else 'เพิ่มรายการเทรด' }}</h1>
<form method="post" action="{{ url_for('trade_save') }}" enctype="multipart/form-data" class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4 mb-8">
  <input type="hidden" name="trade_id" value="{{ edit_trade['id'] if edit_trade else '' }}">
  <datalist id="qty-presets">
    <option value="100"><option value="200"><option value="300"><option value="500">
    <option value="1000"><option value="2000"><option value="3000"><option value="5000"><option value="10000">
  </datalist>
  <div class="grid grid-cols-2 md:grid-cols-6 gap-4">
    <div class="md:col-span-2">
      <label class="block text-sm text-slate-400 mb-1">ชื่อหุ้น</label>
      <input name="symbol" required placeholder="เช่น IVL" value="{{ edit_trade['symbol'] if edit_trade else '' }}" list="symbol-list" oninput="this.value = this.value.toUpperCase()" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 uppercase">
      <datalist id="symbol-list">
        {% for sym in known_symbols %}<option value="{{ sym }}">{% endfor %}
      </datalist>
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">ฝั่ง</label>
      <select name="direction" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
        <option value="long" {{ 'selected' if edit_trade and edit_trade['direction']=='long' else '' }}>Long (ซื้อก่อนขาย)</option>
        <option value="short" {{ 'selected' if edit_trade and edit_trade['direction']=='short' else '' }}>Short (ขายก่อนซื้อคืน)</option>
      </select>
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">หน้าเทรด</label>
      <select name="strategy" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
        <option value="">ไม่ระบุ</option>
        {% for s in strategies %}
        <option value="{{ s }}" {{ 'selected' if edit_trade and edit_trade['strategy']==s else '' }}>{{ s }}</option>
        {% endfor %}
      </select>
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">ราคา Stop Loss</label>
      <input type="number" step="any" name="stop_loss" value="{{ edit_trade['stop_loss'] if edit_trade and edit_trade['stop_loss'] else '' }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">ราคาเป้าหมาย</label>
      <input type="number" step="any" name="target_price" value="{{ edit_trade['target_price'] if edit_trade and edit_trade['target_price'] else '' }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
  </div>
  <div class="max-w-xs">
    <label class="block text-sm text-slate-400 mb-1">ทำตามแผนหรือไม่</label>
    <select name="followed_plan" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <option value="unspecified" {{ 'selected' if edit_trade and edit_trade['followed_plan']=='unspecified' else '' }}>ไม่ระบุ</option>
      <option value="yes" {{ 'selected' if edit_trade and edit_trade['followed_plan']=='yes' else '' }}>ทำตามแผน</option>
      <option value="no" {{ 'selected' if edit_trade and edit_trade['followed_plan']=='no' else '' }}>ไม่ทำตามแผน</option>
    </select>
  </div>

  <div class="grid md:grid-cols-2 gap-4">
    <div class="border border-emerald-800/60 rounded-xl p-3">
      <div class="flex items-center justify-between mb-2">
        <h2 class="font-semibold text-emerald-400">ไม้เข้า (ซื้อ)</h2>
        <div class="flex gap-2">
          <button type="button" onclick="addRow('open')" class="text-xs bg-slate-800 hover:bg-slate-700 px-2 py-1 rounded">+ เพิ่มไม้</button>
          <button type="button" onclick="splitEvenly('open')" class="text-xs bg-slate-800 hover:bg-slate-700 px-2 py-1 rounded">แบ่งจำนวนเท่าๆกัน</button>
        </div>
      </div>
      <div id="open-rows" class="space-y-2"></div>
    </div>
    <div class="border border-rose-800/60 rounded-xl p-3">
      <div class="flex items-center justify-between mb-2">
        <h2 class="font-semibold text-rose-400">ไม้ออก (ขาย)</h2>
        <div class="flex gap-2">
          <button type="button" onclick="addRow('close')" class="text-xs bg-slate-800 hover:bg-slate-700 px-2 py-1 rounded">+ เพิ่มไม้</button>
          <button type="button" onclick="splitEvenly('close')" class="text-xs bg-slate-800 hover:bg-slate-700 px-2 py-1 rounded">แบ่งจำนวนเท่าๆกัน</button>
        </div>
      </div>
      <div id="close-rows" class="space-y-2"></div>
    </div>
  </div>
  <p class="text-xs text-slate-500">ติ๊ก "ทำแล้ว" เมื่อออเดอร์จับคู่จริง ไม้ที่ไม่ได้ติ๊กจะเป็นแค่ "แผน" ยังไม่นำไปคำนวณกำไรหรือพอร์ต · ค่าธรรมเนียมเว้นว่างเพื่อคำนวณอัตโนมัติจากอัตราที่ตั้งไว้</p>

  <div>
    <label class="block text-sm text-slate-400 mb-1">บันทึกเพิ่มเติม</label>
    <textarea name="note" rows="2" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">{{ edit_trade['note'] if edit_trade else '' }}</textarea>
  </div>

  <div class="flex gap-3">
    <button class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg font-medium">บันทึกรายการ</button>
    <a href="{{ url_for('trade_page') }}" class="bg-slate-800 hover:bg-slate-700 px-4 py-2 rounded-lg font-medium">ล้างฟอร์ม</a>
  </div>
</form>

<h2 class="font-semibold mb-3">รายการเทรดทั้งหมด</h2>
<div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
  <input id="f-search" placeholder="ค้นหาชื่อหุ้น" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm">
  <select id="f-strategy" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <option value="">ทุกหน้าเทรด</option>
    {% for s in strategies %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
  </select>
  <select id="f-status" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <option value="">ทุกสถานะ</option>
    <option value="open">เปิดอยู่</option>
    <option value="closed">ปิดแล้ว</option>
    <option value="planned">วางแผน</option>
  </select>
  <select id="f-result" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <option value="">ทุกผล</option>
    <option value="win">กำไร</option>
    <option value="loss">ขาดทุน</option>
  </select>
</div>
<div class="overflow-x-auto bg-slate-900 rounded-xl border border-slate-800">
<table class="w-full text-sm">
  <thead class="text-slate-400 border-b border-slate-800">
    <tr>
      <th class="text-left p-3">วันที่เข้า</th><th class="text-left p-3">หุ้น</th><th class="text-left p-3">ฝั่ง</th>
      <th class="text-left p-3">หน้าเทรด</th><th class="text-right p-3">ต้นทุนเฉลี่ย</th><th class="text-right p-3">ขายเฉลี่ย</th>
      <th class="text-right p-3">ซื้อ/ขาย/เหลือ</th><th class="text-right p-3">กำไรสุทธิ</th><th class="text-right p-3">%</th>
      <th class="text-right p-3">R</th><th class="text-left p-3">สถานะ</th><th class="p-3"></th>
    </tr>
  </thead>
  <tbody id="trade-rows">
  {% for o in orders %}
  {% set s = order_stats[o['id']] %}
  <tr class="border-b border-slate-800/60 hover:bg-slate-800/40 trade-row"
      data-symbol="{{ o['symbol']|lower }}" data-strategy="{{ o['strategy'] or '' }}" data-status="{{ s.status }}"
      data-result="{{ 'win' if s.realized_pnl>0 else ('loss' if s.realized_pnl<0 else '') }}">
    <td class="p-3">{{ (s.first_open_time or o['created_at'])[:10] }}</td>
    <td class="p-3 font-semibold">{{ o['symbol'] }}</td>
    <td class="p-3"><span class="px-2 py-0.5 rounded text-xs {{ 'bg-blue-900 text-blue-300' if o['direction']=='long' else 'bg-orange-900 text-orange-300' }}">{{ 'Long' if o['direction']=='long' else 'Short' }}</span></td>
    <td class="p-3">{% if o['strategy'] %}<span class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300">{{ o['strategy'] }}</span>{% endif %}</td>
    <td class="p-3 text-right">{{ '{:,.2f}'.format(s.avg_open) if s.avg_open else '–' }}</td>
    <td class="p-3 text-right">{{ '{:,.2f}'.format(s.avg_close) if s.avg_close else '–' }}</td>
    <td class="p-3 text-right">{{ '{:,.0f}'.format(s.opened_qty) }} / {{ '{:,.0f}'.format(s.closed_qty) }} / {{ '{:,.0f}'.format(s.remaining_qty) }}</td>
    <td class="p-3 text-right {{ 'text-emerald-400' if s.realized_pnl>=0 else 'text-rose-400' }}">{{ '{:,.2f}'.format(s.realized_pnl) }}</td>
    <td class="p-3 text-right">{{ '{:+.1f}%'.format(s.roi_pct) if s.roi_pct is not none else '–' }}</td>
    <td class="p-3 text-right">{{ '%.2fR'|format(s.r_multiple) if s.r_multiple is not none else '–' }}</td>
    <td class="p-3"><span class="px-2 py-0.5 rounded text-xs bg-slate-700 text-slate-300">{{ {'open':'ถืออยู่','closed':'ปิดแล้ว','planned':'วางแผน'}[s.status] }}</span></td>
    <td class="p-3 text-right whitespace-nowrap">
      <a href="{{ url_for('trade_page', edit=o['id']) }}" class="text-emerald-400 hover:underline">แก้ไข</a>
      <form method="post" action="{{ url_for('trade_delete', order_id=o['id']) }}" class="inline" onsubmit="return confirm('ลบเทรดนี้?');">
        <button class="text-rose-400 hover:underline ml-2">ลบ</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="12" class="p-6 text-center text-slate-500">ยังไม่มีเทรด</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>

<script>
""" + LEG_ROW_JS + """
const EDIT_TRADE = {{ edit_json|safe }};
if (EDIT_TRADE) {
  (EDIT_TRADE.open || []).forEach(leg => addRow('open', leg));
  (EDIT_TRADE.close || []).forEach(leg => addRow('close', leg));
} else {
  addRow('open');
  addRow('close');
}

function applyFilters() {
  const search = document.getElementById('f-search').value.toLowerCase();
  const strategy = document.getElementById('f-strategy').value;
  const status = document.getElementById('f-status').value;
  const result = document.getElementById('f-result').value;
  document.querySelectorAll('.trade-row').forEach(row => {
    const ok = (!search || row.dataset.symbol.includes(search))
      && (!strategy || row.dataset.strategy === strategy)
      && (!status || row.dataset.status === status)
      && (!result || row.dataset.result === result);
    row.style.display = ok ? '' : 'none';
  });
}
['f-search','f-strategy','f-status','f-result'].forEach(id => {
  document.getElementById(id).addEventListener('input', applyFilters);
  document.getElementById(id).addEventListener('change', applyFilters);
});
</script>
""" + BASE_TAIL


PORTFOLIO_TEMPLATE = BASE_HEAD + """
<div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4 mb-8">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">มูลค่าพอร์ต</div>
    <div class="text-lg font-semibold">฿{{ '{:,.2f}'.format(d.current_value) }}</div>
    <div class="text-xs text-slate-500">ทุนเริ่มต้น {{ '{:,.2f}'.format(d.initial_capital) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ฝากเพิ่ม / ถอน</div>
    <div class="text-lg font-semibold">{{ '{:,.2f}'.format(d.net_deposits) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ผลตอบแทนรวม</div>
    <div class="text-lg font-semibold {{ 'text-emerald-400' if d.total_return_pct>=0 else 'text-rose-400' }}">{{ '{:+.2f}%'.format(d.total_return_pct) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ผลตอบแทนต่อปี (CAGR)</div>
    <div class="text-lg font-semibold">{{ '{:+.2f}%'.format(d.cagr) if d.cagr is not none else '–' }}</div>
    <div class="text-xs text-slate-500">ต้องมีข้อมูลอย่างน้อย 30 วัน</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Max Drawdown</div>
    <div class="text-lg font-semibold text-rose-400">{{ '{:.2f}%'.format(d.max_drawdown) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">สถานะที่ยังถืออยู่</div>
    <div class="text-lg font-semibold">{{ d.open_position_count }}</div>
    <div class="text-xs text-slate-500">ต้นทุนคงเหลือ {{ '{:,.2f}'.format(d.open_cost_value) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">สัดส่วนถือหุ้น</div>
    <div class="text-lg font-semibold">{{ '{:.1f}%'.format(d.invested_ratio) }}</div>
    <div class="text-xs text-slate-500">ต้นทุนคงเหลือ ÷ มูลค่าพอร์ต</div>
  </div>
</div>

<div class="grid md:grid-cols-2 gap-6">
  <div class="bg-slate-900 border border-slate-800 rounded-xl p-6">
    <h2 class="font-semibold mb-4">บันทึกฝาก/ถอนเงิน</h2>
    <form method="post" action="{{ url_for('add_cashflow') }}" class="space-y-3 mb-6">
      <div>
        <label class="block text-sm text-slate-400 mb-1">วันที่</label>
        <input type="date" name="flow_date" required value="{{ today }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
      <div>
        <label class="block text-sm text-slate-400 mb-1">ประเภท</label>
        <select name="type" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
          <option value="deposit">ฝากเงิน</option>
          <option value="withdraw">ถอนเงิน</option>
        </select>
      </div>
      <div>
        <label class="block text-sm text-slate-400 mb-1">จำนวนเงิน</label>
        <input type="number" step="any" name="amount" required min="0.01" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
      <div>
        <label class="block text-sm text-slate-400 mb-1">หมายเหตุ</label>
        <input name="note" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
      <button class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg font-medium w-full">เพิ่มรายการ</button>
    </form>
    <div class="space-y-2 max-h-80 overflow-y-auto">
      {% for cf in d.cash_flows|reverse %}
      <div class="flex items-center justify-between bg-slate-800/60 rounded-lg px-3 py-2 text-sm">
        <div>
          <span class="{{ 'text-emerald-400' if cf['type']=='deposit' else 'text-rose-400' }}">{{ 'ฝาก' if cf['type']=='deposit' else 'ถอน' }}</span>
          {{ '{:,.2f}'.format(cf['amount']) }} <span class="text-slate-500">· {{ cf['flow_date'] }}</span>
          {% if cf['note'] %}<div class="text-xs text-slate-500">{{ cf['note'] }}</div>{% endif %}
        </div>
        <form method="post" action="{{ url_for('delete_cashflow', cf_id=cf['id']) }}" onsubmit="return confirm('ลบรายการนี้?');">
          <button class="text-rose-400 hover:underline text-xs">ลบ</button>
        </form>
      </div>
      {% else %}
      <p class="text-slate-500 text-center py-6">ยังไม่มีรายการฝาก/ถอน</p>
      {% endfor %}
    </div>
  </div>

  <div class="bg-slate-900 border border-slate-800 rounded-xl p-6">
    <h2 class="font-semibold mb-4">จำลองการเติบโตของพอร์ต</h2>
    <div class="space-y-3 mb-4">
      <div>
        <label class="block text-sm text-slate-400 mb-1">ผลตอบแทนต่อเดือน (%)</label>
        <input type="number" step="any" id="sim-rate" value="3" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
      <div>
        <label class="block text-sm text-slate-400 mb-1">ฝากเพิ่มต่อเดือน</label>
        <input type="number" step="any" id="sim-deposit" value="0" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
      <div>
        <label class="block text-sm text-slate-400 mb-1">ระยะเวลา (เดือน)</label>
        <input type="number" id="sim-months" value="24" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
    </div>
    <p class="text-xs text-slate-500 mb-3">เริ่มจากมูลค่าพอร์ตปัจจุบัน ทบต้นรายเดือน · ใช้ประกอบการวางแผนเท่านั้น ไม่ใช่การรับประกันผล</p>
    <div class="max-h-80 overflow-y-auto">
      <table class="w-full text-sm">
        <thead class="text-slate-400 text-left sticky top-0 bg-slate-900"><tr><th class="py-1">เดือนที่</th><th class="text-right">มูลค่าพอร์ต</th><th class="text-right">ฝากเพิ่มสะสม</th><th class="text-right">เติบโตจากปัจจุบัน</th></tr></thead>
        <tbody id="sim-rows"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const START_VALUE = {{ d.current_value }};
function runSimulation() {
  const rate = (parseFloat(document.getElementById('sim-rate').value) || 0) / 100;
  const deposit = parseFloat(document.getElementById('sim-deposit').value) || 0;
  const months = parseInt(document.getElementById('sim-months').value) || 0;
  let value = START_VALUE, depositSum = 0;
  const rows = [];
  for (let m = 1; m <= months; m++) {
    value = value * (1 + rate) + deposit;
    depositSum += deposit;
    const growth = START_VALUE ? ((value - depositSum - START_VALUE) / START_VALUE) * 100 : 0;
    rows.push(`<tr class="border-t border-slate-800"><td class="py-1">${m}</td><td class="text-right font-medium">${value.toLocaleString(undefined,{maximumFractionDigits:2})}</td><td class="text-right">${depositSum.toLocaleString(undefined,{maximumFractionDigits:2})}</td><td class="text-right text-emerald-400">+${growth.toFixed(2)}%</td></tr>`);
  }
  document.getElementById('sim-rows').innerHTML = rows.join('');
}
['sim-rate','sim-deposit','sim-months'].forEach(id => document.getElementById(id).addEventListener('input', runSimulation));
runSimulation();
</script>
""" + BASE_TAIL


SETTINGS_TEMPLATE = BASE_HEAD + """
<h1 class="text-xl font-bold mb-4">ตั้งค่าพอร์ต</h1>
<div class="grid md:grid-cols-2 gap-6 mb-8">
  <form method="post" action="{{ url_for('settings_page') }}" class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4">
    <h2 class="font-semibold">ตั้งค่าพอร์ต</h2>
    <div>
      <label class="block text-sm text-slate-400 mb-1">เงินทุนเริ่มต้น</label>
      <input type="number" step="any" name="initial_capital" value="{{ initial_capital }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">ค่าธรรมเนียมต่อฝั่ง (% รวม VAT)</label>
      <input type="number" step="any" name="fee_rate" value="{{ fee_rate }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <button class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg font-medium">บันทึกการตั้งค่า</button>
    <p class="text-xs text-slate-500">ค่าธรรมเนียมอัตโนมัติ = ราคา × จำนวนหุ้น × อัตรา ต่อไม้ (เมื่อไม่กรอกเอง)</p>
  </form>

  <div class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4">
    <h2 class="font-semibold">ประเภทหน้าเทรด</h2>
    <div class="flex flex-wrap gap-2">
      {% for s in strategies %}
      <span class="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded-full px-3 py-1 text-sm">
        {{ s }}
        <form method="post" action="{{ url_for('delete_strategy') }}" class="inline">
          <input type="hidden" name="name" value="{{ s }}">
          <button class="text-slate-500 hover:text-rose-400">×</button>
        </form>
      </span>
      {% endfor %}
    </div>
    <form method="post" action="{{ url_for('add_strategy') }}" class="flex gap-2">
      <input name="name" required placeholder="ชื่อหน้าเทรดใหม่" class="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <button class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded-lg font-medium">เพิ่ม</button>
    </form>
  </div>
</div>

<div class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4 mb-8">
  <h2 class="font-semibold">จัดการข้อมูล</h2>
  <div class="flex flex-wrap gap-3">
    <a href="{{ url_for('export_json') }}" class="bg-slate-800 hover:bg-slate-700 px-4 py-2 rounded-lg text-sm font-medium">สำรองข้อมูล (JSON)</a>
    <form method="post" action="{{ url_for('import_json') }}" enctype="multipart/form-data" class="flex items-center gap-2">
      <input type="file" name="file" accept="application/json" required class="text-sm">
      <button class="bg-slate-800 hover:bg-slate-700 px-4 py-2 rounded-lg text-sm font-medium">นำเข้า (JSON)</button>
    </form>
    <a href="{{ url_for('export_csv') }}" class="bg-slate-800 hover:bg-slate-700 px-4 py-2 rounded-lg text-sm font-medium">ส่งออกไม้ทั้งหมด (CSV)</a>
    <form method="post" action="{{ url_for('delete_all_data') }}" onsubmit="return confirm('ลบข้อมูลเทรดและฝาก/ถอนทั้งหมด? การตั้งค่าจะยังคงอยู่');">
      <button class="bg-rose-700 hover:bg-rose-600 px-4 py-2 rounded-lg text-sm font-medium">ลบข้อมูลทั้งหมด</button>
    </form>
  </div>
  <p class="text-xs text-slate-500">ข้อมูลเก็บอยู่ในไฟล์ trades.db บนเครื่องที่รันแอปนี้ · ควรสำรองเป็นไฟล์ JSON เป็นระยะ</p>
</div>

<div class="bg-slate-900 border border-slate-800 rounded-xl p-6 max-w-3xl space-y-4">
  <h2 class="text-lg font-semibold">สำรองข้อมูลไป Google Sheets</h2>
  {% if not gspread_available %}
  <div class="bg-amber-900/40 border border-amber-700 text-amber-200 px-4 py-2 rounded text-sm">
    ยังไม่ได้ติดตั้งไลบรารีที่จำเป็น กรุณารัน <code class="bg-slate-800 px-1 rounded">pip install gspread google-auth</code> แล้วรีสตาร์ทแอป
  </div>
  {% endif %}
  <p class="text-sm text-slate-400">
    ต้องมี Google Service Account (ไฟล์ JSON Key) ที่เปิดใช้งาน Google Sheets API และ Drive API
    วาง JSON ทั้งก้อนด้านล่าง ระบุ Spreadsheet ID ถ้ามีอยู่แล้ว (ไม่ระบุ = สร้างใหม่อัตโนมัติ)
    และถ้าต้องการให้อีเมลส่วนตัวเห็นไฟล์ ให้กรอกอีเมลเพื่อแชร์สิทธิ์แก้ไขให้อัตโนมัติ
  </p>
  <form method="post" action="{{ url_for('save_google_settings') }}" class="space-y-3">
    <div>
      <label class="block text-sm text-slate-400 mb-1">Service Account JSON</label>
      <textarea name="google_credentials_json" rows="6" placeholder='{"type": "service_account", ...}'
                class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 font-mono text-xs">{{ google_credentials_json }}</textarea>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="block text-sm text-slate-400 mb-1">Spreadsheet ID (ถ้ามี)</label>
        <input name="google_spreadsheet_id" value="{{ google_spreadsheet_id }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
      <div>
        <label class="block text-sm text-slate-400 mb-1">แชร์ให้อีเมล (ถ้าต้องการ)</label>
        <input name="google_share_email" value="{{ google_share_email }}" placeholder="you@gmail.com" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      </div>
    </div>
    <button class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg font-medium">บันทึกการตั้งค่า Google Sheets</button>
  </form>
  <hr class="border-slate-800">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <div class="text-sm text-slate-400">
      {% if google_last_backup %}
      สำรองข้อมูลล่าสุด: {{ google_last_backup }}
      {% if google_spreadsheet_url %} — <a href="{{ google_spreadsheet_url }}" target="_blank" class="text-emerald-400 hover:underline">เปิด Spreadsheet</a>{% endif %}
      {% else %}
      ยังไม่เคยสำรองข้อมูล
      {% endif %}
    </div>
    <form method="post" action="{{ url_for('run_google_backup') }}">
      <button class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded-lg font-medium">สำรองข้อมูลตอนนี้</button>
    </form>
  </div>
</div>
""" + BASE_TAIL


# ---------------------------------------------------------------------------
# Flash (very small, session-free) message store
# ---------------------------------------------------------------------------

_flash_box = {"messages": []}


def flash_msg(msg):
    _flash_box["messages"].append(msg)


def get_flashed():
    msgs = _flash_box["messages"]
    _flash_box["messages"] = []
    return msgs


app.jinja_env.globals["get_flashed"] = get_flashed
app.jinja_env.globals["float"] = float


# ---------------------------------------------------------------------------
# Routes - overview
# ---------------------------------------------------------------------------

@app.route("/")
def overview():
    d = compute_dashboard()
    return render_template_string(
        OVERVIEW_TEMPLATE,
        title="ภาพรวม",
        active="overview",
        d=d,
        equity_json=json.dumps(d["equity_curve"]),
    )


# ---------------------------------------------------------------------------
# Routes - trades
# ---------------------------------------------------------------------------

def _order_to_edit_json(order):
    executions = fetch_executions(order["id"])
    def leg(e):
        return {
            "id": e["id"],
            "date": e["exec_time"][:10] if e["exec_time"] else "",
            "time": e["exec_time"][11:16] if e["exec_time"] and len(e["exec_time"]) > 10 else "",
            "qty": e["qty"],
            "price": e["price"],
            "fee": e["fee"],
            "executed": bool(e["executed"]),
            "note": e["note"] or "",
            "image": e["image_filename"] or "",
        }
    return {
        "id": order["id"],
        "open": [leg(e) for e in executions if e["action"] == "open"],
        "close": [leg(e) for e in executions if e["action"] == "close"],
    }


@app.route("/trade")
def trade_page():
    orders = fetch_orders()
    d = compute_dashboard()
    edit_id = request.args.get("edit", type=int)
    edit_trade = fetch_order(edit_id) if edit_id else None
    edit_json = json.dumps(_order_to_edit_json(edit_trade)) if edit_trade else "null"
    known_symbols = [
        r["symbol"] for r in get_db().execute("SELECT DISTINCT symbol FROM orders ORDER BY symbol")
    ]
    return render_template_string(
        TRADE_TEMPLATE,
        title="บันทึกเทรด",
        active="trade",
        orders=orders,
        order_stats=d["order_stats"],
        strategies=get_strategies(),
        edit_trade=edit_trade,
        edit_json=edit_json,
        known_symbols=known_symbols,
    )


@app.route("/trade/save", methods=["POST"])
def trade_save():
    db = get_db()
    trade_id = request.form.get("trade_id", "").strip()
    symbol = request.form["symbol"].strip().upper()
    direction = request.form.get("direction", "long")
    strategy = request.form.get("strategy", "").strip()
    stop_loss = request.form.get("stop_loss", "").strip()
    target_price = request.form.get("target_price", "").strip()
    followed_plan = request.form.get("followed_plan", "unspecified")
    note = request.form.get("note", "").strip()

    if trade_id:
        order_id = int(trade_id)
        db.execute(
            "UPDATE orders SET symbol=?, direction=?, strategy=?, stop_loss=?, target_price=?, "
            "followed_plan=?, note=? WHERE id=?",
            (
                symbol, direction, strategy,
                float(stop_loss) if stop_loss else None,
                float(target_price) if target_price else None,
                followed_plan, note, order_id,
            ),
        )
        existing_ids = {row["id"] for row in fetch_executions(order_id)}
        existing_by_id = {row["id"]: row for row in fetch_executions(order_id)}
    else:
        cur = db.execute(
            "INSERT INTO orders (symbol, direction, strategy, stop_loss, target_price, followed_plan, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol, direction, strategy,
                float(stop_loss) if stop_loss else None,
                float(target_price) if target_price else None,
                followed_plan, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        order_id = cur.lastrowid
        existing_ids = set()
        existing_by_id = {}

    fee_rate = get_fee_rate()
    seen_ids = set()

    for side, action in (("open", "open"), ("close", "close")):
        dates = request.form.getlist(f"{side}_date[]")
        hours = request.form.getlist(f"{side}_hour[]")
        minutes = request.form.getlist(f"{side}_minute[]")
        qtys = request.form.getlist(f"{side}_qty[]")
        prices = request.form.getlist(f"{side}_price[]")
        fees = request.form.getlist(f"{side}_fee[]")
        dones = request.form.getlist(f"{side}_done[]")
        notes = request.form.getlist(f"{side}_note[]")
        images = request.files.getlist(f"{side}_image[]")
        row_ids = request.form.getlist(f"{side}_row_id[]") or [""] * len(dates)

        for i in range(len(dates)):
            if not qtys[i] or not prices[i]:
                continue
            qty = float(qtys[i])
            price = float(prices[i])
            fee_raw = fees[i].strip() if i < len(fees) else ""
            fee = float(fee_raw) if fee_raw else round(qty * price * fee_rate / 100, 2)
            executed = 1 if (i < len(dones) and dones[i] == "1") else 0
            note_leg = notes[i] if i < len(notes) else ""
            hh = hours[i] if i < len(hours) else "00"
            mm = minutes[i] if i < len(minutes) else "00"
            exec_time = f"{dates[i]} {hh}:{mm}"
            row_id = row_ids[i].strip() if i < len(row_ids) else ""
            image_file = images[i] if i < len(images) else None
            new_image = save_image(image_file)

            if row_id and int(row_id) in existing_ids:
                rid = int(row_id)
                image_filename = new_image if new_image else existing_by_id[rid]["image_filename"]
                db.execute(
                    "UPDATE executions SET action=?, qty=?, price=?, fee=?, executed=?, exec_time=?, note=?, image_filename=? "
                    "WHERE id=? AND order_id=?",
                    (action, qty, price, fee, executed, exec_time, note_leg, image_filename, rid, order_id),
                )
                seen_ids.add(rid)
            else:
                cur = db.execute(
                    "INSERT INTO executions (order_id, action, qty, price, fee, executed, exec_time, note, image_filename) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order_id, action, qty, price, fee, executed, exec_time, note_leg, new_image),
                )
                seen_ids.add(cur.lastrowid)

    for old_id in existing_ids - seen_ids:
        db.execute("DELETE FROM executions WHERE id = ?", (old_id,))

    db.commit()
    flash_msg(f"บันทึกเทรด {symbol} เรียบร้อยแล้ว")
    return redirect(url_for("trade_page"))


@app.route("/trade/<int:order_id>/delete", methods=["POST"])
def trade_delete(order_id):
    db = get_db()
    db.execute("DELETE FROM executions WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    db.commit()
    flash_msg("ลบเทรดเรียบร้อยแล้ว")
    return redirect(url_for("trade_page"))


# ---------------------------------------------------------------------------
# Routes - portfolio
# ---------------------------------------------------------------------------

@app.route("/portfolio")
def portfolio_page():
    d = compute_dashboard()
    return render_template_string(
        PORTFOLIO_TEMPLATE,
        title="พอร์ต",
        active="portfolio",
        d=d,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/portfolio/cashflow", methods=["POST"])
def add_cashflow():
    db = get_db()
    db.execute(
        "INSERT INTO cash_flows (flow_date, type, amount, note) VALUES (?, ?, ?, ?)",
        (
            request.form["flow_date"],
            request.form.get("type", "deposit"),
            float(request.form["amount"]),
            request.form.get("note", "").strip(),
        ),
    )
    db.commit()
    flash_msg("บันทึกรายการฝาก/ถอนเรียบร้อยแล้ว")
    return redirect(url_for("portfolio_page"))


@app.route("/portfolio/cashflow/<int:cf_id>/delete", methods=["POST"])
def delete_cashflow(cf_id):
    db = get_db()
    db.execute("DELETE FROM cash_flows WHERE id = ?", (cf_id,))
    db.commit()
    flash_msg("ลบรายการเรียบร้อยแล้ว")
    return redirect(url_for("portfolio_page"))


# ---------------------------------------------------------------------------
# Routes - settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        set_setting("initial_capital", request.form.get("initial_capital", "100000"))
        set_setting("fee_rate", request.form.get("fee_rate", "0.157"))
        flash_msg("บันทึกการตั้งค่าเรียบร้อยแล้ว")
        return redirect(url_for("settings_page"))
    return render_template_string(
        SETTINGS_TEMPLATE,
        title="ตั้งค่า",
        active="settings",
        initial_capital=get_setting("initial_capital", "100000"),
        fee_rate=get_setting("fee_rate", "0.157"),
        strategies=get_strategies(),
        gspread_available=GSPREAD_AVAILABLE,
        google_credentials_json=get_setting("google_credentials_json", "") or "",
        google_spreadsheet_id=get_setting("google_spreadsheet_id", "") or "",
        google_share_email=get_setting("google_share_email", "") or "",
        google_last_backup=get_setting("google_last_backup", "") or "",
        google_spreadsheet_url=get_setting("google_spreadsheet_url", "") or "",
    )


@app.route("/settings/strategy/add", methods=["POST"])
def add_strategy():
    name = request.form.get("name", "").strip()
    if name:
        strategies = get_strategies()
        if name not in strategies:
            strategies.append(name)
            set_strategies(strategies)
            flash_msg(f"เพิ่มหน้าเทรด {name} เรียบร้อยแล้ว")
    return redirect(url_for("settings_page"))


@app.route("/settings/strategy/delete", methods=["POST"])
def delete_strategy():
    name = request.form.get("name", "").strip()
    strategies = [s for s in get_strategies() if s != name]
    set_strategies(strategies)
    flash_msg(f"ลบหน้าเทรด {name} เรียบร้อยแล้ว")
    return redirect(url_for("settings_page"))


@app.route("/settings/google", methods=["POST"])
def save_google_settings():
    set_setting("google_credentials_json", request.form.get("google_credentials_json", "").strip())
    set_setting("google_spreadsheet_id", request.form.get("google_spreadsheet_id", "").strip())
    set_setting("google_share_email", request.form.get("google_share_email", "").strip())
    flash_msg("บันทึกการตั้งค่า Google Sheets เรียบร้อยแล้ว")
    return redirect(url_for("settings_page"))


@app.route("/backup/google-sheets", methods=["POST"])
def run_google_backup():
    try:
        url = backup_to_google_sheets()
        flash_msg(f"สำรองข้อมูลไป Google Sheets สำเร็จ: {url}")
    except Exception as exc:  # noqa: BLE001 - surface any backup failure to the user
        flash_msg(f"สำรองข้อมูลไม่สำเร็จ: {exc}")
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------------------
# Routes - data import / export
# ---------------------------------------------------------------------------

@app.route("/export/json")
def export_json():
    db = get_db()
    data = {
        "settings": {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")},
        "orders": [dict(r) for r in db.execute("SELECT * FROM orders")],
        "executions": [dict(r) for r in db.execute("SELECT * FROM executions")],
        "cash_flows": [dict(r) for r in db.execute("SELECT * FROM cash_flows")],
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    fname = f"trade-journal-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name=fname)


@app.route("/import/json", methods=["POST"])
def import_json():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash_msg("กรุณาเลือกไฟล์ JSON")
        return redirect(url_for("settings_page"))
    try:
        data = json.loads(file.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        flash_msg(f"นำเข้าไม่สำเร็จ: ไฟล์ JSON ไม่ถูกต้อง ({exc})")
        return redirect(url_for("settings_page"))

    db = get_db()
    try:
        db.execute("DELETE FROM executions")
        db.execute("DELETE FROM orders")
        db.execute("DELETE FROM cash_flows")
        for key, value in (data.get("settings") or {}).items():
            set_setting(key, value)
        for o in data.get("orders", []):
            db.execute(
                "INSERT INTO orders (id, symbol, direction, strategy, stop_loss, target_price, followed_plan, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    o.get("id"), o.get("symbol"), o.get("direction"), o.get("strategy", ""),
                    o.get("stop_loss"), o.get("target_price"), o.get("followed_plan", "unspecified"),
                    o.get("note"), o.get("created_at"),
                ),
            )
        for e in data.get("executions", []):
            db.execute(
                "INSERT INTO executions (id, order_id, action, qty, price, fee, executed, exec_time, note, image_filename) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e.get("id"), e.get("order_id"), e.get("action"), e.get("qty"), e.get("price"),
                    e.get("fee", 0), e.get("executed", 1), e.get("exec_time"), e.get("note"),
                    e.get("image_filename"),
                ),
            )
        for cf in data.get("cash_flows", []):
            db.execute(
                "INSERT INTO cash_flows (id, flow_date, type, amount, note) VALUES (?, ?, ?, ?, ?)",
                (cf.get("id"), cf.get("flow_date"), cf.get("type"), cf.get("amount"), cf.get("note")),
            )
        db.commit()
        flash_msg("นำเข้าข้อมูลเรียบร้อยแล้ว")
    except sqlite3.Error as exc:
        db.rollback()
        flash_msg(f"นำเข้าไม่สำเร็จ: {exc}")
    return redirect(url_for("settings_page"))


@app.route("/export/csv")
def export_csv():
    orders = fetch_orders()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "OrderID", "Symbol", "Direction", "Strategy", "Status", "Action", "Qty", "Price",
        "Fee", "Executed", "ExecTime", "Note",
    ])
    for o in orders:
        for e in fetch_executions(o["id"]):
            stats = compute_order_stats(o, fetch_executions(o["id"]))
            writer.writerow([
                o["id"], o["symbol"], o["direction"], o["strategy"] or "", stats["status"],
                e["action"], e["qty"], e["price"], e["fee"] or 0, "yes" if e["executed"] else "no",
                e["exec_time"], e["note"] or "",
            ])
    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    fname = f"trade-journal-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=fname)


@app.route("/data/delete-all", methods=["POST"])
def delete_all_data():
    db = get_db()
    db.execute("DELETE FROM executions")
    db.execute("DELETE FROM orders")
    db.execute("DELETE FROM cash_flows")
    db.commit()
    flash_msg("ลบข้อมูลเทรดและฝาก/ถอนทั้งหมดเรียบร้อยแล้ว (การตั้งค่ายังคงอยู่)")
    return redirect(url_for("settings_page"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5001)
