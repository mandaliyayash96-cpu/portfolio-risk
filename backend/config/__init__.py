"""
Package init for the Django project config.

Importing the Celery app here is not decoration - it is what makes the app
exist before anything else runs. Django imports `config` on startup (it is the
parent package of the settings module), so this line guarantees that by the
time any app module is loaded, `@shared_task` has a configured app to bind to.

Without it, `@shared_task` falls back to a default app that has never read our
settings: the tasks would still be importable, `manage.py` would still work,
and the failure would only show up as a worker connecting to amqp://localhost
instead of Redis, or beat scheduling tasks nobody consumes.
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
