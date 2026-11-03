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
