"""
Django settings for the Investor Portfolio Monitoring & Risk Management System.

Phase 1: project config only. SQLite, DRF wired to the standard JSON envelope.
Celery / Redis / Channels / PostgreSQL seams are marked with TODO and stay inert.

Generated with Django 6.1.
"""

import os
from pathlib import Path

# backend/  (manage.py lives here)
BASE_DIR = Path(__file__).resolve().parent.parent


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
        "rest_framework.authentication.SessionAuthentication",
        # TODO Phase 4: add TokenAuthentication / JWT for the React client.
    ],
    # Hackathon MVP: open so Phases 2-4 are testable with curl/Postman.
    # TODO Phase 4: flip to IsAuthenticated and scope every queryset by request.user.
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

# TODO Phase 2/6: Celery + Redis. The broker can share the host above; give
# Celery its own database number so a FLUSHDB on either never eats the other.
# CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
# CELERY_RESULT_BACKEND = CELERY_BROKER_URL
# CELERY_TIMEZONE = TIME_ZONE
# CELERY_BEAT_SCHEDULE = {...}   # poll_prices every 60s, scan_alerts every 60s
