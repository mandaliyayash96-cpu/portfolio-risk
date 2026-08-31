# Running the stack

Every command below is for **Git Bash on Windows**, run from the repository
root unless the block says otherwise. Copy them as written.

> **The one thing you cannot get wrong:** the Celery worker must be started
> with `--pool=solo`. See [Why `--pool=solo`](#why---poolsolo).

---

## Processes

Five, in this order. Each wants its own terminal.

| # | Process | What breaks without it |
|---|---------|------------------------|
| 1 | Redis | Everything below. Channels and Celery both need it. |
| 2 | Daphne (API + WebSocket) | The dashboard has no backend. |
| 3 | Celery **worker** | Tasks are queued and never executed. |
| 4 | Celery **beat** | Nothing is ever queued — prices go stale. |
| 5 | Vite dev server | No dashboard. |

Only 1, 2 and 5 are needed to *use* the app. 3 and 4 are what make prices
refresh and alerts fire **automatically**, which is Phase 8.

---

### 1. Redis

Must be listening on `127.0.0.1:6379`. Two logical databases are used and they
are deliberately separate:

- **DB 0** — Channels (the WebSocket alert feed)
- **DB 1** — Celery (broker + result backend)

Same server, different keyspaces, so `celery purge` or a stray `FLUSHDB` on one
cannot take the other down.

Check it is up:

```bash
redis-cli ping          # -> PONG
```

If you run Redis under WSL or Docker, start it however you normally do; the
rest of this file assumes it answers on that port.

---

### 2. Daphne — API and WebSocket

```bash
cd backend
source venv/Scripts/activate
daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

`runserver` also works for HTTP, but daphne is what actually serves the
WebSocket alert feed.

---

### 3. Celery worker — **the `--pool=solo` one**

```bash
cd backend
source venv/Scripts/activate
celery -A config worker --pool=solo --loglevel=info
```

You should see, in the banner:

```
.> transport:   redis://127.0.0.1:6379/1
.> results:     redis://127.0.0.1:6379/1
.> concurrency: 8 (solo)          <-- (solo) must appear here

[tasks]
  . alerts.scan_all_alerts
  . config.celery.debug_task
  . marketdata.refresh_all_prices
```

If `(solo)` is missing, stop and re-read the next section — the worker will
look healthy and do nothing.

---

### 4. Celery beat — the schedule

```bash
cd backend
source venv/Scripts/activate
celery -A config beat --loglevel=info
```

Beat writes its last-run state to `backend/celerybeat-schedule` (gitignored). To
force a clean schedule after changing intervals, stop beat and delete it:

```bash
rm -f backend/celerybeat-schedule*
```

Within about a minute you should see, in the **beat** terminal:

```
Scheduler: Sending due task refresh-all-prices (marketdata.refresh_all_prices)
Scheduler: Sending due task scan-all-alerts (alerts.scan_all_alerts)
```

and, in the **worker** terminal, those tasks being received and succeeding.

---

### 5. Frontend

```bash
cd frontend
npm install     # first time only
npm run dev
```

---

## Why `--pool=solo`

**Celery's default `prefork` pool does not work on Windows.**

It is built on `fork()`, which Windows has no equivalent of. The failure mode is
the expensive kind — *silent*. The worker:

- starts without an error,
- prints its banner,
- lists the registered tasks,
- connects to Redis,

and then **consumes nothing, forever**. Beat happily queues tasks, the queue
grows, and no task ever runs. It looks exactly like a broker or routing problem,
which is why people lose an afternoon to it.

`--pool=solo` runs tasks in the worker's own main thread and works correctly on
Windows.

**This is handled in two places, on purpose:**

1. The command above passes `--pool=solo` explicitly.
2. `config/settings.py` sets `CELERY_WORKER_POOL` to `solo` when `os.name ==
   "nt"`, so a worker started *without* the flag still works on this machine. It
   stays `prefork` on Linux, so a deployment is not crippled by a Windows
   workaround.

The flag and the setting agree. Keep the flag anyway — a command that works when
pasted into a fresh shell beats a default someone has to know about.

### What solo costs you

One execution thread, so **`refresh_all_prices` and `scan_all_alerts` never run
concurrently — they queue**. That happens to suit this pipeline (the scan wants
the prices the refresh just wrote), but two consequences are worth knowing:

- A slow yfinance poll delays the scan behind it. The beat entries carry
  `expires` (~55s), so a message the worker cannot reach within its interval is
  **discarded** rather than run late behind a backlog.
- `task_time_limit` / `task_soft_time_limit` are **not enforced** under solo —
  they need prefork's signals. They are set in settings for a future Linux
  deployment. On Windows the real bounds are `expires` above and the provider's
  own 20s per-request timeout in `marketdata/providers.py`.

`--pool=threads` also works on Windows if you want real concurrency; solo is the
recommendation here because it makes the serial ordering above guaranteed rather
than incidental.

---

## The schedule

Defined in `config/settings.py` as `CELERY_BEAT_SCHEDULE`. Intervals are
settings constants, all overridable by environment variable:

| Setting | Default | Effect |
|---|---|---|
| `PRICE_REFRESH_SECONDS` | 60 | how often `marketdata.refresh_all_prices` runs |
| `ALERT_SCAN_SECONDS` | 60 | how often `alerts.scan_all_alerts` runs |
| `ALERT_SCAN_OFFSET_SECONDS` | 10 | how long after the refresh the scan runs |

The offset is a real phase shift, not a hope: the scan's beat entry carries
`options: {"countdown": 10}`, so beat publishes on its own 60s tick and the
worker holds the message 10 seconds before executing it. That is what makes the
scan measure the prices the refresh just wrote instead of the previous minute's.

To run at a different cadence for a demo:

```bash
PRICE_REFRESH_SECONDS=30 ALERT_SCAN_OFFSET_SECONDS=5 celery -A config beat --loglevel=info
```

Beat reads the schedule at startup, so restart beat (and delete
`celerybeat-schedule`) after changing it.

---

## Verifying it works

**Is the worker actually consuming?** This is the prefork check — it prints in
the *worker's* terminal, not yours:

```bash
cd backend
python -c "
import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django; django.setup()
from config.celery import debug_task
print('queued:', debug_task.delay().id)
"
```

If nothing appears in the worker terminal within a second, the pool is wrong.

**Does a real refresh work end to end?** This one returns a summary:

```bash
cd backend
python -c "
import os, json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django; django.setup()
from marketdata.tasks import refresh_all_prices
print(json.dumps(refresh_all_prices.delay().get(timeout=180), indent=2))
"
```

Expect something like:

```json
{
  "ok": true,
  "tickers": 4,
  "live_fetched": 4,
  "history_written": 1008,
  "history_failed": 0,
  "errors": {}
}
```

`"ok": false` with entries in `errors` is a *degraded* run, not a crash — see
below.

---

## Running the jobs by hand

The management commands still exist and are unchanged. They call the same
services the tasks do, so they are the right tool for debugging one ticker
without waiting for a tick:

```bash
cd backend
python manage.py fetch_prices                              # everything held + benchmark
python manage.py fetch_prices --tickers RELIANCE.NS --days 60
python manage.py scan_alerts --dry-run                     # what would fire, writes nothing
python manage.py scan_alerts --portfolio 1
```

One deliberate difference between the command and the task: `scan_alerts` exits
**non-zero** when a portfolio could not be measured, because a human asked and
needs to know. The scheduled task reports the same condition as a `skipped`
entry and **succeeds** — an unmeasurable portfolio is a standing state, and an
alarm that fires every 60 seconds forever is the same as no alarm.

---

## When something fails

**A fetch failure never crashes the worker and never damages stored prices.**
Both tasks return a summary dict instead of raising:

- `refresh_all_prices` catches per-ticker provider errors and whole-feed
  outages, logs them, and reports them in `errors` keyed by
  `"TICKER (live)"` / `"TICKER (history)"`.
- `scan_all_alerts` catches per portfolio, so one unmeasurable portfolio does
  not stop the others.

Raising would be worse: Celery's answer to a raised task is a retry, and
retrying into a rate limit is how a poll becomes a ban. **The next tick is the
retry.**

Nothing is deleted on failure. `fetch_live` writes a snapshot only for tickers
the provider answered for, and `fetch_history` raises before writing when the
frame comes back empty — so a failed poll leaves the last good prices in place
and the dashboard keeps showing them with their `last close` tag.

| Symptom | Cause | Fix |
|---|---|---|
| Worker starts, tasks queue, nothing runs | prefork pool on Windows | `--pool=solo` |
| `Cannot connect to redis://...` | Redis not running | start Redis, check `redis-cli ping` |
| Beat logs "Sending due task", worker silent | worker not running, or wrong pool | see above |
| `"ok": false`, errors mention rate limiting | yfinance throttling | nothing to do; next tick retries |
| Alerts never fire | no active rules on the portfolio | add one in the dashboard's Alerts panel |
| Events created but `broadcast` < `created` | Redis reachable for Celery, not Channels | check `REDIS_URL` (DB 0) |

---

## Tests

No test needs Redis, a worker, or the network — the provider is stubbed through
`settings.MARKET_DATA_PROVIDER` and the tasks are called as plain functions.

```bash
cd backend
python -m pytest -q
```
