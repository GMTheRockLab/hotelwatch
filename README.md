# 🏨 HotelWatch

**Automatically monitors your hotel bookings and alerts you the moment a lower rate appears.**

Connect your Outlook inbox once. HotelWatch finds your hotel confirmation emails, extracts your bookings, and checks prices every 3 hours — alerting you instantly if you can save money by rebooking before your cancellation deadline.

---

## How it works

1. **Sign in with Microsoft** → HotelWatch gets read access to your Outlook inbox
2. **Auto-discovers bookings** → Scans for hotel confirmation emails, extracts hotel, dates, price, room type, and cancellation deadline
3. **Monitors prices every 3 hours** → Checks the current rate for your exact room on the hotel's website
4. **Emails you instantly** → If the price drops below what you paid, you get an alert with the savings, a link to rebook, and how many days you have left before your deadline

---

## Deploy to Railway (recommended — free tier works)

1. Fork this repo and connect it to [Railway](https://railway.app)
2. Set environment variables (see `.env.example`)
3. Done — Railway runs it 24/7

### Required environment variables

| Variable | Description |
|---|---|
| `APP_BASE_URL` | Your deployed URL, e.g. `https://hotelwatch.railway.app` |
| `MS_CLIENT_ID` | Azure app registration client ID |
| `MS_CLIENT_SECRET` | Azure app registration secret |
| `ANTHROPIC_API_KEY` | For smart email + page parsing |
| `SMTP_USER` / `SMTP_PASS` | For sending alert emails |

### Microsoft Azure setup (5 minutes)

1. Go to [portal.azure.com](https://portal.azure.com) → Azure Active Directory → App registrations → New registration
2. Name: `HotelWatch`, Supported account types: **Multitenant + personal**
3. Redirect URI: `https://your-app.railway.app/auth/microsoft/callback`
4. After creating: Certificates & secrets → New client secret
5. API permissions → Add → Microsoft Graph → Delegated → `Mail.Read`, `offline_access`

---

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # Fill in your values
uvicorn main:app --reload
```

Open http://localhost:8000

---

## Architecture

```
main.py           FastAPI app — routes, auth, pages
database.py       SQLAlchemy models (User, Booking, PriceCheck)
email_parser.py   Outlook email scanning + AI booking extraction
price_checker.py  Orchestrates price checks across all bookings
alerter.py        SMTP email alerts for price drops
scheduler.py      APScheduler — price checks every 3h, inbox scan every 6h
scrapers/
  base.py         Base scraper class
  marriott.py     Marriott.com rate parser
  generic.py      Fallback scraper (Hilton, Hyatt, Expedia, etc.)
templates/
  dashboard.html  Single-page web dashboard
  login.html      Sign-in page
```

## Roadmap

- [ ] Dedicated scrapers for Hilton, Hyatt, IHG
- [ ] Gmail support
- [ ] SMS alerts via Twilio
- [ ] Price history charts
- [ ] iOS/Android push notifications
- [ ] Affiliate links for rebooking (revenue model)
