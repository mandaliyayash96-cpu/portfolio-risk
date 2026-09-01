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

## Phone authentication

The browser owns the OTP flow: Firebase sends the SMS, the user types the code,
Firebase mints an **ID token**. This backend's only job is to *verify* that
token and map the phone number inside it to a user and their own portfolio. The
login UI does not exist yet — that is Part 2.

### Configuration

`backend/.env` (gitignored, never committed):

```
FIREBASE_CREDENTIALS=<service-account-file>.json   # sits in backend/, also gitignored
RAZORPAY_KEY_ID=...                                # loaded now, used in Part 2
RAZORPAY_KEY_SECRET=...                            # never leaves the server
```

`config/settings.py` loads this with python-dotenv **before** it reads anything
from the environment, and a real environment variable still wins over the file.
The service account is read once at startup by `accounts/apps.py`; the log line
`Firebase initialised from <file>` is the confirmation. Without the file the
process still boots — everything except `/api/auth/` works, and `/api/auth/`
answers `503 firebase_unavailable` rather than pretending the token was bad.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/session/` | Called the instant the OTP succeeds. Creates the user + their portfolio on a first login, returns `{user_id, phone, portfolio_id, first_login}`. |
| GET | `/api/auth/me/` | Boot-time "who am I". Pure read. |

Both take `Authorization: Bearer <firebase_id_token>` and **nothing else** — a
phone number in the body or a header is ignored. Only the number inside the
verified token is trusted.

```bash
curl -X POST http://127.0.0.1:8000/api/auth/session/      -H "Authorization: Bearer $ID_TOKEN"
curl http://127.0.0.1:8000/api/auth/me/      -H "Authorization: Bearer $ID_TOKEN"
```

No token → `401 not_authenticated`. Expired → `401 token_expired` (distinct
from `invalid_token`, so the dashboard can tell "sign in again" from "your
client is broken"). All of them arrive in the usual envelope.

The dashboard obtains `$ID_TOKEN` for you — see *Signing in from the dashboard*
below. To exercise the API by hand, copy a token out of the browser console
(`await firebase.auth().currentUser.getIdToken()` equivalents), or rely on the
test suite, which mocks the one verification call and asserts everything
downstream of it (`backend/accounts/tests/`).

### What this changes for the existing endpoints

`/api/risk/`, `/api/rebalance/`, `/api/performance/` and the holdings routes
now read **the caller's own portfolio** when the request carries a valid token,
whatever id the URL names. With no token they behave exactly as before, so
every `portfolio/1/` command in this file still works.

That fallback is deliberate and temporary — see the `TODO Part 3` markers in
`accounts/selectors.py` and each view.

---

## Signing in from the dashboard

The Vite app now opens on a phone sign-in screen and only shows the dashboard
once you are signed in. A returning user with a live Firebase session skips
straight through — no second OTP.

### Open it at `localhost`, not `127.0.0.1`

```
http://localhost:5173      ✅
http://127.0.0.1:5173      ❌  auth/unauthorized-domain
```

Firebase authorises `localhost` for every project by default; `127.0.0.1` is a
different host string and is not on that list. The login screen names this
error if you hit it, but it is easier to avoid. (The API is still reached at
`127.0.0.1:8000` — that is a different origin and CORS already allows it.)

### Firebase console, once per project

1. **Authentication → Sign-in method → Phone**: enable it. Without this every
   "Send OTP" fails with `auth/operation-not-allowed`.
2. **Authentication → Settings → Authorized domains**: `localhost` is there by
   default. Add your deployed domain when there is one.
3. **Test numbers** — **required for local development**, see the next
   section. Under **Authentication → Sign-in method → Phone → Phone numbers
   for testing**, click *Add phone number* and enter a pair:

   | Field | Example | Rules |
   |-------|---------|-------|
   | Phone number | `+91 99999 99999` | E.164, and it must be a number nobody actually owns |
   | Verification code | `123456` | Exactly six digits, fixed, yours to choose |

   That pair signs in with **no SMS, no charge and no rate limit**, and the
   backend still receives a real, verifiable ID token — so every gate
   downstream (session, portfolio, the ₹9 unlock) behaves exactly as it does
   for a real user. Up to ten numbers per project. Type the number into the
   login screen **exactly as registered**, and the fixed code in place of the
   SMS.

---

### No reCAPTCHA image challenge in development

Firebase will not send an OTP without an app-verification token, and on the web
that means reCAPTCHA. The widget is configured `size: 'invisible'`, but
*invisible* only means Google **may** stay out of the way — it decides per
attempt, and a developer submitting the same form from the same browser twenty
times an hour is the exact traffic shape its risk model distrusts. What you get
is the photo grid: *select all the crosswalks*, several rounds of it.

Running on `localhost`, the app now turns app verification off entirely:

```js
// src/firebase.js, at module scope
auth.settings.appVerificationDisabledForTesting = true
```

Firebase then swaps in a **mock** reCAPTCHA — no script fetched from Google, no
widget rendered, no challenge, ever. The login screen says so with a banner,
and the browser console logs it on load.

**The trade, and it is not optional:** with app verification off, only the
**test numbers** from step 3 above can sign in. A real number sends a mock
token, and Firebase rejects it. That is the whole reason step 3 is required
rather than a nice-to-have.

**To use a real number and a real SMS on localhost**, opt out:

```bash
# frontend/.env.local   (git-ignored; restart `npm run dev` after editing)
VITE_FIREBASE_TEST_MODE=false
```

Set it to `true` instead to force test mode on a dev host that is not
`localhost`.

#### It cannot reach production

Three guards, in `src/firebase.js`, and the first is the one that matters:

1. `import.meta.env.DEV` — Vite replaces this with the literal `false` when
   building, so the whole block is **dead code eliminated from the bundle**.
   Confirm it yourself after `npm run build`:

   ```bash
   cd frontend
   grep -c "VITE_FIREBASE_TEST_MODE" dist/assets/index-*.js   # -> 0
   ```
2. The hostname has to be `localhost` (or `::1`), so `npm run dev --host` on a
   LAN address does not silently pick it up.
3. `VITE_FIREBASE_TEST_MODE`, which overrides both ways, as above.

### What the screen does

| Step | What happens |
|------|--------------|
| Phone | `signInWithPhoneNumber` with an **invisible reCAPTCHA** — mocked entirely in development. The container div is always mounted, because unmounting it would take the widget with it. |
| Code | `confirm(code)` signs you into Firebase. For a test number this is the fixed code from the console, not an SMS. |
| Then | `POST /api/auth/session/` with the fresh ID token; the returned `portfolio_id` is what every panel on the dashboard reads. |

The header shows the signed-in number and a **Log out** button. The theme
toggle works on the login screen too.

### If "Send OTP" does nothing the second time

That is the classic reCAPTCHA symptom: the token is single-use, and a screen
that re-sends a spent one fails with `auth/invalid-app-credential`.

This app builds the verifier **once** and **resets** it in the `finally` of
every send, which retires the token and keeps the widget. (It used to destroy
and rebuild the widget instead. That also worked, and it was part of *why*
challenges appeared — a widget with no history on the page is a visitor with no
history, which is who Google shows the photo grid to.) See the comment at the
top of `src/auth/LoginScreen.jsx`.

So if you still see it, the cause is elsewhere — check the browser console for
the `auth/…` code and match it against `src/auth/firebase-errors.js`.

| Console code | Usually means |
|--------------|---------------|
| `auth/invalid-app-credential` | Test mode is **on** and you typed a real number — use a registered test number, or set `VITE_FIREBASE_TEST_MODE=false` |
| `auth/invalid-phone-number` | Not E.164 — it needs the `+` and the country code |
| `auth/unauthorized-domain` | You opened `127.0.0.1` instead of `localhost` |
| `auth/operation-not-allowed` | Phone sign-in is not enabled in the console |

### Where the config lives

`src/firebase.js`, in the source, on purpose. The `apiKey` there identifies the
project and authorises nothing: sign-in still needs a real OTP from an
authorised domain, and the backend verifies every token server-side before it
reads a row. The real secret — the service-account JSON — is in `backend/.env`
and cannot be reached from the browser.

---

## Paying to edit (₹9 per session)

Viewing the dashboard is free. **Editing holdings is not**: adding a position,
importing a CSV and deleting a row all require a paid unlock, bought through
Razorpay in **test mode**.

### When the user is asked

**At submit, never before.** The add form and the CSV import are always visible
and always usable, and the delete buttons are always on the holdings table.
Nothing in the UI is locked.

The client simply attempts the write. If the server answers **402**, the browser
parks that request, opens a modal summarising exactly what is about to be saved
("Add RELIANCE.NS ×10", "Import 5 holdings from sample.csv", "Remove TCS.NS"),
and offers one button: **Pay ₹9 & Save**. On a verified payment it re-runs the
same request, closes the modal and refreshes the dashboard. If a round is
already open the write just goes through and no modal appears.

Cancelling the Razorpay sheet changes nothing: no charge, and the form still
holds everything that was typed.

This is a **client** change only. The gate, the amount, the signature check and
the round lifecycle below are exactly as they were — the browser learns it has
to pay from the 402 it gets back, not from anything it decides for itself.

### What one ₹9 buys

One *editing round*. It starts when the payment signature verifies and ends at
whichever of these comes **first**:

| # | Ends when | Triggered by |
|---|-----------|--------------|
| 1 | The user presses **End round** on the Manage-holdings panel, or leaves the dashboard | the client (`POST /api/payments/finish/`) |
| 2 | A new order is created — it retires whatever was outstanding | the client |
| 3 | `EDITING_UNLOCK_TTL` (20 minutes) after payment | **nobody** — this is the backstop |

A write does **not** consume the grant, so one round holds as many edits as you
like. Condition 3 is what makes "one ₹9 cannot be reused forever" true no
matter what the browser does; the other two depend on the client behaving.

**Known gap, stated plainly:** between a reload and either condition 2 or 3 the
grant is still live on the server. The UI locks itself, but someone who reloads
and calls the API by hand could keep editing for the rest of those 20 minutes.
Closing that completely means binding the grant to a nonce that dies with the
page — which also charges ₹9 twice when a browser refreshes mid-round. The TTL
was judged the better trade. `EDITING_UNLOCK_TTL_MINUTES` in the environment
tightens it.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/payments/order/` | Creates a ₹9 order. Returns `{order_id, amount, currency, key_id}` — `key_id` is the **public** key the checkout widget needs. |
| POST | `/api/payments/verify/` | Body: `razorpay_order_id`, `razorpay_payment_id`, `razorpay_signature`. Verifies the HMAC against the key secret and only then marks the payment paid. |
| POST | `/api/payments/finish/` | Ends the round. Idempotent. |

