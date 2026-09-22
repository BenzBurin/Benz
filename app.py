"""Stock Trading Journal - single-file Flask app.

Tracks buy/sell orders with partial (scaled) entries and exits for both
long and short positions, records the exact execution date/time of each
fill, allows attaching a screenshot to each execution, and computes
trading performance (win rate, profit factor, realized P/L) plus a
portfolio growth curve from an initial capital setting.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime

from flask import (
    Flask,
    g,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

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

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB uploads


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
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('open', 'close')),
            qty REAL NOT NULL,
            price REAL NOT NULL,
            exec_time TEXT NOT NULL,
            note TEXT,
            image_filename TEXT,
            FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE
        );
        """
    )
    cur = db.execute("SELECT value FROM settings WHERE key = 'initial_capital'")
    if cur.fetchone() is None:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('initial_capital', ?)",
            ("100000",),
        )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Domain calculations
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


def fetch_orders():
    return get_db().execute("SELECT * FROM orders ORDER BY created_at DESC, id DESC").fetchall()


def fetch_order(order_id):
    return get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def fetch_executions(order_id):
    return get_db().execute(
        "SELECT * FROM executions WHERE order_id = ? ORDER BY exec_time ASC, id ASC",
        (order_id,),
    ).fetchall()


def compute_order_stats(order, executions):
    """Weighted-average cost model.

    open executions build the average entry price; close executions
    realize P/L against that average (long: sell above cost = profit,
    short: buy back below the average short price = profit).
    """
    opens = [e for e in executions if e["action"] == "open"]
    closes = [e for e in executions if e["action"] == "close"]

    opened_qty = sum(e["qty"] for e in opens)
    closed_qty = sum(e["qty"] for e in closes)
    avg_open = (sum(e["qty"] * e["price"] for e in opens) / opened_qty) if opened_qty else 0.0
    avg_close = (sum(e["qty"] * e["price"] for e in closes) / closed_qty) if closed_qty else 0.0

    sign = 1 if order["direction"] == "long" else -1
    realized_pnl = sum(sign * e["qty"] * (e["price"] - avg_open) for e in closes)
    remaining_qty = round(opened_qty - closed_qty, 6)
    status = "closed" if opened_qty > 0 and remaining_qty <= 0 else "open"

    return {
        "opened_qty": opened_qty,
        "closed_qty": closed_qty,
        "remaining_qty": remaining_qty,
        "avg_open": avg_open,
        "avg_close": avg_close,
        "realized_pnl": realized_pnl,
        "status": status,
    }


