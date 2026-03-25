"""
HotelWatch — scrapers/generic.py
Generic price scraper used for hotel chains without a dedicated scraper,
and as a fallback. Uses Claude to parse the page if simple regex fails.
Covers: Hilton, Hyatt, IHG, Expedia, Booking.com, and unknowns.
"""

import re
import os
import logging
import httpx
from .base import BaseScraper, PriceResult

log = logging.getLogger("hotelwatch.scrapers.generic")


class GenericScraper(BaseScraper):
    chain_name = "generic"

    def check_price(self, booking) -> PriceResult:
        url = booking.booking_url
        if not url:
            return PriceResult.unavailable("No booking URL available — manual check recommended.")

        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            html = resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 429):
                return PriceResult.unavailable(
                    f"Site blocked automated access (HTTP {e.response.status_code}). "
                    "Manual check recommended."
                )
            return PriceResult.unavailable(f"HTTP error {e.response.status_code}")
        except Exception as e:
            return PriceResult.unavailable(f"Could not reach site: {e}")

        # Strip HTML to text
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()

        # Try regex first (fast, no API call)
        result = self._regex_parse(text, booking)
        if result.success:
            return result

        # Fall back to Claude for tricky pages
        return self._claude_parse(text[:3000], booking)

    def _regex_parse(self, text: str, booking) -> PriceResult:
        booked = booking.total_booked_price
        room_type = (booking.room_type or "").lower()

        # Find all dollar amounts in a reasonable hotel price range
        prices = [
            float(p.replace(",", ""))
            for p in re.findall(r'\$([\d,]{2,4}(?:\.\d{2})?)', text)
            if 30 < float(p.replace(",", "")) < 3000
        ]

        if not prices:
            return PriceResult.unavailable("No prices found on page.")

        # If we have a room type, try to find prices near it
        if room_type:
            # Look for price within 200 chars of our room type mention
            pattern = rf"(?i){re.escape(room_type[:15])}.{{0,200}}\$([\d,]+(?:\.\d{{2}})?)"
            matches = re.findall(pattern, text)
            if matches:
                nearby_price = float(matches[0].replace(",", ""))
                if 30 < nearby_price < 3000:
                    drop = round(booked - nearby_price, 2)
                    notes = (
                        f"Price dropped! ${booked:.0f} → ${nearby_price:.0f}. Save ${drop:.0f}!"
                        if drop > 0 else
                        f"No change. Current: ${nearby_price:.0f} (booked: ${booked:.0f})."
                    )
                    return PriceResult(
                        current_price=nearby_price,
                        room_type=booking.room_type,
                        notes=notes,
                        raw_snippet=f"Matched near room type: ${nearby_price:.0f}",
                    )

        # No room-type match — return lowest price with a caveat
        best = min(prices)
        drop = round(booked - best, 2)
        notes = (
            f"Lowest price found: ${best:.0f} (booked: ${booked:.0f}). "
            "Room type not confirmed — verify before rebooking."
        )
        return PriceResult(
            current_price=best,
            notes=notes,
            raw_snippet=f"Lowest of {len(prices)} prices found",
        )

    def _claude_parse(self, page_text: str, booking) -> PriceResult:
        """Ask Claude to extract the price for our specific room from page text."""
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return PriceResult.unavailable("Page parsing failed — no AI fallback configured.")

        prompt = f"""This is text from a hotel booking page. Extract the current nightly rate for this room:
Room type: {booking.room_type or "any available room"}
Check-in: {booking.check_in}
Check-out: {booking.check_out}

Page text:
{page_text}

Reply with ONLY a JSON object: {{"price": 123.00, "room_type": "room name found", "rate_name": "rate type", "confidence": "high/medium/low"}}
If you cannot find a price, reply: {{"price": null, "notes": "reason"}}"""

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
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=15,
            )
            resp.raise_for_status()
            import json
            text = resp.json()["content"][0]["text"].strip()
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                price = data.get("price")
                if price is None:
                    return PriceResult.unavailable(data.get("notes", "Claude could not find price."))
                booked = booking.total_booked_price
                drop = round(booked - float(price), 2)
                notes = (
                    f"Price dropped! ${booked:.0f} → ${price:.0f}. Save ${drop:.0f}!"
                    if drop > 0 else
                    f"No change. Current: ${price:.0f} (booked: ${booked:.0f})."
                )
                return PriceResult(
                    current_price=float(price),
                    room_type=data.get("room_type"),
                    rate_name=data.get("rate_name"),
                    notes=notes,
                    raw_snippet=f"Claude parsed (confidence: {data.get('confidence', '?')})",
                )
        except Exception as e:
            log.warning(f"Claude page parse failed: {e}")

        return PriceResult.unavailable("Could not extract price. Manual check recommended.")
