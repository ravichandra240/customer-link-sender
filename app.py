"""
Customer Link Sender
--------------------
You save a customer -> on the weekend (Saturday) they get a WhatsApp/SMS with a
PERSONAL link. That link opens a small page where the customer can only:
  * ask for a service (repair, site visit, ...), and
  * see how to contact you.
They cannot see any other customer or your dashboard.

Requests appear in your dashboard under "Service requests".

Run:  python app.py     then open http://localhost:5000
"""
import hmac
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import (
    Flask, Response, abort, flash, redirect, render_template_string, request, url_for,
)

# ---------------- Configuration (set via environment variables) ----------------
WEBSITE_LINK = os.getenv("WEBSITE_LINK", "https://eleganthome.co.in/contact-us/")
# Public address of THIS app (used to build each customer's personal link).
# Local test: http://localhost:5000   Hosted: https://your-app.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")
MESSAGE_TEMPLATE = os.getenv(
    "MESSAGE_TEMPLATE",
    "Hello {name}, thank you for connecting with us! "
    "Request a service or contact us here: {customer_link}\n"
    "Our website: {link}",
)
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
SEND_HOUR = int(os.getenv("SEND_HOUR", "10"))  # Saturday send time (24h clock)
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")  # India

# Shown to customers on their page. Leave blank to hide.
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Elegant Home")
CONTACT_PHONE = os.getenv("CONTACT_PHONE", "")        # e.g. +919876543210
CONTACT_WHATSAPP = os.getenv("CONTACT_WHATSAPP", "")  # e.g. +919876543210
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")

