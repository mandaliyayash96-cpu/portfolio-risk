"""App config for `accounts` - and the one place Firebase is booted."""

import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Accounts & phone authentication"

    def ready(self) -> None:
        """
        Initialise firebase-admin ONCE, at startup.

        `ready()` is Django's only hook that runs after settings are loaded and
        before the first request, in every process that serves anything -
        runserver, daphne, a Celery worker, pytest. Initialising lazily on the
        first authenticated request instead would put a certificate read and a
        file-system hit on a request path, and would race between threads.

        `init_firebase()` swallows its own failures (see its docstring): a
        deployment with no service-account file still boots, and only the auth
        endpoints answer 503.
        """
        from accounts import firebase

        firebase.init_firebase()
