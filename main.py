"""
HotelWatch — main.py
FastAPI application: auth, API routes, Microsoft OAuth, static pages.
Run with: uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import uuid
import secrets
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import create_tables, get_db, User, Booking, PriceCheck
from scheduler import start_scheduler, trigger_price_check_now, trigger_inbox_scan_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
log = logging.getLogger("hotelwatch.main")

app = FastAPI(title="HotelWatch", version="1.0.0")

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    create_tables()
    start_scheduler()
    log.info("HotelWatch started.")

@app.on_event("shutdown")
def on_shutdown():
    from scheduler import stop_scheduler
    stop_scheduler()


# ── Auth helpers ──────────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = credentials.credentials
    user  = db.query(User).filter(User.api_token == token).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


# ── Pages ─────────────────────────────────────────────────────────────────────

def _read_template(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "templates", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/", response_class=HTMLResponse)
def index():
    return _read_template("dashboard.html")

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _read_template("login.html")


# ── Email/password Auth ───────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    email: str
    password: str

@app.post("/api/auth/register")
def register(body: AuthRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(400, "Account already exists for this email")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user = User(
        id         = str(uuid.uuid4()),
        email      = body.email,
        alert_email= body.email,
        api_token  = secrets.token_urlsafe(32),
    )
    # Store password hash in ms_access_token field for now (simple approach)
    user.ms_access_token = f"pw:{hash_password(body.password)}"
    db.add(user)
    db.commit()
    return {"token": user.api_token, "email": user.email}

@app.post("/api/auth/login")
def login(body: AuthRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        raise HTTPException(401, "Invalid email or password")
    stored = user.ms_access_token or ""
    if not stored.startswith("pw:") or stored[3:] != hash_password(body.password):
        raise HTTPException(401, "Invalid email or password")
    return {"token": user.api_token, "email": user.email}


# ── Microsoft OAuth ────────────────────────────────────────────────────────────

MS_CLIENT_ID     = os.getenv("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "")
MS_TENANT_ID     = os.getenv("MS_TENANT_ID", "common")
APP_BASE_URL     = os.getenv("APP_BASE_URL", "http://localhost:8000")
MS_REDIRECT_URI  = f"{APP_BASE_URL}/auth/microsoft/callback"

MS_SCOPES = "openid profile email offline_access https://graph.microsoft.com/Mail.Read"

@app.get("/auth/microsoft")
def ms_auth_redirect():
    """Redirect user to Microsoft login."""
    if not MS_CLIENT_ID:
        raise HTTPException(500, "Microsoft OAuth not configured (MS_CLIENT_ID missing)")
    state = secrets.token_urlsafe(16)
    url = (
        f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/authorize"
        f"?client_id={MS_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={MS_REDIRECT_URI}"
        f"&scope={MS_SCOPES.replace(' ', '%20')}"
        f"&state={state}"
        f"&prompt=select_account"
    )
    return RedirectResponse(url)

@app.get("/auth/microsoft/callback")
def ms_auth_callback(code: str = None, error: str = None, db: Session = Depends(get_db)):
    """Handle Microsoft OAuth callback — create/update user, return token."""
    if error or not code:
        return RedirectResponse(f"/login?error={error or 'cancelled'}")

    # Exchange code for tokens
    try:
        token_resp = httpx.post(
            f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token",
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  MS_REDIRECT_URI,
                "client_id":     MS_CLIENT_ID,
                "client_secret": MS_CLIENT_SECRET,
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
    except Exception as e:
        log.error(f"MS token exchange failed: {e}")
        return RedirectResponse("/login?error=token_exchange_failed")

    access_token  = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in    = tokens.get("expires_in", 3600)

    # Get user profile from Graph
    try:
        me_resp = httpx.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_resp.raise_for_status()
        me = me_resp.json()
    except Exception as e:
        log.error(f"MS Graph /me failed: {e}")
        return RedirectResponse("/login?error=profile_fetch_failed")

    ms_email = me.get("mail") or me.get("userPrincipalName", "")
    ms_name  = me.get("displayName", "")

    # Create or update user
    user = db.query(User).filter(User.email == ms_email).first()
    if not user:
        user = User(
            id          = str(uuid.uuid4()),
            email       = ms_email,
            name        = ms_name,
            alert_email = ms_email,
            api_token   = secrets.token_urlsafe(32),
        )
        db.add(user)

    user.ms_access_token  = access_token
    user.ms_refresh_token = refresh_token
    user.ms_token_expiry  = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    user.name             = ms_name
    db.commit()

    # Kick off inbox scan immediately for new users
    trigger_inbox_scan_now()

    return RedirectResponse(f"/login?token={user.api_token}")


# ── API: Me ───────────────────────────────────────────────────────────────────

@app.get("/api/me")
def get_me(user: User = Depends(get_current_user)):
    return {
        "id":                user.id,
        "email":             user.email,
        "name":              user.name,
        "alert_email":       user.alert_email,
        "outlook_connected": bool(user.ms_refresh_token),
        "last_inbox_scan":   user.last_email_scan.isoformat() if user.last_email_scan else None,
        "check_enabled":     user.check_enabled,
    }


# ── API: Stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bookings = db.query(Booking).filter(
        Booking.user_id == user.id,
        Booking.active  == True,
    ).all()

    total_savings = 0.0
    total_drops   = 0
    last_check_at = None

    for b in bookings:
        if b.last_checked:
            if last_check_at is None or b.last_checked > last_check_at:
                last_check_at = b.last_checked
        best_drop = b.total_booked_price - (b.lowest_price_seen or b.total_booked_price)
        if best_drop > 0:
            total_savings += best_drop
            total_drops   += 1

    return {
        "total_bookings": len(bookings),
        "total_savings":  round(total_savings, 2),
        "total_drops":    total_drops,
        "last_check_at":  last_check_at.isoformat() if last_check_at else None,
    }


# ── API: Bookings ─────────────────────────────────────────────────────────────

@app.get("/api/bookings")
def list_bookings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bookings = (
        db.query(Booking)
        .filter(Booking.user_id == user.id, Booking.active == True)
        .order_by(Booking.check_in)
        .all()
    )
    result = []
    for b in bookings:
        # Get last 8 price checks for sparkline
        recent = (
            db.query(PriceCheck)
            .filter(PriceCheck.booking_id == b.id)
            .order_by(PriceCheck.checked_at.desc())
            .limit(8)
            .all()
        )
        recent_data = [
            {"price_drop": c.price_drop, "current_price": c.current_price,
             "checked_at": c.checked_at.isoformat()}
            for c in reversed(recent)
        ]
        last_check = recent[-1] if recent else None
        result.append({
            "id":                  b.id,
            "hotel_name":          b.hotel_name,
            "hotel_chain":         b.hotel_chain,
            "booking_site":        b.booking_site,
            "booking_url":         b.booking_url,
            "confirmation_number": b.confirmation_number,
            "check_in":            b.check_in,
            "check_out":           b.check_out,
            "num_nights":          b.num_nights,
            "room_type":           b.room_type,
            "total_booked_price":  b.total_booked_price,
            "currency":            b.currency,
            "cancellation_deadline": b.cancellation_deadline,
            "is_refundable":       b.is_refundable,
            "source":              b.source,
            "active":              b.active,
            "last_price":          last_check.current_price if last_check else None,
            "last_price_drop":     last_check.price_drop    if last_check else None,
            "last_checked":        b.last_checked.isoformat() if b.last_checked else None,
            "lowest_price_seen":   b.lowest_price_seen,
            "recent_checks":       recent_data,
        })
    return result


class BookingCreate(BaseModel):
    hotel_name:            str
    booking_site:          Optional[str] = None
    booking_url:           Optional[str] = None
    check_in:              str
    check_out:             str
    num_nights:            int  = 1
    room_type:             Optional[str] = None
    booked_price_per_night: Optional[float] = None
    total_booked_price:    float
    currency:              str  = "USD"
    cancellation_deadline: Optional[str] = None
    is_refundable:         bool = True
    confirmation_number:   Optional[str] = None
    source:                str  = "manual"

@app.post("/api/bookings", status_code=201)
def create_booking(
    body: BookingCreate,
    user: User = Depends(get_current_user),
    db:   Session = Depends(get_db),
):
    from email_parser import _detect_chain, _build_booking_url
    import uuid as _uuid

    b_dict = body.dict()
    b_dict["id"]       = str(_uuid.uuid4())
    b_dict["user_id"]  = user.id
    b_dict["active"]   = True
    b_dict["hotel_chain"] = _detect_chain(body.hotel_name, body.booking_site or "")
    if not body.booking_url:
        b_dict["booking_url"] = _build_booking_url(b_dict)
    if not body.booked_price_per_night:
        b_dict["booked_price_per_night"] = round(body.total_booked_price / max(body.num_nights, 1), 2)

    booking = Booking(**b_dict)
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Trigger an immediate price check for this booking
    import threading
    from price_checker import check_booking
    def _check():
        from database import SessionLocal
        _db = SessionLocal()
        try:
            b = _db.query(Booking).filter(Booking.id == booking.id).first()
            if b:
                check_booking(b, _db)
        finally:
            _db.close()
    threading.Thread(target=_check, daemon=True).start()

    return {"id": booking.id, "message": "Booking added and price check queued."}


@app.delete("/api/bookings/{booking_id}")
def delete_booking(
    booking_id: str,
    user: User = Depends(get_current_user),
    db:   Session = Depends(get_db),
):
    booking = db.query(Booking).filter(
        Booking.id == booking_id,
        Booking.user_id == user.id
    ).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    booking.active = False  # Soft delete — keep history
    db.commit()
    return {"message": "Booking removed from monitoring."}


@app.post("/api/bookings/{booking_id}/check")
def check_booking_now(
    booking_id: str,
    user: User = Depends(get_current_user),
    db:   Session = Depends(get_db),
):
    """Immediately check the price for a single booking."""
    booking = db.query(Booking).filter(
        Booking.id      == booking_id,
        Booking.user_id == user.id,
        Booking.active  == True,
    ).first()
    if not booking:
        raise HTTPException(404, "Booking not found")

    from price_checker import check_booking
    check = check_booking(booking, db)
    return {
        "current_price": check.current_price,
        "price_drop":    check.price_drop,
        "notes":         check.notes,
    }


# ── API: Trigger runs ─────────────────────────────────────────────────────────

@app.post("/api/check-all")
def check_all(user: User = Depends(get_current_user)):
    """Manually trigger a price check for all bookings."""
    trigger_price_check_now()
    return {"message": "Price check started in background."}

@app.post("/api/scan-inbox")
def scan_inbox(user: User = Depends(get_current_user)):
    """Manually trigger an inbox scan for new bookings."""
    trigger_inbox_scan_now()
    return {"message": "Inbox scan started in background."}


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "app": "HotelWatch", "version": "1.0.0"}