All three need the Firebase Bearer token from Part 1.

The amount is a **server-side constant** (`EDITING_UNLOCK_AMOUNT_PAISE = 900`),
never read from the request: an amount the client can name is an amount the
client can set to zero.

### What a locked write looks like

```json
{"success": false, "data": null,
 "error": {"code": "payment_required",
           "message": "Editing is locked. Unlock a round of edits for ₹9 to continue.",
           "details": null}}
```

with HTTP **402**. A signed-out caller gets **401** instead — there is nobody
for a payment to belong to.

### Testing it

Razorpay test mode never charges a real card. In the checkout sheet use card
`4111 1111 1111 1111`, any future expiry, any CVV, and any name; for UPI, the
test VPA `success@razorpay`. The `rzp_test_…` key in `backend/.env` is what
keeps it in that mode — a live key would take real money with no other code
change, so check the prefix before deploying.

The suite proves the part that matters without a network: signature
verification is a local HMAC, so `payments/tests/` computes a real signature
with a test secret and lets the production verification code run against it.
Only order creation — an HTTPS call — is mocked.

---

## Tests

No test needs Redis, a worker, the network, a Firebase service account or a
Razorpay account — the provider is stubbed through
`settings.MARKET_DATA_PROVIDER`, the tasks are called as plain functions, token
verification is mocked at a single seam (`accounts.firebase.verify_token`), and
of the payments only order creation is mocked (signature verification is a
local HMAC and runs for real).

```bash
cd backend
python -m pytest -q
python -m pytest accounts -q      # just the auth work
python -m pytest payments -q      # the ₹9 gate
```
