# Customer Link Sender

Save a customer → your website link is automatically sent to them on WhatsApp or SMS at the **end of the week (Saturday)**.

## How it works
- Saved Monday–Friday → sent the coming **Saturday at 10:00 AM** (India time).
- Saved Saturday before 10:00 → sent that day at 10:00.
- Saved Saturday after 10:00 or Sunday → sent right away (already weekend).
- A background scheduler checks every 30 seconds and sends due messages.
- Each row has **Send now** and **Open WhatsApp** (manual click-to-send) buttons.

## Run it
```bash
pip install flask requests
export WEBSITE_LINK="https://slhomes.in/dashboard"  # already the default
python app.py
```
Open http://localhost:5000. By default `DRY_RUN=true`, so messages are only printed in the terminal.

## Customer service page
Each saved customer gets a private link (`/c/<random code>`) inside their weekend message. On that page they can only:
- send a **service request** (service type, message, best time to contact), and
- see your **contact details** (call, WhatsApp, email).

They cannot see other customers or your dashboard. Requests show up in your dashboard under **Service requests** (New / In progress / Done, plus a Reply on WhatsApp button).

Settings: `PUBLIC_BASE_URL` (the address customers' links start with, e.g. `http://localhost:5000` locally or your hosted address), `BUSINESS_NAME`, `CONTACT_PHONE`, `CONTACT_WHATSAPP`, `CONTACT_EMAIL`. Each customer can send up to 5 requests per day.

Important: set `PUBLIC_BASE_URL` to your real hosted address before sending real messages, or customers get a `localhost` link that will not open on their phones.

## Public "request a service" page (no link needed)
Anyone can open `/service` (for example `https://your-app/service`) and request a service without being added by you first. It collects name, mobile, email, address/area, service type, message and best time. They appear in your dashboard as **Self-registered**, and their request shows under **Service requests**. If the mobile number already exists, the request is added to that customer instead of creating a duplicate. Self-registered people do not get the weekend message.

Share the `/service` link on your website, WhatsApp status or as a QR code. The dashboard shows it near the top.

Your dashboard shows each customer's email, address and source, and the **Download customers (CSV)** button exports everything to Excel.

Spam protection: a hidden form field catches bots, each phone number can send 5 requests a day, and at most 100 new people can register per day.

## Send real messages (Twilio)
WhatsApp and SMS cannot be sent for free automatically; you need a provider. Twilio handles both:
1. Create a Twilio account, get Account SID and Auth Token.
2. Get an SMS-capable number, and enable a WhatsApp sender (sandbox for testing, approved WhatsApp Business number for production).
3. Set:
```bash
export DRY_RUN=false
export TWILIO_ACCOUNT_SID=ACxxxxxxxx
export TWILIO_AUTH_TOKEN=xxxxxxxx
export TWILIO_SMS_FROM=+1XXXXXXXXXX
export TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```
Notes:
- WhatsApp business-initiated messages usually need a pre-approved **message template**; free-form text works only within 24 hours of the customer's last message. Get your template approved in Twilio/Meta for production use.
- In India, SMS needs DLT registration of sender ID and template. Check with your provider.
- Only message customers who have agreed to receive messages.

## Settings (environment variables)
`WEBSITE_LINK`, `MESSAGE_TEMPLATE` (use `{name}` and `{link}`), `SEND_HOUR`, `TIMEZONE`, `DEFAULT_COUNTRY_CODE`, `DRY_RUN`, `DB_PATH`, `PORT`.

## Deploy
Needs a host that keeps running (so the scheduler works), e.g. Render, Railway, or a VPS. Use a persistent disk or Postgres for the database.
