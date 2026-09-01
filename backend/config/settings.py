"""
Django settings for the Investor Portfolio Monitoring & Risk Management System.

Phase 1: project config only. SQLite, DRF wired to the standard JSON envelope.
Phase 6 activated the Channels/Redis seam and Phase 8 the Celery one - both are
live below. PostgreSQL is still a TODO.

Generated with Django 6.1.
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# backend/  (manage.py lives here)
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Secrets — backend/.env, which is gitignored and never committed.
#
# Loaded HERE, before the first os.environ.get() below, because everything in
# this file reads the environment at import time. load_dotenv does NOT override
# variables that are already set, so a real environment (a container, a CI
# runner, a systemd unit) still wins over the developer's file.
#
# Nothing in this project logs a secret. The Firebase service-account JSON and
# both Razorpay keys are read into settings and passed to their SDKs; the only
# thing that ever reaches a log line is the credentials PATH.
# ---------------------------------------------------------------------------
load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
# SECURITY WARNING: the fallback is a dev-only key. Set DJANGO_SECRET_KEY in prod.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-ofbage(02mwe54t=(d)w=yzi&gmg10u2n(+5bx51tg0&5x4fl5",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
# Daphne must come FIRST in INSTALLED_APPS, ahead of django.contrib.staticfiles.
# Its AppConfig is what replaces `runserver` with the ASGI-capable version; the
# staticfiles app ships a runserver override too, and whichever app is listed
# later wins. Listed after staticfiles, `manage.py runserver` would quietly go
# back to serving WSGI only and every WebSocket handshake would 404.
ASGI_APPS = ["daphne"]

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",  # the Vite dev server on :5173 is a cross-origin caller
    "channels",     # WebSocket alert feed (alerts/consumers.py)
]

LOCAL_APPS = [
    "common",       # envelope, exception handler, abstract base models
    "accounts",     # phone identity (Firebase) -> AppUser -> their portfolio
    "payments",     # Razorpay ₹9 editing unlocks
    "portfolio",    # Portfolio / Holding / Transaction
    "marketdata",   # PriceSnapshot / PriceHistory  (+ provider interface in Phase 2)
    "alerts",       # AlertRule / AlertEvent + evaluator, consumer, scan cmd
    "risk",         # pure engine + risk services   (+ optimizer in Phase 5)
]

INSTALLED_APPS = ASGI_APPS + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # As high as possible, and necessarily above CommonMiddleware: CORS headers
    # must be attached even to responses that CommonMiddleware short-circuits
    # (an APPEND_SLASH redirect), or the browser reports a CORS failure instead
    # of the real status.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
# Points at the ProtocolTypeRouter in config/asgi.py, not at a bare Django app:
# this is what routes an incoming "websocket" scope to alerts/routing.py while
# leaving "http" scopes with Django. Read by daphne and by runserver alike.
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Database — SQLite for this phase.
# TODO Phase 8: swap ENGINE to django.db.backends.postgresql and read from env.
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Portfolio.user points at settings.AUTH_USER_MODEL, never at auth.User directly,
# so a custom user model stays a cheap swap later.
AUTH_USER_MODEL = "auth.User"


# ---------------------------------------------------------------------------
# Django REST Framework — every response leaves through the {success,data,error}
# envelope: successes via the renderer, failures via the exception handler.
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "common.exceptions.custom_exception_handler",
    "DEFAULT_RENDERER_CLASSES": (
        ["common.renderers.EnvelopeJSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer"]
        if DEBUG
        else ["common.renderers.EnvelopeJSONRenderer"]
    ),
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # FIRST, for two reasons. It is the scheme the SPA actually uses, and
        # DRF asks the FIRST authenticator in this list for the
        # WWW-Authenticate challenge - which is what makes a rejected token a
        # 401 rather than DRF's silent downgrade to 403.
        #
        # It DECLINES (returns None) when there is no Bearer header, so every
        # AllowAny endpoint still serves anonymous callers exactly as before;
        # adding it here changes nothing until a request carries a token.
        "accounts.authentication.FirebaseAuthentication",
        # Kept for /admin and the browsable API. Ordered second so an admin
        # session never shadows a Bearer token.
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Hackathon MVP: open so Phases 2-4 are testable with curl/Postman. The
    # auth endpoints set their own permission (accounts.permissions.IsAppUser),
    # so this default does not reach them.
    # TODO Part 3: flip to IsAuthenticated and drop the URL-id fallback in
    # accounts.selectors.resolve_portfolio_id, which is the last thing letting
    # an anonymous caller read a portfolio by guessing its id.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PAGINATION_CLASS": None,  # TODO Phase 4: paginate transaction history
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}


# ---------------------------------------------------------------------------
# CORS - development only.
#
# The React dashboard runs on the Vite dev server at :5173 while the API runs at
# :8000. Different port means different origin, so the browser preflights every
# XHR and drops the response unless the API opts that origin in.
#
# Both spellings of localhost are listed because "localhost" and "127.0.0.1" are
# distinct origins to a browser, and Vite prints the first while some setups
# open the second.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

# Cookies are not sent cross-origin: the API is AllowAny for now and the
# dashboard authenticates with nothing. Flipping this on later (for session
# auth from the SPA) also requires CSRF_TRUSTED_ORIGINS.
CORS_ALLOW_CREDENTIALS = False

# Content-Disposition is SENT on the PDF report either way - a browser
# navigating straight to the URL downloads it correctly with no help from here.
# But the dashboard fetches it with XHR, and cross-origin JavaScript can only
# read the six CORS-safelisted response headers unless the server names the
# others explicitly. Without this line `response.headers['content-disposition']`
# is undefined in the browser, and every download silently falls back to a
# generic filename instead of "risk-report-my-demo-2026-08-31.pdf".
#
# Invisible from curl, which is not subject to CORS at all: the header is
# present in the response whether or not this setting exists. It only affects
# what the BROWSER lets the page see.
CORS_EXPOSE_HEADERS = ["Content-Disposition"]

# TODO Phase 8 (prod): serve the built SPA from the same origin as the API so
# CORS stops applying at all, or read this list from an env var pinned to the
# deployed domain. Never CORS_ALLOW_ALL_ORIGINS = True outside local dev.


# ---------------------------------------------------------------------------
# Auth / i18n / static
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"

# NSE trading calendar is IST; timestamps are still stored in UTC (USE_TZ=True).
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
    },
}


# ---------------------------------------------------------------------------
# Logging — unhandled API errors are logged by common.exceptions before the
# 500 envelope is returned, so the traceback still reaches the console.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[{levelname}] {asctime} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------
# yfinance symbol conventions: Indian equities carry a .NS suffix; the NIFTY 50
# benchmark is ^NSEI. Nothing outside marketdata may import yfinance (rule 4).
DEFAULT_BENCHMARK_TICKER = "^NSEI"

# Swappable market data feed (architecture rule 4). Dotted path to a
# MarketDataProvider subclass; tests point this at a stub.
MARKET_DATA_PROVIDER = os.environ.get(
    "MARKET_DATA_PROVIDER", "marketdata.providers.YFinanceProvider"
)
DEFAULT_BASE_CURRENCY = "INR"
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.065  # annualised; ~India 10Y G-Sec. TODO Phase 3: make configurable.


# ---------------------------------------------------------------------------
# Firebase — phone authentication.
#
# The browser owns the OTP flow entirely (Firebase sends the SMS and mints an
# ID token); this backend's only job is to VERIFY that token, which is what the
# service account below is for. It is read once at startup by
# accounts/apps.py -> accounts.firebase.init_firebase().
#
# The value in .env is a bare filename resolved against BASE_DIR, so the file
# lives beside manage.py and is matched by .gitignore's *firebase-adminsdk*.json
# rule. An empty or missing value is not fatal: the process boots, logs a
# warning, and the auth endpoints answer 503 while everything else works.
# ---------------------------------------------------------------------------
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")

# ---------------------------------------------------------------------------
# Razorpay — loaded now, used in Part 2 (subscription/checkout).
#
# The SECRET signs and verifies payment callbacks and must never leave the
# server: it is not exposed through any endpoint, never rendered into a
# template, and never logged. The KEY_ID is public by design (the browser
# checkout widget needs it) but is still read from the environment so the two
# stay together and neither is ever typed into source.
# ---------------------------------------------------------------------------
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# ---------------------------------------------------------------------------
# The editing unlock — what ₹9 buys.
#
# PAISE, NOT RUPEES. Razorpay's API takes an integer number of the smallest
# unit and its signature covers that integer, so 900 is the number that goes
# over the wire and the number stored on Payment. See payments/models.py for
# why this one figure is deliberately not a Decimal.
#
# The amount lives here rather than in the request body because an amount the
# client sends is an amount the client can set to 1.
# ---------------------------------------------------------------------------
EDITING_UNLOCK_AMOUNT_PAISE = int(os.environ.get("EDITING_UNLOCK_AMOUNT_PAISE", "900"))
EDITING_UNLOCK_CURRENCY = os.environ.get("EDITING_UNLOCK_CURRENCY", "INR")

#: How long one paid editing round may stay open.
#:
#: The other two ways a round ends - the user closes the panel, or starts a new
#: order - are both triggered by the CLIENT. This one is not, which is what
#: makes "one ₹9 cannot be reused forever" true whatever the browser does. Long
#: enough to paste a CSV and fix two typos; short enough that a tab left open
#: overnight is not a standing licence. See payments/services.py.
EDITING_UNLOCK_TTL = timedelta(
    minutes=int(os.environ.get("EDITING_UNLOCK_TTL_MINUTES", "20"))
)

# ---------------------------------------------------------------------------
# Channels - the transport under the live alert feed.
#
# Redis rather than the in-memory layer, because the two halves of an alert run
# in DIFFERENT PROCESSES: `manage.py scan_alerts` detects the breach, while the
# browser's socket is held open by daphne. InMemoryChannelLayer is per-process,
# so a group_send from the command would reach nobody. Redis is the shared bus
# that lets one process fan out to sockets held by another.
#
# `capacity` is per channel: a browser tab that stops reading backs up here, and
# once full the layer drops the oldest messages rather than blocking the scan.
# `expiry` discards anything undelivered after a minute - a stale breach is
# worse than a missing one on a dashboard that re-snapshots on reconnect.
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

#: How long a redis-py socket may wait on a read before it gives up.
#:
#: This is NOT a tuning knob - it is a required fix for a version interaction
#: between the two pinned libraries, and lowering it below 5 breaks the feed:
#:
#:   * channels_redis 4.3.0 parks an idle consumer on BZPOPMIN with
#:     `brpop_timeout = 5`, so Redis holds the connection open for 5s at a time
#:     whenever no message is waiting - which is the normal state of an alert
#:     socket.
#:   * redis-py 8.x introduced DEFAULT_SOCKET_TIMEOUT = 5. channels_redis was
#:     written against older versions where the default was None (wait forever)
#:     and so never sets it.
#:
#: Both timers are therefore 5 seconds and they race on every idle poll. The
#: client usually wins, redis.exceptions.TimeoutError escapes into Channels'
#: dispatch loop, and the consumer dies - the browser sees close code 1011 a few
#: seconds after connecting, on a socket that is working perfectly.
#:
#: 30s is comfortably clear of the 5s blocking pop while still noticing a Redis
#: that has genuinely stopped answering.
REDIS_SOCKET_TIMEOUT = 30

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            # A dict entry (rather than a bare URL string) is passed straight
            # through to redis.asyncio.ConnectionPool.from_url as kwargs, which
            # is the only way to reach socket_timeout from here.
            "hosts": [{"address": REDIS_URL, "socket_timeout": REDIS_SOCKET_TIMEOUT}],
            "capacity": 500,
            "expiry": 60,
        },
    }
}

# The test suite swaps this for channels.layers.InMemoryChannelLayer
# (alerts/tests/conftest.py), so `pytest` never needs a running Redis.

# ---------------------------------------------------------------------------
# Celery - the scheduler that keeps prices fresh without anyone typing a command.
#
# Every option here is read by config/celery.py through the CELERY_ namespace,
# so `CELERY_BROKER_URL` below IS Celery's `broker_url`.
#
# DATABASE 1, NOT 0
# -----------------
# The broker shares the Redis SERVER with the Channels layer above but not its
# keyspace. `celery purge`, a stuck queue drained by hand, a stray FLUSHDB -
# all routine, and none of them should be capable of dropping the alert feed's
# groups. Same reasoning the Channels block gives for its own settings: the two
# systems fail independently or they are not two systems.
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)

CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

#: Results are diagnostics, not data. A task lands every 60s and the useful
#: question is only ever "what did the last few do", so they expire in an hour
#: rather than Celery's default day.
CELERY_RESULT_EXPIRES = 3600
CELERY_TASK_TRACK_STARTED = True

#: Redis is on the same machine, but a worker started before it is up should
#: wait rather than exit - beat and the worker are launched by hand in RUN.md
#: and the order is easy to get wrong.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# ---------------------------------------------------------------------------
# WINDOWS: THE WORKER POOL
#
# Celery's default `prefork` pool DOES NOT WORK ON WINDOWS. It depends on
# fork(), which Windows has no equivalent of. The failure is silent and
# expensive to diagnose: the worker boots, prints its banner, lists the
# registered tasks, connects to Redis - and then consumes nothing at all. It
# looks exactly like a broker problem, and it is not.
#
# So the pool defaults to `solo` on Windows (os.name == "nt") and stays
# `prefork` everywhere else, which keeps a Linux deployment on real
# concurrency. RUN.md ALSO passes --pool=solo explicitly, because a command
# line that works when copied into a fresh shell is worth more than a default
# somebody has to know about; the flag and this setting agree.
#
# The cost of solo is real and worth knowing: ONE execution thread, so
# refresh_all_prices and scan_all_alerts never overlap - they queue. That suits
# this pipeline (the scan wants the prices the refresh just wrote) but it means
# a slow yfinance poll delays the scan behind it, which is what `expires` in
# the beat schedule below exists to bound.
# ---------------------------------------------------------------------------
CELERY_WORKER_POOL = os.environ.get(
    "CELERY_WORKER_POOL", "solo" if os.name == "nt" else "prefork"
)

#: Acknowledge on receipt, not on completion: a queued poll that was
#: interrupted mid-run is worthless - the next tick is 60 seconds away and
#: carries fresher data than the retry would.
CELERY_TASK_ACKS_LATE = False

#: Time limits are enforced by the PREFORK pool's signals, so on Windows under
#: --pool=solo they are inert - a hung yfinance call cannot be interrupted
#: here. They are set for a Linux deployment, where they matter. On Windows the
#: real protection is `expires` in the schedule below plus the provider's own
#: 20s per-request timeout (marketdata/providers.py).
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 300

# ---------------------------------------------------------------------------
# Beat schedule
#
# Two jobs, in this order and for this reason: refresh writes prices, scan
# measures them. A scan that runs on the same tick as the refresh would be
# measuring the PREVIOUS minute's prices, so it is published with a countdown -
# beat sends the message on its own 60s tick and the worker holds it for
# ALERT_SCAN_OFFSET_SECONDS before executing. That is a genuine phase offset
# rather than a hope that one task finishes before the other starts.
#
# `expires` on both entries is the anti-pile-up. If the worker cannot reach a
# message within its interval - a yfinance stall, a solo pool busy with the
# previous poll - the message is DISCARDED rather than run late behind a
# backlog. Running a stale poll three minutes after it was scheduled has no
# value when a fresh one is already queued behind it.
# ---------------------------------------------------------------------------
#: How often prices are refreshed.
PRICE_REFRESH_SECONDS = int(os.environ.get("PRICE_REFRESH_SECONDS", "60"))
ALERT_SCAN_SECONDS = int(os.environ.get("ALERT_SCAN_SECONDS", "60"))

#: How long after the refresh the scan runs. Long enough that the common case
#: (a handful of tickers, all reachable) has finished writing.
ALERT_SCAN_OFFSET_SECONDS = int(os.environ.get("ALERT_SCAN_OFFSET_SECONDS", "10"))


def _expires_after(interval_seconds: int) -> int:
    """
    Drop a message that is nearly as old as the gap between messages.

    Five seconds of slack so a poll that starts a moment late still runs; below
    a floor of 5s the whole schedule is being run at a cadence this project
    does not support anyway.
    """
    return max(interval_seconds - 5, 5)


CELERY_BEAT_SCHEDULE = {
    "refresh-all-prices": {
        "task": "marketdata.refresh_all_prices",
        "schedule": timedelta(seconds=PRICE_REFRESH_SECONDS),
        "options": {"expires": _expires_after(PRICE_REFRESH_SECONDS)},
    },
    "scan-all-alerts": {
        "task": "alerts.scan_all_alerts",
        "schedule": timedelta(seconds=ALERT_SCAN_SECONDS),
        "options": {
            # The offset. See the block comment above.
            "countdown": ALERT_SCAN_OFFSET_SECONDS,
            "expires": _expires_after(ALERT_SCAN_SECONDS),
        },
    },
}

# TODO Phase-later: django-celery-beat, once schedules need to be editable at
# runtime instead of at deploy time. The static dict above is the right shape
# while there are two jobs and both are infrastructure.
