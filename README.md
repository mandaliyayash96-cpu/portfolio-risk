# Clarisk — Investor Portfolio Monitoring & Risk Management System

> **See your risk clearly.**
> A real-time fintech platform that consolidates an investor's holdings, values them live against market data, computes a full quantitative risk profile, suggests lower-risk rebalancing, and pushes alerts the moment a risk limit is breached.

**GLS Nexus Hackathon 2026** · M.Sc. Information Technology, GLS University, Ahmedabad
**Team:** Yash Mandaliya · Kaivan Parikh

---

## Problem Statement

Individual investors hold assets across multiple platforms, resulting in fragmented information and limited visibility into their overall financial position. Without a centralized system, tracking portfolio performance, assessing diversification, monitoring risk exposure, and identifying trends becomes difficult.

**Clarisk** solves this: one unified dashboard that consolidates portfolio data, monitors performance, analyzes asset allocation, and generates quantitative risk insights — in plain English, updating automatically.

---

## Key Features

- **Phone-OTP sign-in** — secure Firebase phone authentication; each user gets their own portfolio.
- **Multi-platform aggregation** — add holdings by hand, import a CSV, or connect a broker (Zerodha / Groww / Upstox / ICICI — simulated). Overlapping stocks consolidate automatically.
- **Live valuation & performance** — real-time prices, profit/loss, portfolio value and drawdown charts.
- **Quantitative risk engine** — Value at Risk (Historical, Parametric, Monte Carlo), CVaR, volatility, beta, Sharpe, Sortino, maximum drawdown.
- **Diversification insights** — correlation matrix and concentration index (HHI).
- **Markowitz rebalancing** — minimum-variance suggestion plus the efficient frontier.
- **Real-time alerts** — user-defined risk limits, pushed live over WebSocket the moment they break.
- **AI insights (Google Gemini)** — on-demand, plain-English analysis of gains, losses, concentration and risk. Observations only, never financial advice.
- **Automatic refresh** — prices and alerts update every 60 seconds via a background scheduler.
- **PDF risk report** — one-click downloadable report.
- **₹9 pay-per-edit** — Razorpay-gated holdings editing (test mode).
- **Modern fintech UI** — tabbed dashboard, dark mode, graceful handling of delisted/bad tickers.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Backend | Python, Django, Django REST Framework |
| Risk mathematics | NumPy, Pandas, SciPy |
| Market data | yfinance (NSE `.NS` tickers) |
| Authentication | Firebase Phone Auth (`firebase-admin`) |
| Payments | Razorpay (test mode) |
| AI insights | Google Gemini API (`google-generativeai`) |
| Real-time | Django Channels + daphne (WebSocket) |
| Scheduling | Celery + Celery Beat |
| Broker / channel layer | Redis |
| Database | SQLite |
| Frontend | React, Vite, Recharts |
| Reporting | ReportLab (PDF) |
| Testing | pytest, pytest-django (560+ automated tests) |

---

## Architecture

```
React Dashboard  ──REST + WebSocket──►  Django + DRF API
   (tabs, charts,                         (thin views → services;
    modals)                                verifies Firebase token &
                                           Razorpay signature)
                                              │
        ┌─────────────────────────────────────┼──────────────────┐
        ▼                     ▼                ▼                  ▼
   Risk Engine          Market Data        Firebase Auth      Razorpay
   (pure NumPy/          (yfinance)         (phone OTP)        (₹9 unlock)
    SciPy functions)         │
                             ▼
                   Celery + Beat ──► Redis ──► refresh prices &
                                                scan alerts every 60s
```

**Design principle:** API views stay thin — all logic lives in a service/selector layer, and the risk engine is composed of *pure functions* (no database or framework), so every metric is independently testable and verified against known mathematical results.

---

## Repository Structure

```
portfolio-risk/
├── backend/
│   ├── config/         # settings, Celery, ASGI (WebSocket)
│   ├── accounts/       # Firebase auth, per-user portfolio
│   ├── payments/       # Razorpay orders, verify, ₹9 gating
│   ├── portfolio/      # holdings CRUD + CSV import + broker sync
│   ├── marketdata/     # yfinance provider, price fetch, Celery task
│   ├── risk/           # engine (pure), optimizer, services, PDF, AI insights
│   ├── alerts/         # rules, evaluator, WebSocket consumer, task
│   └── requirements.txt
├── frontend/
│   └── src/            # auth, payments, dashboard, tabs, charts, theme
└── docs/               # presentation (PPT / PDF), documentation
```

---

## Setup & Run

