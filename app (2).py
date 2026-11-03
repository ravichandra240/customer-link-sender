"""
Customer Link Sender
--------------------
Save a customer -> a message with your website link is queued ->
it is sent automatically on the weekend (Saturday) via WhatsApp or SMS.

Run:  python app.py     then open http://localhost:5000
"""
import hmac
import os
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, redirect, render_template_string, request, url_for, flash

# ---------------- Configuration (set via environment variables) ----------------
WEBSITE_LINK = os.getenv("WEBSITE_LINK", "https://eleganthome.co.in/contact-us/")
MESSAGE_TEMPLATE = os.getenv(
    "MESSAGE_TEMPLATE",
    "Hello {name}, thank you for connecting with us! Visit our website: {link}",
)
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
SEND_HOUR = int(os.getenv("SEND_HOUR", "10"))  # Saturday send time (24h clock)
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")  # India

# DRY_RUN=true  -> messages are only logged, nothing is really sent.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Twilio (handles both SMS and WhatsApp)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM", "")  # e.g. +1415XXXXXXX
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. whatsapp:+14155238886

DB_PATH = os.getenv("DB_PATH", "customers.db")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")

# Password protection (HTTP Basic Auth). Set APP_PASSWORD to turn it on.
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return None
    auth = request.authorization
    if (
        auth
        and hmac.compare_digest(auth.username or "", APP_USERNAME)
        and hmac.compare_digest(auth.password or "", APP_PASSWORD)
    ):
        return None
    return Response(
        "Login required", 401, {"WWW-Authenticate": 'Basic realm="Customer Link Sender"'}
    )


