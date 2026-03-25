"""
HotelWatch — scrapers/marriott.py
Checks current rates on Marriott.com for a given booking.
Uses their public JSON API endpoint (the same one the website calls).
"""

import re
import logging
from datetime import datetime
from .base import BaseScraper, PriceResult

log = logging.getLogger("hotelwatch.scrapers.marriott")


class MarriottScraper(BaseScraper):
    chain_name = "marriott"

    # Marriott's internal property search API — same endpoint the website uses
    RATE_API = "https://www.marriott.com/search/submitSearch.mi"

    def check_price(self, booking) -> PriceResult:
        check_in  = booking.check_in   # YYYY-MM-DD
        check_out = booking.check_out
        room_type = booking.room_type or ""

        try:
            # Format dates as the API expects: MM/DD/YYYY
            ci = datetime.strptime(check_in,  "%Y-%m-%d").strftime("%m/%d/%Y")
            co = datetime.strptime(check_out, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError as e:
            return PriceResult.unavailable(f"Date parse error: {e}")

        # Hit the rates page directly — Marriott returns structured data in the page
        # for the specific hotel if we include the marshaCode
        url = booking.booking_url or (
            f"https://www.marriott.com/reservation/rateListMenu.mi"
            f"?fromDate={ci}&toDate={co}&clusterCode=none&numberOfRooms=1&numberOfAdults=1"
        )

        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            log.warning(f"Marriott fetch failed: {e}")
            return PriceResult.unavailable(f"Could not reach Marriott.com: {e}")

        return self._parse_rates(html, room_type, booking.total_booked_price)

    def _parse_rates(self, html: str, target_room_type: str, booked_price: float) -> PriceResult:
        """
        Extract rates from Marriott's rate list page.
        Marriott embeds JSON rate data in a <script> block.
        """
        # Look for embedded JSON rate data
        json_match = re.search(
            r'"roomRateDetailsList"\s*:\s*(\[.*?\])',
            html, re.DOTALL
        )

        prices_found = []

        if json_match:
            import json
            try:
                rooms = json.loads(json_match.group(1))
                for room in rooms:
                    room_name = room.get("roomTypeName", "")
                    rates = room.get("rateList", [])
                    for rate in rates:
                        price = rate.get("totalAmount") or rate.get("averageAmount")
                        if price:
                            prices_found.append({
                                "room": room_name,
                                "price": float(price),
                                "rate_name": rate.get("ratePlanDescription", ""),
                            })
            except Exception as e:
                log.debug(f"JSON parse failed, falling back to regex: {e}")

        # Fallback: regex scan for price patterns near room type names
        if not prices_found:
            # Pattern: "1 King Bed" near "$258" or "258 USD"
            price_blocks = re.findall(
                r'([\w\s,]+(?:King|Queen|Double|Twin|Suite)[\w\s,]*?)'
                r'.*?\$([\d,]+(?:\.\d{2})?)',
                html, re.DOTALL | re.IGNORECASE
            )
            for room, price_str in price_blocks[:10]:
                prices_found.append({
                    "room":      room.strip()[:80],
                    "price":     float(price_str.replace(",", "")),
                    "rate_name": "",
                })

        if not prices_found:
            # Last resort: find the cheapest price on the page
            all_prices = re.findall(r'\$([\d]{2,4}(?:\.\d{2})?)', html)
            numeric = [float(p) for p in all_prices if 50 < float(p) < 2000]
            if numeric:
                best = min(numeric)
                return PriceResult(
                    current_price=best,
                    notes=f"Found lowest price ${best:.0f} on page (room type not confirmed). Booked: ${booked_price:.0f}.",
                    raw_snippet=html[:300],
                )
            return PriceResult.unavailable("Could not parse prices from Marriott page — manual check recommended.")

        # Find the best match for our room type
        target_lower = target_room_type.lower()
        matched = [r for r in prices_found if target_lower[:10] in r["room"].lower()]
        candidates = matched if matched else prices_found

        # Take the lowest available price (member rate if available, else standard)
        best = min(candidates, key=lambda r: r["price"])

        drop = round(booked_price - best["price"], 2)
        if drop > 0:
            notes = f"Price dropped! Was ${booked_price:.0f}, now ${best['price']:.0f} ({best['rate_name'] or best['room']}). Save ${drop:.0f}."
        elif drop < 0:
            notes = f"Price increased. Current: ${best['price']:.0f} vs booked ${booked_price:.0f}."
        else:
            notes = f"No change. Current: ${best['price']:.0f}/night ({best['rate_name'] or best['room']})."

        return PriceResult(
            current_price=best["price"],
            room_type=best["room"],
            rate_name=best["rate_name"],
            notes=notes,
            raw_snippet=str(best),
        )