Clarisk runs as **five coordinated processes**: Redis, the Django backend (ASGI), the frontend, a Celery worker, and Celery Beat.

### Prerequisites
- Python 3.11+ · Node.js 18+ · Redis (via WSL/Ubuntu on Windows, or native on Linux/macOS)
- API credentials (see **Environment Variables** below): a Firebase service-account JSON, a Razorpay test key pair, and a Google Gemini API key.

### 1. Clone & backend setup
```bash
git clone <this-repo-url>
cd portfolio-risk/backend
python -m venv venv
source venv/Scripts/activate      # Windows Git Bash
# source venv/bin/activate        # Linux / macOS
pip install -r requirements.txt
```

### 2. Environment variables
Create `backend/.env` (this file is git-ignored — never commit real keys):
```env
# Firebase — path to the service-account JSON placed in backend/
FIREBASE_CREDENTIALS=your-firebase-adminsdk.json

# Razorpay (test mode)
RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxx

# Google Gemini
GEMINI_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxxx
```
Place your Firebase service-account JSON inside `backend/` and set its filename in `FIREBASE_CREDENTIALS`.
In the Firebase console, enable **Phone** sign-in and add `localhost` to authorized domains. For testing without real SMS, add a test phone number under *Authentication → Sign-in method → Phone numbers for testing*.

### 3. Database
```bash
python manage.py migrate
python manage.py createsuperuser   # optional, for the admin
```

### 4. Frontend setup
```bash
cd ../frontend
npm install
# add your Firebase web config to src/firebase.js
```

### 5. Run all five processes (separate terminals)
```bash
# 1 — Redis (WSL / Linux)
sudo service redis-server start

# 2 — Backend (ASGI, serves API + WebSocket)
cd backend && source venv/Scripts/activate
daphne -b 127.0.0.1 -p 8000 config.asgi:application

# 3 — Frontend
cd frontend && npm run dev            # http://localhost:5173

# 4 — Celery worker (Windows needs the solo pool)
cd backend && source venv/Scripts/activate
celery -A config worker --pool=solo --loglevel=info

# 5 — Celery Beat (scheduler)
cd backend && source venv/Scripts/activate
celery -A config beat --loglevel=info
```

Open **http://localhost:5173**, sign in with your phone number, add holdings, and the dashboard updates automatically. Fetch prices manually any time with `python manage.py fetch_prices`.

---

## Testing

```bash
cd backend && source venv/Scripts/activate
python -m pytest
```
560+ automated tests cover the risk engine (each metric verified against known mathematical properties), authentication and data scoping, payments and signature verification, CSV/broker import validation, and graceful degradation on bad tickers.

---

## Risk Methodology

Given portfolio weights **w** and a matrix **R** of aligned daily returns:

- **Volatility** — σ = √(wᵀΣw) × √252
- **Beta** — Cov(rₚ, rₘ) / Var(rₘ)
- **Sharpe / Sortino** — excess return per unit of total / downside risk
- **VaR** — Historical (empirical percentile), Parametric (μ + z·σ), Monte Carlo (simulated tail)
- **CVaR** — expected loss beyond VaR
- **Max drawdown** — largest peak-to-trough decline
- **Concentration (HHI)** — Σ wᵢ²
- **Rebalancing** — minimize wᵀΣw subject to Σwᵢ = 1, wᵢ ≥ 0 (minimum-variance) + efficient frontier

Return histories are inner-joined on date before any calculation, so different trading calendars (e.g. index vs individual stocks) never silently misalign returns.

---

## Demo

A walkthrough video demonstrating the full flow — sign-in, holdings import, risk analytics, rebalancing, live alerts, AI insights and PDF export — is available here:

**Demo video:** _<add your unlisted YouTube / Google Drive link here>_

---

## What's Real vs Roadmap

**Working today:** phone-OTP auth, per-user portfolios, live valuation and full risk engine, Markowitz rebalancing, real-time WebSocket alerts, 60-second auto-refresh, CSV import, PDF export, Razorpay ₹9 gate, AI insights, and simulated multi-broker sync.

**On the roadmap:** live broker API sync (Zerodha Kite Connect), production SMS OTP + reCAPTCHA, multiple portfolios per user, stress testing, multi-currency support, and a mobile app.

We are transparent about what is simulated (broker sync uses sample data; Razorpay runs in test mode; reCAPTCHA is bypassed for test numbers in development) — and the architecture is built with swappable seams so each becomes production-ready without a rewrite.

---

## License

Built for the GLS Nexus Hackathon 2026. For evaluation purposes.
