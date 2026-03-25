"""
HotelWatch — price_checker.py
Orchestrates price checking for all active bookings.
Called by the scheduler every 3 hours.
"""

import uuid
import logging
from datetime import datetime, date, timezone
from sqlalchemy.orm import Session

from database import Booking, PriceCheck, User
from scrapers.marriott import MarriottScraper
from scrapers.generic  import GenericScraper
from alerter import send_price_drop_alert

log = logging.getLogger("hotelwatch.price_checker")

# Map chain name → scraper class
SCRAPERS = {
    "marriott": MarriottScraper,
    "hilton":   GenericScraper,   # TODO: dedicated Hilton scraper
    "hyatt":    GenericScraper,
    "ihg":      GenericScraper,
    "expedia":  GenericScraper,
    "booking":  GenericScraper,
}


def get_scraper(chain: str):
    """Return the right scraper for a hotel chain."""
    cls = SCRAPERS.get(chain, GenericScraper)
    return cls()


def should_check(booking: Booking) -> tuple[bool, str]:
    """Decide if a booking is worth checking right now."""
    if not booking.active:
        return False, "inactive"

    today = date.today()

    # Don't check if cancellation deadline has passed
    if booking.cancellation_deadline:
        try:
            deadline = date.fromisoformat(booking.cancellation_deadline)
            if deadline < today:
                return False, f"cancellation deadline passed ({deadline})"
        except ValueError:
            pass

    # Don't check if check-in is in the past
    try:
        check_in = date.fromisoformat(booking.check_in)
        if check_in < today:
            return False, "already checked in"
    except ValueError:
        pass

    return True, "ok"


def check_booking(booking: Booking, db: Session) -> PriceCheck:
    """Check the current price for a single booking and save the result."""
    ok, reason = should_check(booking)

    if not ok:
        log.info(f"Skipping booking {booking.id} ({booking.hotel_name}): {reason}")
        # Still log a skipped check so history is complete
        check = PriceCheck(
            booking_id    = booking.id,
            booked_price  = booking.total_booked_price,
            current_price = None,
            price_drop    = None,
            notes         = f"Skipped: {reason}",
            alert_sent    = False,
        )
        db.add(check)
        db.commit()
        return check

    log.info(f"Checking price for {booking.hotel_name} ({booking.check_in} → {booking.check_out})")

    chain = booking.hotel_chain or "generic"
    with get_scraper(chain) as scraper:
        result = scraper.check_price(booking)

    price_drop = None
    if result.current_price is not None:
        price_drop = round(booking.total_booked_price - result.current_price, 2)

    check = PriceCheck(
        booking_id    = booking.id,
        booked_price  = booking.total_booked_price,
        current_price = result.current_price,
        price_drop    = price_drop,
        notes         = result.notes,
        raw_response  = result.raw_snippet,
        alert_sent    = False,
    )
    db.add(check)

    # Update denormalized lowest price on the booking
    if result.current_price is not None:
        if booking.lowest_price_seen is None or result.current_price < booking.lowest_price_seen:
            booking.lowest_price_seen = result.current_price
    booking.last_checked = datetime.now(timezone.utc)

    db.commit()

    # Send alert if meaningful price drop found
    if price_drop and price_drop > 0:
        try:
            send_price_drop_alert(booking.user, booking, check)
            check.alert_sent = True
            db.commit()
        except Exception as e:
            log.error(f"Alert send failed for {booking.hotel_name}: {e}")

    return check


def run_all_checks(db: Session) -> dict:
    """
    Run price checks for ALL active bookings across all users.
    Called by scheduler every 3 hours.
    Returns a summary dict.
    """
    now = datetime.now(timezone.utc)
    log.info(f"Starting price check run at {now.isoformat()}")

    bookings = (
        db.query(Booking)
        .join(User)
        .filter(Booking.active == True, User.check_enabled == True)
        .all()
    )

    total      = len(bookings)
    checked    = 0
    skipped    = 0
    drops      = 0
    total_save = 0.0

    for booking in bookings:
        ok, _ = should_check(booking)
        if not ok:
            skipped += 1
            continue
        check = check_booking(booking, db)
        checked += 1
        if check.price_drop and check.price_drop > 0:
            drops      += 1
            total_save += check.price_drop

    summary = {
        "run_at":        now.isoformat(),
        "total_bookings": total,
        "checked":       checked,
        "skipped":       skipped,
        "price_drops":   drops,
        "total_savings": round(total_save, 2),
    }

    log.info(
        f"Check run complete: {checked} checked, {drops} drops found, "
        f"${total_save:.2f} potential savings."
    )
    return summary