def compute_portfolio():
    db = get_db()
    orders = fetch_orders()
    initial_capital = float(get_setting("initial_capital", "100000") or 0)

    pnl_events = []  # (datetime_str, pnl)
    all_stats = {}
    for order in orders:
        executions = fetch_executions(order["id"])
        stats = compute_order_stats(order, executions)
        all_stats[order["id"]] = stats

        opens = [e for e in executions if e["action"] == "open"]
        closes = [e for e in executions if e["action"] == "close"]
        opened_qty = sum(e["qty"] for e in opens)
        avg_open = (sum(e["qty"] * e["price"] for e in opens) / opened_qty) if opened_qty else 0.0
        sign = 1 if order["direction"] == "long" else -1
        for e in closes:
            pnl = sign * e["qty"] * (e["price"] - avg_open)
            pnl_events.append((e["exec_time"], pnl))

    pnl_events.sort(key=lambda x: x[0])

    total_realized = sum(p for _, p in pnl_events)
    wins = [p for _, p in pnl_events if p > 0]
    losses = [p for _, p in pnl_events if p < 0]
    win_rate = (len(wins) / len(pnl_events) * 100) if pnl_events else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    equity_curve = [{"time": "start", "equity": initial_capital}]
    running = initial_capital
    for t, p in pnl_events:
        running += p
        equity_curve.append({"time": t, "equity": round(running, 2)})

    current_equity = running
    growth_pct = ((current_equity - initial_capital) / initial_capital * 100) if initial_capital else 0.0

    return {
        "initial_capital": initial_capital,
        "current_equity": current_equity,
        "growth_pct": growth_pct,
        "total_realized": total_realized,
        "trade_count": len(pnl_events),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "equity_curve": equity_curve,
        "order_stats": all_stats,
    }


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
        raise RuntimeError(
            "ยังไม่ได้ติดตั้งไลบรารีที่จำเป็น กรุณารัน: pip install gspread google-auth"
        )
    creds_raw = get_setting("google_credentials_json", "")
    if not creds_raw:
        raise RuntimeError("ยังไม่ได้ตั้งค่า Google Service Account credentials ในหน้า Settings")
    try:
        info = json.loads(creds_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Credentials JSON ไม่ถูกต้อง: {exc}") from exc
    credentials = GoogleServiceCredentials.from_service_account_info(
        info, scopes=GOOGLE_SHEETS_SCOPES
    )
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
    """Export orders, executions and the performance summary to a Google Sheet.

    Returns the spreadsheet URL on success; raises RuntimeError with a
    human-readable message on failure.
    """
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
            "ID", "Symbol", "Direction", "Note", "Created At", "Status",
            "Avg Open", "Avg Close", "Opened Qty", "Closed Qty",
            "Remaining Qty", "Realized P/L",
        ]
    ]
    exec_rows = [
        ["Order ID", "Symbol", "Action", "Qty", "Price", "Exec Time", "Note", "Image Filename"]
    ]
    for o in orders:
        executions = fetch_executions(o["id"])
        stats = compute_order_stats(o, executions)
        order_rows.append(
            [
                o["id"], o["symbol"], o["direction"], o["note"] or "", o["created_at"],
                stats["status"], round(stats["avg_open"], 4), round(stats["avg_close"], 4),
                stats["opened_qty"], stats["closed_qty"], stats["remaining_qty"],
                round(stats["realized_pnl"], 2),
            ]
        )
        for e in executions:
            exec_rows.append(
                [
                    o["id"], o["symbol"], e["action"], e["qty"], e["price"], e["exec_time"],
                    e["note"] or "", e["image_filename"] or "",
                ]
            )

    portfolio = compute_portfolio()
    summary_rows = [
        ["Metric", "Value"],
        ["Initial Capital", portfolio["initial_capital"]],
        ["Current Equity", round(portfolio["current_equity"], 2)],
        ["Growth %", round(portfolio["growth_pct"], 2)],
        ["Total Realized P/L", round(portfolio["total_realized"], 2)],
        ["Win Rate %", round(portfolio["win_rate"], 2)],
        [
            "Profit Factor",
            portfolio["profit_factor"] if portfolio["profit_factor"] != float("inf") else "inf",
        ],
        ["Avg Win", round(portfolio["avg_win"], 2)],
        ["Avg Loss", round(portfolio["avg_loss"], 2)],
        ["Backup Time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]

    _write_sheet(sh, "Orders", order_rows)
    _write_sheet(sh, "Executions", exec_rows)
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
<nav class="bg-slate-900 border-b border-slate-800 px-6 py-4 flex items-center justify-between">
  <a href="{{ url_for('index') }}" class="text-xl font-bold text-emerald-400">📈 Trading Journal</a>
  <div class="flex gap-4 text-sm">
    <a href="{{ url_for('index') }}" class="hover:text-emerald-400">Dashboard</a>
    <a href="{{ url_for('new_order') }}" class="hover:text-emerald-400">New Order</a>
    <a href="{{ url_for('settings_page') }}" class="hover:text-emerald-400">Settings</a>
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

INDEX_TEMPLATE = BASE_HEAD + """
<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ทุนเริ่มต้น</div>
    <div class="text-lg font-semibold">{{ '{:,.2f}'.format(p.initial_capital) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">มูลค่าพอร์ตปัจจุบัน</div>
    <div class="text-lg font-semibold">{{ '{:,.2f}'.format(p.current_equity) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">การเติบโตพอร์ต</div>
    <div class="text-lg font-semibold {{ 'text-emerald-400' if p.growth_pct >= 0 else 'text-rose-400' }}">
      {{ '{:+.2f}'.format(p.growth_pct) }}%
    </div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">กำไร/ขาดทุนสะสม</div>
    <div class="text-lg font-semibold {{ 'text-emerald-400' if p.total_realized >= 0 else 'text-rose-400' }}">
      {{ '{:,.2f}'.format(p.total_realized) }}
    </div>
  </div>
</div>

<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Win Rate</div>
    <div class="text-lg font-semibold">{{ '{:.1f}'.format(p.win_rate) }}%</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">Profit Factor</div>
    <div class="text-lg font-semibold">{{ '%.2f'|format(p.profit_factor) if p.profit_factor != float('inf') else '∞' }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">กำไรเฉลี่ย/ไม้</div>
    <div class="text-lg font-semibold text-emerald-400">{{ '{:,.2f}'.format(p.avg_win) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ขาดทุนเฉลี่ย/ไม้</div>
    <div class="text-lg font-semibold text-rose-400">{{ '{:,.2f}'.format(p.avg_loss) }}</div>
  </div>
</div>

<div class="bg-slate-900 rounded-xl p-4 border border-slate-800 mb-8">
  <h2 class="text-sm text-slate-400 mb-2">กราฟการเติบโตของพอร์ต (Equity Curve)</h2>
  <canvas id="equityChart" height="90"></canvas>
</div>

<div class="flex items-center justify-between mb-4">
  <h2 class="text-lg font-semibold">รายการออเดอร์</h2>
  <a href="{{ url_for('new_order') }}" class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg text-sm font-medium">+ เปิดออเดอร์ใหม่</a>
</div>

<div class="overflow-x-auto bg-slate-900 rounded-xl border border-slate-800">
<table class="w-full text-sm">
  <thead class="text-slate-400 border-b border-slate-800">
    <tr>
      <th class="text-left p-3">สัญลักษณ์</th>
      <th class="text-left p-3">ประเภท</th>
      <th class="text-right p-3">ราคาเข้าเฉลี่ย</th>
      <th class="text-right p-3">ปิดแล้ว</th>
      <th class="text-right p-3">คงเหลือ</th>
      <th class="text-right p-3">P/L รับรู้แล้ว</th>
      <th class="text-left p-3">สถานะ</th>
      <th class="text-left p-3"></th>
    </tr>
  </thead>
  <tbody>
  {% for o in orders %}
  {% set s = p.order_stats[o['id']] %}
  <tr class="border-b border-slate-800/60 hover:bg-slate-800/40">
    <td class="p-3 font-semibold">{{ o['symbol'] }}</td>
    <td class="p-3">
      <span class="px-2 py-0.5 rounded text-xs {{ 'bg-blue-900 text-blue-300' if o['direction']=='long' else 'bg-orange-900 text-orange-300' }}">
        {{ 'Long' if o['direction']=='long' else 'Short' }}
      </span>
    </td>
    <td class="p-3 text-right">{{ '{:,.2f}'.format(s.avg_open) }}</td>
    <td class="p-3 text-right">{{ '{:,.0f}'.format(s.closed_qty) }} / {{ '{:,.0f}'.format(s.opened_qty) }}</td>
    <td class="p-3 text-right">{{ '{:,.0f}'.format(s.remaining_qty) }}</td>
    <td class="p-3 text-right {{ 'text-emerald-400' if s.realized_pnl >= 0 else 'text-rose-400' }}">{{ '{:,.2f}'.format(s.realized_pnl) }}</td>
    <td class="p-3">
      <span class="px-2 py-0.5 rounded text-xs {{ 'bg-slate-700 text-slate-300' if s.status=='closed' else 'bg-emerald-900 text-emerald-300' }}">
        {{ 'ปิดแล้ว' if s.status=='closed' else 'เปิดอยู่' }}
      </span>
    </td>
    <td class="p-3 text-right"><a href="{{ url_for('order_detail', order_id=o['id']) }}" class="text-emerald-400 hover:underline">ดูรายละเอียด →</a></td>
  </tr>
  {% else %}
  <tr><td colspan="8" class="p-6 text-center text-slate-500">ยังไม่มีออเดอร์</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>

<script>
const curve = {{ equity_json|safe }};
new Chart(document.getElementById('equityChart'), {
  type: 'line',
  data: {
    labels: curve.map(p => p.time),
    datasets: [{
      label: 'Equity',
      data: curve.map(p => p.equity),
      borderColor: '#34d399',
      backgroundColor: 'rgba(52,211,153,0.15)',
      fill: true,
      tension: 0.25,
      pointRadius: 2,
    }]
  },
  options: {
    scales: {
      x: { ticks: { color: '#94a3b8', maxRotation: 0, autoSkip: true } },
      y: { ticks: { color: '#94a3b8' } }
    },
    plugins: { legend: { labels: { color: '#e2e8f0' } } }
  }
});
</script>
""" + BASE_TAIL

NEW_ORDER_TEMPLATE = BASE_HEAD + """
<h1 class="text-xl font-bold mb-4">เปิดออเดอร์ใหม่</h1>
<form method="post" class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4 max-w-lg">
  <div>
    <label class="block text-sm text-slate-400 mb-1">สัญลักษณ์หุ้น</label>
    <input name="symbol" required placeholder="เช่น IVL, PTTGC" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 uppercase">
  </div>
  <div>
    <label class="block text-sm text-slate-400 mb-1">ประเภทออเดอร์</label>
    <select name="direction" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <option value="long">Long (ซื้อก่อน แล้วขาย)</option>
      <option value="short">Short (ขายชอร์ตก่อน แล้วซื้อคืน)</option>
    </select>
  </div>
  <div>
    <label class="block text-sm text-slate-400 mb-1">หมายเหตุแผนการเทรด</label>
    <textarea name="note" rows="3" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2"></textarea>
  </div>
  <hr class="border-slate-800">
  <p class="text-sm text-slate-400">รายการเข้าไม้แรก (Open Execution)</p>
  <div class="grid grid-cols-2 gap-4">
    <div>
      <label class="block text-sm text-slate-400 mb-1">จำนวนหุ้น</label>
      <input type="number" step="any" name="qty" required class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">ราคา</label>
      <input type="number" step="any" name="price" required class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
  </div>
  <div class="grid grid-cols-2 gap-4">
    <div>
      <label class="block text-sm text-slate-400 mb-1">วันที่</label>
      <input type="date" name="exec_date" required value="{{ today }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <div>
      <label class="block text-sm text-slate-400 mb-1">เวลา</label>
      <input type="time" name="exec_clock" required value="{{ now_time }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
  </div>
  <div>
    <label class="block text-sm text-slate-400 mb-1">แนบภาพหน้าจอ (ถ้ามี)</label>
    <input type="file" name="image" accept="image/*" class="w-full text-sm">
  </div>
  <button class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg font-medium">สร้างออเดอร์</button>
</form>
""" + BASE_TAIL

ORDER_DETAIL_TEMPLATE = BASE_HEAD + """
<div class="flex items-center justify-between mb-6">
  <div>
    <h1 class="text-2xl font-bold">{{ order['symbol'] }}
      <span class="text-sm font-normal px-2 py-0.5 rounded {{ 'bg-blue-900 text-blue-300' if order['direction']=='long' else 'bg-orange-900 text-orange-300' }}">
        {{ 'Long' if order['direction']=='long' else 'Short' }}
      </span>
    </h1>
    {% if order['note'] %}<p class="text-slate-400 text-sm mt-1">{{ order['note'] }}</p>{% endif %}
  </div>
  <span class="px-3 py-1 rounded text-sm {{ 'bg-slate-700 text-slate-300' if stats.status=='closed' else 'bg-emerald-900 text-emerald-300' }}">
    {{ 'ปิดแล้ว' if stats.status=='closed' else 'เปิดอยู่' }}
  </span>
</div>

<div class="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ราคาเข้าเฉลี่ย</div>
    <div class="text-lg font-semibold">{{ '{:,.2f}'.format(stats.avg_open) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">ราคาออกเฉลี่ย</div>
    <div class="text-lg font-semibold">{{ '{:,.2f}'.format(stats.avg_close) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">เข้าไม้แล้ว</div>
    <div class="text-lg font-semibold">{{ '{:,.0f}'.format(stats.opened_qty) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">คงเหลือ</div>
    <div class="text-lg font-semibold">{{ '{:,.0f}'.format(stats.remaining_qty) }}</div>
  </div>
  <div class="bg-slate-900 rounded-xl p-4 border border-slate-800">
    <div class="text-xs text-slate-400">P/L รับรู้แล้ว</div>
    <div class="text-lg font-semibold {{ 'text-emerald-400' if stats.realized_pnl >= 0 else 'text-rose-400' }}">{{ '{:,.2f}'.format(stats.realized_pnl) }}</div>
  </div>
</div>

<div class="grid md:grid-cols-2 gap-6 mb-8">
  <form method="post" action="{{ url_for('add_execution', order_id=order['id']) }}" enctype="multipart/form-data"
        class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-3">
    <h2 class="font-semibold mb-1">
      {{ 'ทยอยซื้อเพิ่ม (Scale In)' if order['direction']=='long' else 'ทยอยขายชอร์ตเพิ่ม (Scale In)' }}
    </h2>
    <input type="hidden" name="action" value="open">
    <div class="grid grid-cols-2 gap-3">
      <input type="number" step="any" name="qty" required placeholder="จำนวนหุ้น" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <input type="number" step="any" name="price" required placeholder="ราคา" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <div class="grid grid-cols-2 gap-3">
      <input type="date" name="exec_date" required value="{{ today }}" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <input type="time" name="exec_clock" required value="{{ now_time }}" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <input type="text" name="note" placeholder="หมายเหตุ" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    <input type="file" name="image" accept="image/*" class="w-full text-sm">
    <button class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded-lg font-medium w-full">บันทึกไม้เข้า</button>
  </form>

  <form method="post" action="{{ url_for('add_execution', order_id=order['id']) }}" enctype="multipart/form-data"
        class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-3">
    <h2 class="font-semibold mb-1">
      {{ 'ทยอยขาย (Scale Out)' if order['direction']=='long' else 'ทยอยซื้อคืน (Cover)' }}
    </h2>
    <input type="hidden" name="action" value="close">
    <div class="grid grid-cols-2 gap-3">
      <input type="number" step="any" name="qty" required placeholder="จำนวนหุ้น" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <input type="number" step="any" name="price" required placeholder="ราคา" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <div class="grid grid-cols-2 gap-3">
      <input type="date" name="exec_date" required value="{{ today }}" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
      <input type="time" name="exec_clock" required value="{{ now_time }}" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    </div>
    <input type="text" name="note" placeholder="หมายเหตุ" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
    <input type="file" name="image" accept="image/*" class="w-full text-sm">
    <button class="bg-rose-600 hover:bg-rose-500 px-4 py-2 rounded-lg font-medium w-full">บันทึกไม้ออก</button>
  </form>
</div>

<h2 class="font-semibold mb-3">ประวัติการเทรด (Executions)</h2>
<div class="space-y-3">
{% for e in executions %}
<div class="bg-slate-900 border border-slate-800 rounded-xl p-4 flex items-start justify-between gap-4">
  <div>
    <div class="flex items-center gap-2 mb-1">
      <span class="px-2 py-0.5 rounded text-xs {{ 'bg-blue-900 text-blue-300' if e['action']=='open' else 'bg-rose-900 text-rose-300' }}">
        {{ 'เข้าไม้' if e['action']=='open' else 'ออกไม้' }}
      </span>
      <span class="text-sm text-slate-400">{{ e['exec_time'] }}</span>
    </div>
    <div class="text-sm">จำนวน {{ '{:,.0f}'.format(e['qty']) }} หุ้น @ {{ '{:,.2f}'.format(e['price']) }}</div>
    {% if e['note'] %}<div class="text-xs text-slate-500 mt-1">{{ e['note'] }}</div>{% endif %}
  </div>
  {% if e['image_filename'] %}
  <a href="{{ url_for('uploaded_file', filename=e['image_filename']) }}" target="_blank">
    <img src="{{ url_for('uploaded_file', filename=e['image_filename']) }}" class="w-20 h-20 object-cover rounded-lg border border-slate-700">
  </a>
  {% endif %}
</div>
{% else %}
<p class="text-slate-500">ยังไม่มีรายการ</p>
{% endfor %}
</div>
""" + BASE_TAIL

SETTINGS_TEMPLATE = BASE_HEAD + """
<h1 class="text-xl font-bold mb-4">ตั้งค่า</h1>
<form method="post" action="{{ url_for('settings_page') }}" class="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4 max-w-md mb-8">
  <div>
    <label class="block text-sm text-slate-400 mb-1">ทุนเริ่มต้นของพอร์ต</label>
    <input type="number" step="any" name="initial_capital" value="{{ initial_capital }}" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2">
  </div>
  <button class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-lg font-medium">บันทึก</button>
</form>

<div class="bg-slate-900 border border-slate-800 rounded-xl p-6 max-w-2xl space-y-4">
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
      {% if google_spreadsheet_url %}
      — <a href="{{ google_spreadsheet_url }}" target="_blank" class="text-emerald-400 hover:underline">เปิด Spreadsheet</a>
      {% endif %}
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
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    orders = fetch_orders()
    portfolio = compute_portfolio()

    return render_template_string(
        INDEX_TEMPLATE,
        title="Dashboard",
        orders=orders,
        p=portfolio,
        equity_json=json.dumps(portfolio["equity_curve"]),
    )


@app.route("/order/new", methods=["GET", "POST"])
def new_order():
    now = datetime.now()
    if request.method == "POST":
        symbol = request.form["symbol"].strip().upper()
        direction = request.form.get("direction", "long")
        note = request.form.get("note", "").strip()
        qty = float(request.form["qty"])
        price = float(request.form["price"])
        exec_dt = f"{request.form['exec_date']} {request.form['exec_clock']}"

        db = get_db()
        cur = db.execute(
            "INSERT INTO orders (symbol, direction, note, created_at) VALUES (?, ?, ?, ?)",
            (symbol, direction, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        order_id = cur.lastrowid

        image_filename = save_image(request.files.get("image"))
        db.execute(
            "INSERT INTO executions (order_id, action, qty, price, exec_time, note, image_filename) "
            "VALUES (?, 'open', ?, ?, ?, ?, ?)",
            (order_id, qty, price, exec_dt, "", image_filename),
        )
        db.commit()
        flash_msg(f"เปิดออเดอร์ {symbol} เรียบร้อยแล้ว")
        return redirect(url_for("order_detail", order_id=order_id))

    return render_template_string(
        NEW_ORDER_TEMPLATE,
        title="New Order",
        today=now.strftime("%Y-%m-%d"),
        now_time=now.strftime("%H:%M"),
    )


@app.route("/order/<int:order_id>")
def order_detail(order_id):
    order = fetch_order(order_id)
    if order is None:
        return redirect(url_for("index"))
    executions = fetch_executions(order_id)
    stats = compute_order_stats(order, executions)
    now = datetime.now()
    return render_template_string(
        ORDER_DETAIL_TEMPLATE,
        title=order["symbol"],
        order=order,
        executions=executions,
        stats=stats,
        today=now.strftime("%Y-%m-%d"),
        now_time=now.strftime("%H:%M"),
    )


@app.route("/order/<int:order_id>/execution", methods=["POST"])
def add_execution(order_id):
    order = fetch_order(order_id)
    if order is None:
        return redirect(url_for("index"))

    action = request.form.get("action", "open")
    qty = float(request.form["qty"])
    price = float(request.form["price"])
    note = request.form.get("note", "").strip()
    exec_dt = f"{request.form['exec_date']} {request.form['exec_clock']}"
    image_filename = save_image(request.files.get("image"))

    db = get_db()
    db.execute(
        "INSERT INTO executions (order_id, action, qty, price, exec_time, note, image_filename) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, action, qty, price, exec_dt, note, image_filename),
    )
    db.commit()
    flash_msg("บันทึกรายการเรียบร้อยแล้ว")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        set_setting("initial_capital", request.form.get("initial_capital", "100000"))
        flash_msg("บันทึกการตั้งค่าเรียบร้อยแล้ว")
        return redirect(url_for("settings_page"))
    return render_template_string(
        SETTINGS_TEMPLATE,
        title="Settings",
        initial_capital=get_setting("initial_capital", "100000"),
        gspread_available=GSPREAD_AVAILABLE,
        google_credentials_json=get_setting("google_credentials_json", "") or "",
        google_spreadsheet_id=get_setting("google_spreadsheet_id", "") or "",
        google_share_email=get_setting("google_share_email", "") or "",
        google_last_backup=get_setting("google_last_backup", "") or "",
        google_spreadsheet_url=get_setting("google_spreadsheet_url", "") or "",
    )


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


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
