"""
App config for the risk package.

Registered in INSTALLED_APPS from Phase 4 on: the package now ships Django-aware
modules (services, views, urls) and will ship management commands later. It has
no models - `engine.py` stays pure and importable without Django either way.
"""

from django.apps import AppConfig


class RiskConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "risk"
    verbose_name = "Risk analytics"
