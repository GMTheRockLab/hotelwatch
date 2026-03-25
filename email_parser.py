"""
HotelWatch — email_parser.py
Connects to a user's Outlook inbox via Microsoft Graph API,
finds hotel confirmation emails, and extracts booking details using Claude.

Flow:
  1. Use stored OAuth tokens to call Graph /messages
  2. Filter for hotel confirmation subjects
  3. Pass email body to Claude (via simple prompt) to extract structured data
  4. Return list of parsed Booking dicts
"""

import os
import re
import json
import uuid
import logging
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("hotelwatch.email_parser")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Keywords that strongly suggest a hotel booking confirmation
BOOKING_KEYWORDS = [
    "reservation confirmation", "booking confirmation", "hotel confirmation",
    "your stay", "check-in", "check in", "reservation number", "confirmation number",
    "itinerary", "your upcoming stay", "booking details"
]

# Hotel chains we know how to build rate-check URLs for
KNOWN_CHAINS = {
    "marriott": ["marriott.com", "marriott bonvoy"],
    "hilton":   ["hilton.com", "hilton honors", "hampton inn", "doubletree", "curio", "waldorf"],
    "hyatt":    ["hyatt.com", "world of hyatt"],
    "ihg":      ["ihg.com", "holiday inn", "intercontinental", "crowne plaza", "kimpton"],
    "wyndham":  ["wyndham.com", "days inn", "la quinta", "ramada"],
    "choice":   ["choicehotels.com", "comfort inn", "quality inn"],
    "expedia":  ["expedia.com"],
    "booking":  ["booking.com"],
}


# ── Microsoft Graph helpers ────────────────────────────────────────────────────

def _graph_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token using MSAL."""
    tenant_id    = os.getenv("MS_TENANT_ID", "common")
    client_id    = os.getenv("MS_CLIENT_ID")
    client_secret = os.getenv("MS_CLIENT_SECRET")

    resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
            "scope":         "https://graph.microsoft.com/Mail.Read offline_access",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()   # contains access_token, refresh_token, expires_in


def get_valid_token(user) -> Optional[str]:
    """
    Returns a valid access token for the user, refreshing if needed.
    Updates user.ms_access_token / ms_refresh_token / ms_token_expiry in place.
    Caller must commit the DB session.
    """
    if not user.ms_refresh_token:
        return None

    now = datetime.now(timezone.utc)
    expiry = user.ms_token_expiry
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry is None or now >= expiry - timedelta(minutes=5):
        try:
            tokens = refresh_access_token(user.ms_refresh_token)
            user.ms_access_token  = tokens["access_token"]
            user.ms_refresh_token = tokens.get("refresh_token", user.ms_refresh_token)
            user.ms_token_expiry  = now + timedelta(seconds=tokens["expires_in"])
        except Exception as e:
            log.error(f"Token refresh failed for {user.email}: {e}")
            return None

    return user.ms_access_token


# ── Email scanning ─────────────────────────────────────────────────────────────

def fetch_recent_emails(access_token: str, since: Optional[datetime] = None, limit: int = 50) -> list:
    """Pull recent emails from inbox, filtered by hotel-related keywords."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else "2020-01-01T00:00:00Z"

    # Build OData filter: receivedDateTime AND subject keyword search
    keyword_filter = " or ".join(
        f"contains(subject,'{kw}')" for kw in [
            "confirmation", "reservation", "booking", "hotel", "stay", "check-in"
        ]
    )
    filter_str = f"receivedDateTime ge {since_str} and ({keyword_filter})"

    url = (
        f"{GRAPH_BASE}/me/messages"
        f"?$filter={filter_str}"
        f"&$select=id,subject,from,receivedDateTime,body"
        f"&$top={limit}"
        f"&$orderby=receivedDateTime desc"
    )

    try:
        resp = httpx.get(url, headers=_graph_headers(access_token), timeout=20)
        resp.raise_for_status()
        return resp.json().get("value", [])
    except Exception as e:
        log.error(f"Failed to fetch emails: {e}")
        return []


