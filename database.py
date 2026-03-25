"""
HotelWatch — database.py
Defines all models and provides get_db() for dependency injection.
Uses SQLite via SQLAlchemy (swap DATABASE_URL to postgres:// on Railway with zero code changes).
"""

import os
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    Boolean, DateTime, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./hotelwatch.db")

# SQLite needs check_same_thread=False; Postgres doesn't need it but ignores it
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(String, primary_key=True)          # UUID
    email         = Column(String, unique=True, nullable=False)
    name          = Column(String, default="")
    api_token     = Column(String, unique=True, nullable=False)  # bearer token for API calls
    # Microsoft OAuth tokens (encrypted at rest in production)
    ms_access_token  = Column(Text, nullable=True)
    ms_refresh_token = Column(Text, nullable=True)
    ms_token_expiry  = Column(DateTime, nullable=True)
    # Settings
    alert_email      = Column(String, nullable=True)   # where to send drop alerts
    check_enabled    = Column(Boolean, default=True)
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_email_scan  = Column(DateTime, nullable=True) # last time we scanned their inbox

    bookings = relationship("Booking", back_populates="user", cascade="all, delete-orphan")


class Booking(Base):
    __tablename__ = "bookings"

    id                    = Column(String, primary_key=True)   # UUID
    user_id               = Column(String, ForeignKey("users.id"), nullable=False)
    # Hotel info
    hotel_name            = Column(String, nullable=False)
    hotel_chain           = Column(String, nullable=True)      # "marriott", "hilton", etc.
    booking_site          = Column(String, nullable=True)      # "Marriott.com", "Expedia", etc.
    booking_url           = Column(Text, nullable=True)        # deep link to rates page
    confirmation_number   = Column(String, nullable=True)
    # Stay details
    check_in              = Column(String, nullable=False)     # ISO date YYYY-MM-DD
    check_out             = Column(String, nullable=False)
    num_nights            = Column(Integer, default=1)
    room_type             = Column(String, nullable=True)
    # Pricing
    booked_price_per_night = Column(Float, nullable=False)
    total_booked_price     = Column(Float, nullable=False)
    currency               = Column(String, default="USD")
    # Cancellation
    cancellation_deadline  = Column(String, nullable=True)     # ISO date
    is_refundable          = Column(Boolean, default=True)
    # Source + state
    source                 = Column(String, default="email")   # "email" | "manual"
    source_email_id        = Column(String, nullable=True)     # MS Graph message ID
    active                 = Column(Boolean, default=True)
    added_on               = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Best price seen so far (denormalized for fast dashboard queries)
    lowest_price_seen      = Column(Float, nullable=True)
    last_checked           = Column(DateTime, nullable=True)

    user         = relationship("User", back_populates="bookings")
    price_checks = relationship("PriceCheck", back_populates="booking", cascade="all, delete-orphan")


class PriceCheck(Base):
    __tablename__ = "price_checks"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    booking_id          = Column(String, ForeignKey("bookings.id"), nullable=False)
    checked_at          = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    booked_price        = Column(Float, nullable=False)
    current_price       = Column(Float, nullable=True)    # null = unavailable/blocked
    price_drop          = Column(Float, nullable=True)    # booked - current (positive = savings)
    alert_sent          = Column(Boolean, default=False)
    notes               = Column(Text, nullable=True)
    raw_response        = Column(Text, nullable=True)     # debug: store scraped snippet

    booking = relationship("Booking", back_populates="price_checks")


# ── Helpers ───────────────────────────────────────────────────────────────────

def create_tables():
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session and ensures it closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
