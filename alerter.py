"""
HotelWatch — alerter.py
Sends price drop alert emails via Microsoft Graph API.
Uses the app's MS credentials (client credentials flow) — no App Password needed.
"""

import os
import logging
from datetime import date

import httpx

log = logging.getLogger("hotelwatch.alerter")

MS_TENANT_ID    = os.getenv("MS_TENANT_ID", "common")
MS_CLIENT_ID    = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
SMTP_FROM        = os.getenv("SMTP_FROM", "gene@therocklab.net")
SMTP_USER        = os.getenv("SMTP_USER", "gene@therocklab.net")


def _get_graph_token() -> str:
    """Obtain an app-only access token via client credentials flow."""
    url = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
    resp = httpx.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "scope":         "https://graph.microsoft.com/.default",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _send_via_graph(to: str, subject: str, html: str, plain: str):
    """Send an email using Microsoft Graph API sendMail endpoint."""
    token = _get_graph_token()
    sender = SMTP_USER  # must match the mailbox the app has Mail.Send for

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html,
            },
            "toRecipients": [{"emailAddress": {"address": to}}],
            "from": {"emailAddress": {"address": sender}},
        },
        "saveToSentItems": "false",
    }

    resp = httpx.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()


def send_price_drop_alert(user, booking, price_check):
    """Send a price drop alert email to the user."""
    recipient = user.alert_email or user.email
    if not recipient:
        log.warning(f"No alert email for user {user.id}, skipping alert")
        return

    savings     = price_check.price_drop or 0
    current     = price_check.current_price or 0
    booked      = price_check.booked_price
    pct_savings = round((savings / booked) * 100, 1) if booked else 0

    days_until_deadline = None
    if booking.cancellation_deadline:
        try:
            deadline = date.fromisoformat(booking.cancellation_deadline)
            days_until_deadline = (deadline - date.today()).days
        except ValueError:
            pass

    deadline_html = ""
    if days_until_deadline is not None:
        urgency_color = (
            "#e74c3c" if days_until_deadline <= 2 else
            "#f39c12" if days_until_deadline <= 5 else
            "#27ae60"
        )
        deadline_html = f"""
        <tr>
            <td style="padding:8px 0;color:#666;font-size:14px;">⏰ Cancellation deadline</td>
            <td style="padding:8px 0;font-size:14px;font-weight:bold;color:{urgency_color};">
                {booking.cancellation_deadline} ({days_until_deadline} day{'s' if days_until_deadline != 1 else ''} left)
            </td>
        </tr>"""

    booking_url = getattr(booking, 'booking_url', None) or '#'

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;margin:0;padding:20px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

    <div style="background:linear-gradient(135deg,#2c3e50,#3498db);padding:32px 32px 24px;text-align:center;">
      <div style="font-size:36px;">🏨</div>
      <h1 style="color:#fff;margin:8px 0 4px;font-size:22px;font-weight:700;">Price Drop Alert</h1>
      <p style="color:rgba(255,255,255,0.85);margin:0;font-size:14px;">HotelWatch found a better rate</p>
    </div>

    <div style="background:#27ae60;color:#fff;text-align:center;padding:16px;">
      <span style="font-size:28px;font-weight:800;">Save ${savings:.0f}</span>
      <span style="font-size:16px;opacity:0.9;margin-left:8px;">({pct_savings}% off your booked rate)</span>
    </div>

    <div style="padding:28px 32px;">
      <h2 style="margin:0 0 4px;font-size:18px;color:#2c3e50;">{booking.hotel_name}</h2>
      <p style="margin:0 0 20px;color:#777;font-size:14px;">
        {booking.check_in} → {booking.check_out} &nbsp;·&nbsp; {booking.num_nights} night{'s' if booking.num_nights != 1 else ''}
        {f' &nbsp;·&nbsp; {booking.room_type}' if booking.room_type else ''}
      </p>

      <table style="width:100%;border-collapse:collapse;border-top:1px solid #eee;">
        <tr>
          <td style="padding:12px 0;color:#666;font-size:14px;">Your booked rate</td>
          <td style="padding:12px 0;font-size:14px;text-decoration:line-through;color:#999;">${booked:.0f}</td>
        </tr>
        <tr style="background:#f8fff8;">
          <td style="padding:12px 0 12px 8px;color:#27ae60;font-size:15px;font-weight:600;">New rate available</td>
          <td style="padding:12px 0;font-size:18px;font-weight:800;color:#27ae60;">${current:.0f}</td>
        </tr>
        {deadline_html}
      </table>

      <div style="text-align:center;margin:24px 0 16px;">
        <a href="{booking_url}"
           style="display:inline-block;background:#3498db;color:#fff;text-decoration:none;
                  padding:14px 32px;border-radius:8px;font-size:15px;font-weight:600;">
          View Current Rate →
        </a>
      </div>

      <p style="font-size:13px;color:#888;text-align:center;margin:0;line-height:1.6;">
        To save, cancel your current booking and rebook at the lower rate<br>
        before your cancellation deadline.
      </p>
    </div>

    <div style="background:#f9f9f9;padding:16px 32px;border-top:1px solid #eee;text-align:center;">
      <p style="font-size:12px;color:#aaa;margin:0;">— HotelWatch 🏨</p>
    </div>

  </div>
</body>
</html>"""

    plain = (
        f"HotelWatch Price Drop Alert\n\n"
        f"Hotel: {booking.hotel_name}\n"
        f"Dates: {booking.check_in} → {booking.check_out}\n"
        f"Your booked rate: ${booked:.0f}\n"
        f"New rate: ${current:.0f}\n"
        f"You could save: ${savings:.0f} ({pct_savings}%)\n"
        f"Cancellation deadline: {booking.cancellation_deadline or 'unknown'}\n\n"
        f"— HotelWatch 🏨"
    )

    subject = f"🏨 Hotel Price Drop — Save ${savings:.0f} on {booking.hotel_name}!"

    try:
        _send_via_graph(recipient, subject, html, plain)
        log.info(f"Price drop alert sent to {recipient} for {booking.hotel_name} (${savings:.0f} savings)")
    except Exception as e:
        log.error(f"Failed to send alert email via Graph API: {e}")
        raise