def is_hotel_confirmation(email: dict) -> bool:
    """Quick pre-filter before sending to AI parser."""
    subject = email.get("subject", "").lower()
    body    = (email.get("body", {}).get("content", "") or "")[:500].lower()
    text    = subject + " " + body

    return any(kw in text for kw in BOOKING_KEYWORDS)


# ── AI-powered booking extraction ─────────────────────────────────────────────

def extract_booking_from_email(email: dict, user_id: str) -> Optional[dict]:
    """
    Use Claude to parse a hotel confirmation email and return a structured booking dict.
    Falls back to regex extraction if Claude is unavailable.
    """
    subject = email.get("subject", "")
    body    = email.get("body", {}).get("content", "") or ""
    # Strip HTML tags for cleaner parsing
    body_text = re.sub(r"<[^>]+>", " ", body)
    body_text = re.sub(r"\s+", " ", body_text).strip()[:4000]  # cap at 4k chars

    # Try Claude extraction first
    result = _claude_extract(subject, body_text)

    # Fall back to regex
    if not result:
        result = _regex_extract(subject, body_text)

    if not result:
        return None

    # Enrich with computed fields
    result["id"]              = str(uuid.uuid4())
    result["user_id"]         = user_id
    result["source"]          = "email"
    result["source_email_id"] = email.get("id")
    result["active"]          = True
    result["currency"]        = result.get("currency", "USD")
    result["hotel_chain"]     = _detect_chain(result.get("hotel_name", ""), result.get("booking_site", ""))
    result["booking_url"]     = _build_booking_url(result)

    return result


def _claude_extract(subject: str, body: str) -> Optional[dict]:
    """Call Claude API to extract structured booking data from email text."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    prompt = f"""Extract hotel booking details from this confirmation email. Return ONLY valid JSON, no explanation.

Email subject: {subject}

Email body:
{body}