SERVICE_TYPES = ["General enquiry", "Repair", "Interior work", "Site visit", "Other"]
MAX_REQUESTS_PER_DAY = 5

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
    """Owner pages need the password. Customer pages (/c/<token>...) are public."""
    if request.path.startswith("/c/"):
        return None
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
        # Upgrade older databases: add the personal-link token column.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(customers)")]
        if "token" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN token TEXT")
        for r in conn.execute("SELECT id FROM customers WHERE token IS NULL OR token=''").fetchall():
            conn.execute("UPDATE customers SET token=? WHERE id=?", (new_token(), r["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_token ON customers(token)")

        conn.execute(
            """CREATE TABLE IF NOT EXISTS service_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                service_type TEXT NOT NULL,
                message TEXT NOT NULL,
                preferred_time TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )"""
        )


def new_token() -> str:
    return secrets.token_urlsafe(12)


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
def customer_link(row) -> str:
    return f"{PUBLIC_BASE_URL}/c/{row['token']}"


def build_message(row) -> str:
    return MESSAGE_TEMPLATE.format(
        name=row["name"], link=WEBSITE_LINK, customer_link=customer_link(row)
    )


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
    body = build_message(row)
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


# ---------------- Owner dashboard ----------------
PAGE = """
<!doctype html>
<title>Customer Link Sender</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:system-ui,sans-serif;max-width:1000px;margin:30px auto;padding:0 16px;color:#222}
  h1{margin-bottom:4px} h2{margin-top:36px} .sub{color:#666;margin-top:0}
  form.add{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0;padding:16px;background:#f5f7fa;border-radius:10px}
  input,select,button{padding:10px;font-size:15px;border:1px solid #ccc;border-radius:6px}
  button{background:#1a73e8;color:#fff;border:none;cursor:pointer}
  button.small{padding:5px 10px;font-size:13px}
  button.grey{background:#6b7280} button.green{background:#15803d}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}
  .pending,.new{color:#b45309}.sent,.done{color:#15803d}.failed{color:#b91c1c}.progress{color:#1d4ed8}
  .flash{background:#e6f4ea;padding:10px;border-radius:6px;margin-bottom:10px}
  .mode{font-size:13px;padding:4px 8px;border-radius:4px;background:#fff3cd;display:inline-block}
  .badge{background:#b91c1c;color:#fff;border-radius:10px;padding:1px 8px;font-size:13px}
  form.inline{display:inline;padding:0;margin:0;background:none}
  .msg{white-space:pre-wrap;max-width:320px}
</style>
<h1>Customer Link Sender</h1>
<p class="sub">Save a customer: they get their personal link on the weekend and can ask for a service.
  <span class="mode">{{ 'DRY RUN (no real messages)' if dry else 'LIVE sending' }}</span></p>
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

<h2>Service requests {% if new_count %}<span class="badge">{{ new_count }} new</span>{% endif %}</h2>
<table>
  <tr><th>When</th><th>Customer</th><th>Service</th><th>Message</th><th>Preferred time</th><th>Status</th><th></th></tr>
  {% for r in reqs %}
  <tr>
    <td>{{ r['created_at'][:16].replace('T',' ') }}</td>
    <td>{{ r['name'] }}<br><small>{{ r['phone'] }}</small></td>
    <td>{{ r['service_type'] }}</td>
    <td class="msg">{{ r['message'] }}</td>
    <td>{{ r['preferred_time'] or '-' }}</td>
    <td class="{{ r['status'] }}">{{ {'new':'New','progress':'In progress','done':'Done'}[r['status']] }}</td>
    <td>
      <a href="{{ reply_link(r) }}" target="_blank"><button class="small grey" type="button">Reply on WhatsApp</button></a>
      {% if r['status'] == 'new' %}
      <form class="inline" method="post" action="{{ url_for('request_status', rid=r['id'], status='progress') }}"><button class="small">In progress</button></form>
      {% endif %}
      {% if r['status'] != 'done' %}
      <form class="inline" method="post" action="{{ url_for('request_status', rid=r['id'], status='done') }}"><button class="small green">Done</button></form>
      {% endif %}
    </td>
  </tr>
  {% else %}<tr><td colspan="7">No service requests yet.</td></tr>{% endfor %}
</table>

<h2>Customers</h2>
<form class="add" method="post" action="{{ url_for('add') }}">
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
      <a href="{{ cust_link(c) }}" target="_blank"><button class="small grey" type="button">Customer page</button></a>
      {% if c['status'] != 'sent' %}
      <form class="inline" method="post" action="{{ url_for('send_now', cid=c['id']) }}">
        <button class="small">Send now</button></form>
      {% endif %}
    </td>
  </tr>
  {% else %}<tr><td colspan="6">No customers yet.</td></tr>{% endfor %}
</table>
"""

# ---------------- Customer page (public, personal link) ----------------
CUSTOMER_PAGE = """
<!doctype html>
<title>{{ business }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<style>
  body{font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;padding:20px 16px 40px;color:#222;background:#fafafa}
  h1{font-size:22px;margin:8px 0 2px} .sub{color:#666;margin-top:0}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:16px 0}
  label{display:block;font-weight:600;margin:12px 0 4px;font-size:14px}
  input,select,textarea{width:100%;box-sizing:border-box;padding:11px;font-size:16px;border:1px solid #ccc;border-radius:8px;font-family:inherit}
  textarea{min-height:100px}
  button.go{margin-top:16px;width:100%;padding:13px;font-size:16px;background:#1a73e8;color:#fff;border:none;border-radius:8px;cursor:pointer}
  .flash{background:#e6f4ea;padding:12px;border-radius:8px}
  .err{background:#fde8e8}
  .contact a{display:block;padding:12px;margin:8px 0;border-radius:8px;text-align:center;text-decoration:none;font-weight:600;color:#fff;background:#1a73e8}
  .contact a.wa{background:#15803d} .contact a.mail{background:#6b7280}
  table{width:100%;border-collapse:collapse;font-size:14px} td{padding:6px 4px;border-bottom:1px solid #eee}
  .new{color:#b45309}.progress{color:#1d4ed8}.done{color:#15803d}
</style>
<h1>Hello {{ c['name'] }}</h1>
<p class="sub">Welcome to {{ business }}. Tell us what you need and we will get back to you.</p>
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}<div class="flash {{ 'err' if cat == 'error' else '' }}">{{ m }}</div>{% endfor %}
{% endwith %}

<div class="card">
  <strong>Request a service</strong>
  <form method="post" action="{{ url_for('customer_request', token=c['token']) }}">
    <label for="service_type">Service</label>
    <select id="service_type" name="service_type" required>
      {% for s in services %}<option>{{ s }}</option>{% endfor %}
    </select>
    <label for="message">What do you need?</label>
    <textarea id="message" name="message" maxlength="1000" required placeholder="Tell us a little about it"></textarea>
    <label for="preferred_time">Best time to contact you (optional)</label>
    <input id="preferred_time" name="preferred_time" maxlength="100" placeholder="e.g. Tomorrow after 5 pm">
    <button class="go">Send request</button>
  </form>
</div>

{% if phone or whatsapp or email %}
<div class="card contact">
  <strong>Contact us</strong>
  {% if phone %}<a href="tel:{{ phone }}">Call {{ phone }}</a>{% endif %}
  {% if whatsapp %}<a class="wa" href="https://wa.me/{{ whatsapp.lstrip('+') }}">Chat on WhatsApp</a>{% endif %}
  {% if email %}<a class="mail" href="mailto:{{ email }}">Email {{ email }}</a>{% endif %}
</div>
{% endif %}

{% if history %}
<div class="card">
  <strong>Your requests</strong>
  <table>
    {% for r in history %}
    <tr><td>{{ r['created_at'][:10] }}</td><td>{{ r['service_type'] }}</td>
        <td class="{{ r['status'] }}">{{ {'new':'Received','progress':'In progress','done':'Done'}[r['status']] }}</td></tr>
    {% endfor %}
  </table>
</div>
{% endif %}
"""


def wa_link(row):
    text = urllib.parse.quote(build_message(row))
    return f"https://wa.me/{row['phone'].lstrip('+')}?text={text}"


def reply_link(r):
    text = urllib.parse.quote(
        f"Hello {r['name']}, thank you for your {r['service_type']} request. "
    )
    return f"https://wa.me/{r['phone'].lstrip('+')}?text={text}"


@app.route("/")
def index():
    with db() as conn:
        customers = conn.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()
        reqs = conn.execute(
            """SELECT r.*, c.name, c.phone FROM service_requests r
               JOIN customers c ON c.id = r.customer_id
               ORDER BY (r.status='done'), r.id DESC LIMIT 200"""
        ).fetchall()
    new_count = sum(1 for r in reqs if r["status"] == "new")
    return render_template_string(
        PAGE, customers=customers, reqs=reqs, new_count=new_count, dry=DRY_RUN,
        wa_link=wa_link, reply_link=reply_link, cust_link=customer_link,
    )


@app.route("/add", methods=["POST"])
def add():
    now = datetime.now(TIMEZONE)
    send_at = next_send_time(now)
    with db() as conn:
        conn.execute(
            "INSERT INTO customers (name, phone, channel, saved_at, send_at, token) VALUES (?,?,?,?,?,?)",
            (
                request.form["name"].strip(),
                normalize_phone(request.form["phone"]),
                request.form["channel"],
                now.isoformat(timespec="seconds"),
                send_at.isoformat(timespec="seconds"),
                new_token(),
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


@app.route("/requests/<int:rid>/<status>", methods=["POST"])
def request_status(rid, status):
    if status not in ("progress", "done"):
        abort(400)
    with db() as conn:
        conn.execute("UPDATE service_requests SET status=? WHERE id=?", (status, rid))
    return redirect(url_for("index"))


# ---- public customer routes ----
def customer_by_token(token):
    with db() as conn:
        return conn.execute("SELECT * FROM customers WHERE token=?", (token,)).fetchone()


@app.route("/c/<token>")
def customer_page(token):
    c = customer_by_token(token)
    if not c:
        abort(404)
    with db() as conn:
        history = conn.execute(
            "SELECT * FROM service_requests WHERE customer_id=? ORDER BY id DESC LIMIT 10",
            (c["id"],),
        ).fetchall()
    resp = Response(
        render_template_string(
            CUSTOMER_PAGE, c=c, history=history, services=SERVICE_TYPES,
            business=BUSINESS_NAME, phone=CONTACT_PHONE,
            whatsapp=CONTACT_WHATSAPP, email=CONTACT_EMAIL,
        )
    )
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.route("/c/<token>/request", methods=["POST"])
def customer_request(token):
    c = customer_by_token(token)
    if not c:
        abort(404)
    service_type = request.form.get("service_type", "")
    message = request.form.get("message", "").strip()[:1000]
    preferred = request.form.get("preferred_time", "").strip()[:100]
    if service_type not in SERVICE_TYPES or not message:
        flash("Please choose a service and describe what you need.", "error")
        return redirect(url_for("customer_page", token=token))

    now = datetime.now(TIMEZONE)
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    with db() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM service_requests WHERE customer_id=? AND created_at>=?",
            (c["id"], since),
        ).fetchone()[0]
        if recent >= MAX_REQUESTS_PER_DAY:
            flash("You have sent several requests today. We will contact you soon.", "error")
            return redirect(url_for("customer_page", token=token))
        conn.execute(
            "INSERT INTO service_requests (customer_id, service_type, message, preferred_time, created_at)"
            " VALUES (?,?,?,?,?)",
            (c["id"], service_type, message, preferred, now.isoformat(timespec="seconds")),
        )
    flash("Thank you! Your request has been sent. We will contact you shortly.")
    return redirect(url_for("customer_page", token=token))


init_db()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