# ---------------- Database ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                saved_at TEXT NOT NULL,
                send_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                sent_at TEXT,
                error TEXT
            )"""
        )


# ---------------- Scheduling logic ----------------
def next_send_time(now: datetime) -> datetime:
    """
    Weekend rule:
      - Mon-Fri          -> next Saturday at SEND_HOUR
      - Saturday before SEND_HOUR -> today at SEND_HOUR
      - Saturday after SEND_HOUR / Sunday -> send right away (it's already the weekend)
    """
    weekday = now.weekday()  # Mon=0 ... Sat=5, Sun=6
    send_today = now.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if weekday == 5 and now < send_today:
        return send_today
    if weekday in (5, 6):
        return now
    days_ahead = (5 - weekday) % 7
    return (now + timedelta(days=days_ahead)).replace(
        hour=SEND_HOUR, minute=0, second=0, microsecond=0
    )


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:  # local number without country code
        return f"+{DEFAULT_COUNTRY_CODE}{digits}"
    return "+" + digits


# ---------------- Sending ----------------
def build_message(name: str) -> str:
    return MESSAGE_TEMPLATE.format(name=name, link=WEBSITE_LINK)


def send_via_twilio(phone: str, channel: str, body: str):
    if not (TWILIO_SID and TWILIO_TOKEN):
        raise RuntimeError("Twilio credentials are not set")
    if channel == "whatsapp":
        sender, to = TWILIO_WHATSAPP_FROM, f"whatsapp:{phone}"
    else:
        sender, to = TWILIO_SMS_FROM, phone
    if not sender:
        raise RuntimeError(f"No Twilio 'from' number configured for {channel}")
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"From": sender, "To": to, "Body": body},
        timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Twilio error {resp.status_code}: {resp.text[:200]}")


def deliver(row) -> None:
    body = build_message(row["name"])
    try:
        if DRY_RUN:
            print(f"[DRY RUN] {row['channel']} -> {row['phone']}: {body}")
        else:
            send_via_twilio(row["phone"], row["channel"], body)
        status, error = "sent", None
    except Exception as exc:  # noqa: BLE001
        status, error = "failed", str(exc)
        print(f"[ERROR] customer {row['id']}: {exc}")
    with db() as conn:
        conn.execute(
            "UPDATE customers SET status=?, sent_at=?, error=? WHERE id=?",
            (status, datetime.now(TIMEZONE).isoformat(timespec="seconds"), error, row["id"]),
        )


def scheduler_loop():
    while True:
        try:
            now = datetime.now(TIMEZONE).isoformat(timespec="seconds")
            with db() as conn:
                due = conn.execute(
                    "SELECT * FROM customers WHERE status='pending' AND send_at <= ?", (now,)
                ).fetchall()
            for row in due:
                deliver(row)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] {exc}")
        time.sleep(30)


# ---------------- Web UI ----------------
PAGE = """
<!doctype html>
<title>Customer Link Sender</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:system-ui,sans-serif;max-width:900px;margin:30px auto;padding:0 16px;color:#222}
  h1{margin-bottom:4px} .sub{color:#666;margin-top:0}
  form{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0;padding:16px;background:#f5f7fa;border-radius:10px}
  input,select,button{padding:10px;font-size:15px;border:1px solid #ccc;border-radius:6px}
  button{background:#1a73e8;color:#fff;border:none;cursor:pointer}
  button.small{padding:5px 10px;font-size:13px}
  button.grey{background:#6b7280}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left}
  .pending{color:#b45309}.sent{color:#15803d}.failed{color:#b91c1c}
  .flash{background:#e6f4ea;padding:10px;border-radius:6px;margin-bottom:10px}
  .mode{font-size:13px;padding:4px 8px;border-radius:4px;background:#fff3cd;display:inline-block}
</style>
<h1>Customer Link Sender</h1>
<p class="sub">Save a customer — your website link is sent on the weekend.
  <span class="mode">{{ 'DRY RUN (no real messages)' if dry else 'LIVE sending' }}</span></p>
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
<form method="post" action="{{ url_for('add') }}">
  <input name="name" placeholder="Customer name" required>
  <input name="phone" placeholder="Mobile number" required>
  <select name="channel">
    <option value="whatsapp">WhatsApp</option>
    <option value="sms">SMS</option>
  </select>
  <button>Save customer</button>
</form>
<table>
  <tr><th>Name</th><th>Phone</th><th>Via</th><th>Scheduled</th><th>Status</th><th></th></tr>
  {% for c in customers %}
  <tr>
    <td>{{ c['name'] }}</td><td>{{ c['phone'] }}</td><td>{{ c['channel'] }}</td>
    <td>{{ c['send_at'][:16].replace('T',' ') }}</td>
    <td class="{{ c['status'] }}">{{ c['status'] }}{% if c['error'] %}<br><small>{{ c['error'] }}</small>{% endif %}</td>
    <td>
      <a href="{{ wa_link(c) }}" target="_blank"><button class="small grey" type="button">Open WhatsApp</button></a>
      {% if c['status'] != 'sent' %}
      <form style="display:inline;padding:0;background:none;margin:0" method="post" action="{{ url_for('send_now', cid=c['id']) }}">
        <button class="small">Send now</button></form>
      {% endif %}
    </td>
  </tr>
  {% else %}<tr><td colspan="6">No customers yet.</td></tr>{% endfor %}
</table>
"""


def wa_link(row):
    text = urllib.parse.quote(build_message(row["name"]))
    return f"https://wa.me/{row['phone'].lstrip('+')}?text={text}"


@app.route("/")
def index():
    with db() as conn:
        customers = conn.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()
    return render_template_string(PAGE, customers=customers, dry=DRY_RUN, wa_link=wa_link)


@app.route("/add", methods=["POST"])
def add():
    now = datetime.now(TIMEZONE)
    send_at = next_send_time(now)
    with db() as conn:
        conn.execute(
            "INSERT INTO customers (name, phone, channel, saved_at, send_at) VALUES (?,?,?,?,?)",
            (
                request.form["name"].strip(),
                normalize_phone(request.form["phone"]),
                request.form["channel"],
                now.isoformat(timespec="seconds"),
                send_at.isoformat(timespec="seconds"),
            ),
        )
    flash(f"Customer saved. Message will be sent on {send_at:%A, %d %b %Y %H:%M}.")
    return redirect(url_for("index"))


@app.route("/send-now/<int:cid>", methods=["POST"])
def send_now(cid):
    with db() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if row:
        deliver(row)
        flash("Message processed.")
    return redirect(url_for("index"))


init_db()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
