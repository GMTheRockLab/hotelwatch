"""
HotelWatch — scheduler.py
APScheduler instance that:
  - Checks hotel prices every 3 hours
  - Scans inboxes for new bookings every 6 hours
  - Runs immediately on startup (so first check doesn't wait 3 hours)
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval     import IntervalTrigger
from apscheduler.triggers.cron         import CronTrigger

log = logging.getLogger("hotelwatch.scheduler")

_scheduler = None


def _run_price_checks():
    """Job: check prices for all active bookings."""
    from database import SessionLocal
    from price_checker import run_all_checks
    db = SessionLocal()
    try:
        summary = run_all_checks(db)
        log.info(f"Price check complete: {summary}")
    except Exception as e:
        log.error(f"Price check job failed: {e}", exc_info=True)
    finally:
        db.close()


def _run_inbox_scan():
    """Job: scan all users' inboxes for new hotel confirmation emails."""
    from database import SessionLocal, User, Booking
    from email_parser import scan_inbox_for_bookings
    import uuid

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.check_enabled == True).all()
        for user in users:
            if not user.ms_refresh_token:
                continue
            try:
                new_bookings = scan_inbox_for_bookings(user, db)
                for b_data in new_bookings:
                    booking = Booking(**b_data)
                    db.add(booking)
                db.commit()
                if new_bookings:
                    log.info(f"Added {len(new_bookings)} new bookings for {user.email}")
            except Exception as e:
                log.error(f"Inbox scan failed for {user.email}: {e}", exc_info=True)
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler. Call once at app startup."""
    global _scheduler

    if _scheduler and _scheduler.running:
        log.warning("Scheduler already running")
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Price checks: every 3 hours
    _scheduler.add_job(
        _run_price_checks,
        trigger=IntervalTrigger(hours=3),
        id="price_checks",
        name="Hotel Price Checks",
        replace_existing=True,
        misfire_grace_time=300,  # Allow up to 5 min late
    )

    # Inbox scan: every 6 hours (less frequent — email parsing is heavier)
    _scheduler.add_job(
        _run_inbox_scan,
        trigger=IntervalTrigger(hours=6),
        id="inbox_scan",
        name="Inbox Scan for New Bookings",
        replace_existing=True,
        misfire_grace_time=600,
    )

    _scheduler.start()
    log.info("Scheduler started: price checks every 3h, inbox scan every 6h")

    # Run immediately on startup so users don't wait
    import threading
    threading.Thread(target=_run_price_checks, daemon=True).start()
    threading.Thread(target=_run_inbox_scan,   daemon=True).start()

    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")


def trigger_price_check_now():
    """Manually trigger a price check run (called from API endpoint)."""
    import threading
    t = threading.Thread(target=_run_price_checks, daemon=True)
    t.start()
    return t


def trigger_inbox_scan_now():
    """Manually trigger an inbox scan (called from API endpoint)."""
    import threading
    t = threading.Thread(target=_run_inbox_scan, daemon=True)
    t.start()
    return t
