"""
The Celery application.

One app object for the whole project, built here and imported by
`config/__init__.py` so that merely importing `config` registers it - which is
what makes `@shared_task` in marketdata/ and alerts/ bind to THIS app rather
than to a default that has never read our settings.

CONFIGURATION LIVES IN settings.py, NOT HERE
--------------------------------------------
`config_from_object("django.conf:settings", namespace="CELERY")` means every
Celery option is a Django setting prefixed with CELERY_: CELERY_BROKER_URL sets
`broker_url`, CELERY_BEAT_SCHEDULE sets `beat_schedule`, and so on. So there is
exactly one file to read to know how the workers behave, and it is the same
file that already describes the Channels layer they share a Redis server with.

WHICH REDIS
-----------
Database 1, while Channels uses database 0 (settings.REDIS_URL). Same server,
different keyspace. That is deliberate: `celery purge` and a stray FLUSHDB are
routine operations, and neither should be able to take the live alert feed's
groups with it.

WINDOWS
-------
Nothing here selects the worker pool, because the pool is a launch-time
decision - see settings.CELERY_WORKER_POOL for the default and RUN.md for the
command line. The short version: the default prefork pool does not work on
Windows and must be `solo`.
"""

import os

from celery import Celery

# Must be set BEFORE the app is constructed: autodiscovery walks
# settings.INSTALLED_APPS, which cannot be read until Django knows its settings
# module. `setdefault` rather than assignment so an explicit
# DJANGO_SETTINGS_MODULE in the environment still wins.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("portfolio_risk")

# Every CELERY_* Django setting becomes a Celery option, minus the prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Imports <app>/tasks.py for each entry in INSTALLED_APPS: marketdata.tasks and
# alerts.tasks today. A task module that fails to import is a worker that
# starts fine and silently never runs that task, so `manage.py check` in RUN.md
# is worth the extra line before starting anything.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """
    Prove the round trip: broker reachable, worker consuming, pool working.

    Worth having on Windows specifically. A prefork worker there accepts this
    task and never executes it, so `debug_task.delay()` returning a task id
    while nothing is ever printed is the fastest way to confirm the pool is
    the problem rather than the broker. See RUN.md.
    """
    print(f"debug_task request: {self.request!r}")
