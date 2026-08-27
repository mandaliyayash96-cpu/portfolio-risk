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
    # TODO Phase 6: "channels" (WebSocket alert feed)
    # TODO Phase 7: "corsheaders" (React dev server on :5173)
]

LOCAL_APPS = [
    "common",       # envelope, exception handler, abstract base models
    "portfolio",    # Portfolio / Holding / Transaction
    "marketdata",   # PriceSnapshot / PriceHistory  (+ provider interface in Phase 2)
    "alerts",       # AlertRule / AlertEvent        (+ scan task in Phase 6)
    "risk",         # pure engine + risk services   (+ optimizer in Phase 5)
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # TODO Phase 7: "corsheaders.middleware.CorsMiddleware" goes above CommonMiddleware
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

# TODO Phase 2/6: Celery + Redis
# CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# CELERY_RESULT_BACKEND = CELERY_BROKER_URL
# CELERY_TIMEZONE = TIME_ZONE
# CELERY_BEAT_SCHEDULE = {...}   # poll_prices every 60s, scan_alerts every 60s

# TODO Phase 6: Channels layer
# CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", ...}}
