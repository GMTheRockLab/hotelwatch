"""
HotelWatch — scrapers/base.py
Base scraper class and shared utilities all chain scrapers inherit from.
"""

import logging
import httpx
from typing import Optional

log = logging.getLogger("hotelwatch.scrapers")

# Rotate user agents to reduce blocking
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

DEFAULT_HEADERS = {
    "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Connection":       "keep-alive",
}


class PriceResult:
    """Structured result from a price check."""
    def __init__(
        self,
        current_price: Optional[float],
        room_type: Optional[str] = None,
        rate_name: Optional[str] = None,
        notes: str = "",
        raw_snippet: str = "",
        success: bool = True,
    ):
        self.current_price = current_price
        self.room_type     = room_type
        self.rate_name     = rate_name
        self.notes         = notes
        self.raw_snippet   = raw_snippet[:500]  # cap stored debug info
        self.success       = success

    @classmethod
    def unavailable(cls, reason: str) -> "PriceResult":
        return cls(current_price=None, notes=reason, success=False)


class BaseScraper:
    """All chain scrapers inherit from this."""

    chain_name: str = "unknown"

    def __init__(self):
        import random
        self.client = httpx.Client(
            headers={**DEFAULT_HEADERS, "User-Agent": random.choice(USER_AGENTS)},
            follow_redirects=True,
            timeout=20,
        )

    def check_price(self, booking) -> PriceResult:
        """Override in subclass. booking is a DB Booking object."""
        raise NotImplementedError

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