Return this exact JSON structure (use null for any field you cannot find):
{{
  "hotel_name": "Full hotel name",
  "booking_site": "Site where booked (e.g. Marriott.com, Expedia, etc.)",
  "confirmation_number": "Confirmation/reservation number",
  "check_in": "YYYY-MM-DD",
  "check_out": "YYYY-MM-DD",
  "num_nights": 1,
  "room_type": "Room type description",
  "booked_price_per_night": 0.00,
  "total_booked_price": 0.00,
  "currency": "USD",
  "cancellation_deadline": "YYYY-MM-DD or null",
  "is_refundable": true
}}"""

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        # Extract JSON from response (Claude sometimes adds markdown fences)
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        log.warning(f"Claude extraction failed: {e}")

    return None


def _regex_extract(subject: str, body: str) -> Optional[dict]:
    """Fallback regex extractor for common confirmation email patterns."""
    text = subject + " " + body

    # Date patterns: March 28, 2026 / Mar 28, 2026 / 03/28/2026 / 2026-03-28
    date_pattern = (
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
        r"|\d{1,2}/\d{1,2}/\d{4}"
        r"|\d{4}-\d{2}-\d{2}"
    )
    dates = re.findall(date_pattern, text, re.IGNORECASE)

    # Price pattern
    price_match = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", text)
    total_price = float(price_match.group(1).replace(",", "")) if price_match else None

    # Confirmation number
    conf_match = re.search(
        r"(?:confirmation|reservation|booking)\s*(?:#|number|no\.?)[\s:]*([A-Z0-9\-]{4,20})",
        text, re.IGNORECASE
    )
    conf_num = conf_match.group(1) if conf_match else None

    if not dates or not total_price:
        return None

    def parse_date(d):
        for fmt in ["%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%m/%d/%Y", "%Y-%m-%d"]:
            try:
                return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    parsed_dates = [parse_date(d) for d in dates if parse_date(d)]
    parsed_dates = sorted(set(parsed_dates))

    check_in  = parsed_dates[0]  if len(parsed_dates) >= 1 else None
    check_out = parsed_dates[1]  if len(parsed_dates) >= 2 else None
    num_nights = 1
    if check_in and check_out:
        delta = datetime.strptime(check_out, "%Y-%m-%d") - datetime.strptime(check_in, "%Y-%m-%d")
        num_nights = max(delta.days, 1)

    return {
        "hotel_name":            _extract_hotel_name(subject),
        "booking_site":          None,
        "confirmation_number":   conf_num,
        "check_in":              check_in,
        "check_out":             check_out,
        "num_nights":            num_nights,
        "room_type":             None,
        "booked_price_per_night": round(total_price / num_nights, 2) if total_price else None,
        "total_booked_price":    total_price,
        "currency":              "USD",
        "cancellation_deadline": None,
        "is_refundable":         True,
    }


def _extract_hotel_name(subject: str) -> str:
    """Try to pull hotel name from email subject."""
    # Remove common prefixes
    cleaned = re.sub(
        r"(?i)(your\s+)?(reservation|booking|hotel|stay)\s+(confirmation|details?|at|for|@)\s*",
        "", subject
    ).strip()
    # Remove confirmation numbers at end
    cleaned = re.sub(r"\s*[-–]\s*[A-Z0-9]{4,}$", "", cleaned).strip()
    return cleaned or subject


def _detect_chain(hotel_name: str, booking_site: str) -> Optional[str]:
    text = f"{hotel_name} {booking_site}".lower()
    for chain, keywords in KNOWN_CHAINS.items():
        if any(kw in text for kw in keywords):
            return chain
    return None


def _build_booking_url(booking: dict) -> Optional[str]:
    """Build a direct URL to check rates for this booking."""
    chain    = booking.get("hotel_chain")
    check_in = booking.get("check_in", "")
    check_out= booking.get("check_out", "")

    if not check_in or not check_out:
        return None

    # Format dates as MM/DD/YYYY for most hotel sites
    def fmt(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            return d

    if chain == "marriott":
        return (
            f"https://www.marriott.com/reservation/rateListMenu.mi"
            f"?fromDate={fmt(check_in)}&toDate={fmt(check_out)}"
        )
    if chain == "hilton":
        return (
            f"https://www.hilton.com/en/book/reservation/rooms/"
            f"?arrivalDate={check_in}&departureDate={check_out}&numAdults=1"
        )
    if chain == "hyatt":
        return (
            f"https://www.hyatt.com/shop/rooms"
            f"?checkinDate={check_in}&checkoutDate={check_out}"
        )
    if chain in ("expedia", "booking"):
        return booking.get("booking_url")  # usually in the email itself

    return None


# ── Main entry point ───────────────────────────────────────────────────────────

def scan_inbox_for_bookings(user, db_session) -> list[dict]:
    """
    Full pipeline: authenticate → fetch emails → parse → return new booking dicts.
    Only returns bookings NOT already in the database (deduped by confirmation_number).
    """
    access_token = get_valid_token(user)
    if not access_token:
        log.warning(f"No valid token for {user.email}, skipping inbox scan")
        return []

    # Scan from last scan time (or 90 days back on first run)
    since = user.last_email_scan or (datetime.now(timezone.utc) - timedelta(days=90))
    emails = fetch_recent_emails(access_token, since=since)
    log.info(f"Fetched {len(emails)} candidate emails for {user.email}")

    new_bookings = []
    seen_conf_numbers = {
        b.confirmation_number for b in user.bookings if b.confirmation_number
    }

    for email in emails:
        if not is_hotel_confirmation(email):
            continue
        parsed = extract_booking_from_email(email, user.id)
        if not parsed:
            continue
        conf = parsed.get("confirmation_number")
        if conf and conf in seen_conf_numbers:
            log.debug(f"Skipping duplicate booking {conf}")
            continue
        new_bookings.append(parsed)
        if conf:
            seen_conf_numbers.add(conf)

    # Update last scan time
    user.last_email_scan = datetime.now(timezone.utc)
    db_session.commit()

    log.info(f"Found {len(new_bookings)} new bookings for {user.email}")
    return new_bookings
