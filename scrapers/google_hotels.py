"""
HotelWatch — scrapers/google_hotels.py
Checks current hotel rates via Google Hotels using Playwright.

Why Google Hotels?
- Single scraper handles ALL brands + independent hotels
- No per-chain bot detection to fight
- Consistent DOM structure (span.A6Q78d for prices)
- Shows rates from Marriott direct, Expedia, Priceline, etc. side by side
- Works for any hotel worldwide
"""

import re
import logging
import asyncio
from typing import Optional

from .base import PriceResult

log = logging.getLogger("hotelwatch.scrapers.google_hotels")


def _build_google_hotels_url(hotel_name: str, check_in: str, check_out: str) -> str:
    """
    Build a Google Hotels search URL for a specific hotel and dates.
    check_in / check_out format: YYYY-MM-DD
    """
    import urllib.parse
    query = urllib.parse.quote_plus(f"{hotel_name} hotel")
    return (
        f"https://www.google.com/travel/search"
        f"?q={query}"
        f"&checkin={check_in}"
        f"&checkout={check_out}"
    )


def _parse_price_text(text: str) -> Optional[float]:
    """Extract a numeric price from a string like '$529' or '529'."""
    match = re.search(r"[\$]?([\d,]+(?:\.\d{2})?)", text.replace(",", ""))
    if match:
        try:
            val = float(match.group(1).replace(",", ""))
            if 30 < val < 10000:
                return val
        except ValueError:
            pass
    return None


async def _fetch_google_hotels_price(hotel_name: str, check_in: str, check_out: str) -> dict:
    """
    Use Playwright to load Google Hotels and extract the lowest rate.
    Returns dict with keys: price, source, all_prices, url
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "playwright not installed"}

    url = _build_google_hotels_url(hotel_name, check_in, check_out)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait for price elements to appear (span.A6Q78d is Google's price class)
            try:
                await page.wait_for_selector("span.A6Q78d", timeout=12000)
            except Exception:
                # Prices may be in the hotel detail panel — try clicking the first result
                try:
                    first_result = await page.query_selector('[data-hveid]')
                    if first_result:
                        await first_result.click()
                        await page.wait_for_selector("span.A6Q78d", timeout=8000)
                except Exception:
                    pass

            # Grab all price spans
            price_els = await page.query_selector_all("span.A6Q78d")
            raw_prices = []
            for el in price_els:
                text = await el.inner_text()
                val = _parse_price_text(text)
                if val:
                    raw_prices.append(val)

            # Also check for the featured price shown in the hotel detail panel
            # (shown as a larger badge like "$512 / May 15-17")
            featured_el = await page.query_selector('[data-price], .kR3v4b, .YMkMbb')
            featured_price = None
            if featured_el:
                text = await featured_el.inner_text()
                featured_price = _parse_price_text(text)

            final_url = page.url

        except Exception as e:
            log.warning(f"Playwright page interaction failed: {e}")
            raw_prices = []
            featured_price = None
            final_url = url
        finally:
            await browser.close()

    if not raw_prices and featured_price is None:
        return {"error": "No prices found on Google Hotels page", "url": final_url}

    all_prices = sorted(set(raw_prices))
    # Prefer the featured/highlighted price; otherwise take the lowest
    best_price = featured_price if featured_price else (min(raw_prices) if raw_prices else None)

    return {
        "price": best_price,
        "all_prices": all_prices,
        "url": final_url,
    }


class GoogleHotelsScraper:
    """
    Universal hotel price scraper using Google Hotels + Playwright.
    Replaces all brand-specific scrapers with one that works for any hotel.
    """

    chain_name = "google_hotels"

    def check_price(self, booking) -> PriceResult:
        hotel_name  = booking.hotel_name
        check_in    = booking.check_in   # YYYY-MM-DD
        check_out   = booking.check_out
        booked      = booking.total_booked_price
        room_type   = booking.room_type or ""

        if not hotel_name:
            return PriceResult.unavailable("No hotel name — cannot search Google Hotels.")

        try:
            result = asyncio.run(
                _fetch_google_hotels_price(hotel_name, check_in, check_out)
            )
        except Exception as e:
            log.error(f"Google Hotels scrape failed for {hotel_name}: {e}")
            return PriceResult.unavailable(f"Scrape error: {e}")

        if "error" in result:
            return PriceResult.unavailable(result["error"])

        current = result.get("price")
        if current is None:
            return PriceResult.unavailable("Could not extract price from Google Hotels.")

        drop = round(booked - current, 2)
        all_p = result.get("all_prices", [])
        price_range = f" (range seen: ${min(all_p):.0f}–${max(all_p):.0f})" if len(all_p) > 1 else ""

        if drop > 0:
            notes = (
                f"Price dropped! Was ${booked:.0f}, now ${current:.0f}. "
                f"Save ${drop:.0f}!{price_range}"
            )
        elif drop < 0:
            notes = f"Price increased. Current: ${current:.0f} vs booked ${booked:.0f}.{price_range}"
        else:
            notes = f"No change. Current rate: ${current:.0f}/night.{price_range}"

        return PriceResult(
            current_price=current,
            room_type=room_type or None,
            notes=notes,
            raw_snippet=f"Google Hotels | {result.get('url', '')[:120]}",
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
