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
import csv
import hmac
import io
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
MAX_SELF_SIGNUPS_PER_DAY = 100  # overall cap on new people via the public form

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
    """Owner pages need the password. Customer pages (/c/<token>..., /service) are public."""
    if request.path.startswith("/c/") or request.path == "/service" or request.path.startswith("/service/"):
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
        for col, ddl in (
            ("source", "TEXT NOT NULL DEFAULT 'admin'"),
            ("email", "TEXT"),
            ("address", "TEXT"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE customers ADD COLUMN {col} {ddl}")

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

<div class="flash" style="background:#eef4ff">
  <strong>Public link for anyone to request a service:</strong>
  <a href="{{ service_url }}" target="_blank">{{ service_url }}</a><br>
  <small>Share it on your website, WhatsApp or as a QR code. People who use it appear below as "Self-registered".</small>
</div>
<h2>Customers <a href="{{ url_for('export_csv') }}"><button class="small grey" type="button">Download customers (CSV)</button></a></h2>
<form class="add" method="post" action="{{ url_for('add') }}">
  <input name="name" placeholder="Customer name" required>
  <input name="phone" placeholder="Mobile number" required>
  <input name="email" placeholder="Email (optional)" type="email">
  <input name="address" placeholder="Address / area (optional)">
  <select name="channel">
    <option value="whatsapp">WhatsApp</option>
    <option value="sms">SMS</option>
  </select>
  <button>Save customer</button>
</form>
<table>
  <tr><th>Name</th><th>Phone</th><th>Email / Address</th><th>Source</th><th>Via</th><th>Scheduled</th><th>Status</th><th></th></tr>
  {% for c in customers %}
  <tr>
    <td>{{ c['name'] }}</td><td>{{ c['phone'] }}</td>
    <td>{{ c['email'] or '-' }}<br><small>{{ c['address'] or '' }}</small></td>
    <td>{{ 'Self-registered' if c['source'] == 'self' else 'Added by you' }}</td>
    {% if c['source'] == 'self' %}
    <td>-</td><td>-</td><td>-</td>
    {% else %}
    <td>{{ c['channel'] }}</td>
    <td>{{ c['send_at'][:16].replace('T',' ') }}</td>
    <td class="{{ c['status'] }}">{{ c['status'] }}{% if c['error'] %}<br><small>{{ c['error'] }}</small>{% endif %}</td>
    {% endif %}
    <td>
      <a href="{{ wa_link(c) }}" target="_blank"><button class="small grey" type="button">Open WhatsApp</button></a>
      <a href="{{ cust_link(c) }}" target="_blank"><button class="small grey" type="button">Customer page</button></a>
      {% if c['source'] != 'self' and c['status'] != 'sent' %}
      <form class="inline" method="post" action="{{ url_for('send_now', cid=c['id']) }}">
        <button class="small">Send now</button></form>
      {% endif %}
    </td>
  </tr>
  {% else %}<tr><td colspan="8">No customers yet.</td></tr>{% endfor %}
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
        service_url=f"{PUBLIC_BASE_URL}/service",
    )


@app.route("/add", methods=["POST"])
def add():
    now = datetime.now(TIMEZONE)
    send_at = next_send_time(now)
    with db() as conn:
        conn.execute(
            "INSERT INTO customers (name, phone, channel, saved_at, send_at, token, email, address, source)"
            " VALUES (?,?,?,?,?,?,?,?, 'admin')",
            (
                request.form["name"].strip(),
                normalize_phone(request.form["phone"]),
                request.form["channel"],
                now.isoformat(timespec="seconds"),
                send_at.isoformat(timespec="seconds"),
                new_token(),
                request.form.get("email", "").strip()[:200],
                request.form.get("address", "").strip()[:300],
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


# ---------------- Public "request a service" page (no link needed) ----------------
SERVICE_PAGE = """
<!doctype html>
<title>Request a service - {{ business }}</title>
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
  .flash{background:#e6f4ea;padding:12px;border-radius:8px;margin:12px 0}
  .err{background:#fde8e8}
  .contact a{display:block;padding:12px;margin:8px 0;border-radius:8px;text-align:center;text-decoration:none;font-weight:600;color:#fff;background:#1a73e8}
  .contact a.wa{background:#15803d} .contact a.mail{background:#6b7280}
  .hp{position:absolute;left:-5000px;height:0;overflow:hidden}
</style>
<h1>{{ business }}</h1>
<p class="sub">Request a service and we will contact you.</p>
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}<div class="flash {{ 'err' if cat == 'error' else '' }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% if thanks %}
<div class="card"><strong>Thank you!</strong><br>Your request has been received. We will contact you shortly.</div>
{% else %}
<div class="card">
  <form method="post" action="{{ url_for('service_submit') }}">
    <label for="name">Your name</label>
    <input id="name" name="name" maxlength="100" required>
    <label for="phone">Mobile number</label>
    <input id="phone" name="phone" type="tel" maxlength="20" required placeholder="e.g. 98765 43210">
    <label for="email">Email (optional)</label>
    <input id="email" name="email" type="email" maxlength="200">
    <label for="address">Address / area (optional)</label>
    <input id="address" name="address" maxlength="300">
    <label for="service_type">Service</label>
    <select id="service_type" name="service_type" required>
      {% for s in services %}<option>{{ s }}</option>{% endfor %}
    </select>
    <label for="message">What do you need?</label>
    <textarea id="message" name="message" maxlength="1000" required></textarea>
    <label for="preferred_time">Best time to contact you (optional)</label>
    <input id="preferred_time" name="preferred_time" maxlength="100" placeholder="e.g. Tomorrow after 5 pm">
    <div class="hp" aria-hidden="true"><label>Leave empty<input name="website" tabindex="-1" autocomplete="off"></label></div>
    <button class="go">Send request</button>
  </form>
</div>
{% endif %}
{% if phone or whatsapp or email %}
<div class="card contact">
  <strong>Or contact us directly</strong>
  {% if phone %}<a href="tel:{{ phone }}">Call {{ phone }}</a>{% endif %}
  {% if whatsapp %}<a class="wa" href="https://wa.me/{{ whatsapp.lstrip('+') }}">Chat on WhatsApp</a>{% endif %}
  {% if email %}<a class="mail" href="mailto:{{ email }}">Email {{ email }}</a>{% endif %}
</div>
{% endif %}
"""


def render_service_page(thanks=False):
    resp = Response(
        render_template_string(
            SERVICE_PAGE, thanks=thanks, services=SERVICE_TYPES, business=BUSINESS_NAME,
            phone=CONTACT_PHONE, whatsapp=CONTACT_WHATSAPP, email=CONTACT_EMAIL,
        )
    )
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.route("/service")
def service_page():
    return render_service_page()


@app.route("/service/thanks")
def service_thanks():
    return render_service_page(thanks=True)


@app.route("/service", methods=["POST"])
def service_submit():
    # Hidden field: real people leave it empty, bots fill it. Pretend success.
    if request.form.get("website"):
        return redirect(url_for("service_thanks"))

    name = request.form.get("name", "").strip()[:100]
    digits = "".join(ch for ch in request.form.get("phone", "") if ch.isdigit())
    service_type = request.form.get("service_type", "")
    message = request.form.get("message", "").strip()[:1000]
    if not name or not (8 <= len(digits) <= 15) or service_type not in SERVICE_TYPES or not message:
        flash("Please enter your name, a valid mobile number, a service and a message.", "error")
        return render_service_page()
    phone = normalize_phone(request.form.get("phone", ""))
    email = request.form.get("email", "").strip()[:200]
    address = request.form.get("address", "").strip()[:300]
    preferred = request.form.get("preferred_time", "").strip()[:100]

    now = datetime.now(TIMEZONE)
    now_s = now.isoformat(timespec="seconds")
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    with db() as conn:
        c = conn.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        if c:
            recent = conn.execute(
                "SELECT COUNT(*) FROM service_requests WHERE customer_id=? AND created_at>=?",
                (c["id"], since),
            ).fetchone()[0]
            if recent >= MAX_REQUESTS_PER_DAY:
                return redirect(url_for("service_thanks"))
            # Fill in details they did not have before; never overwrite what you saved.
            conn.execute(
                "UPDATE customers SET email=CASE WHEN COALESCE(email,'')='' THEN ? ELSE email END,"
                " address=CASE WHEN COALESCE(address,'')='' THEN ? ELSE address END WHERE id=?",
                (email, address, c["id"]),
            )
            cid = c["id"]
        else:
            signups = conn.execute(
                "SELECT COUNT(*) FROM customers WHERE source='self' AND saved_at>=?", (since,)
            ).fetchone()[0]
            if signups >= MAX_SELF_SIGNUPS_PER_DAY:
                flash("We are receiving many requests right now. Please call us instead.", "error")
                return render_service_page()
            cur = conn.execute(
                "INSERT INTO customers (name, phone, channel, saved_at, send_at, status, token,"
                " email, address, source) VALUES (?,?,?,?,?,?,?,?,?, 'self')",
                (name, phone, "whatsapp", now_s, now_s, "self", new_token(), email, address),
            )
            cid = cur.lastrowid
        conn.execute(
            "INSERT INTO service_requests (customer_id, service_type, message, preferred_time, created_at)"
            " VALUES (?,?,?,?,?)",
            (cid, service_type, message, preferred, now_s),
        )
    return redirect(url_for("service_thanks"))


def _csv_safe(value):
    """Stop spreadsheet formula injection from text people typed."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@app.route("/export.csv")
def export_csv():
    with db() as conn:
        rows = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Phone", "Email", "Address", "Source", "Saved at", "Requests"])
    with db() as conn:
        for r in rows:
            n = conn.execute(
                "SELECT COUNT(*) FROM service_requests WHERE customer_id=?", (r["id"],)
            ).fetchone()[0]
            w.writerow([
                _csv_safe(r["name"]), r["phone"], _csv_safe(r["email"]), _csv_safe(r["address"]),
                "Self-registered" if r["source"] == "self" else "Added by you",
                r["saved_at"][:16].replace("T", " "), n,
            ])
    # utf-8 with BOM so Excel shows names correctly
    data = "﻿" + out.getvalue()
    return Response(
        data, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


init_db()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
