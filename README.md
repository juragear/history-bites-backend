# HistoryBites Backend

One-fact-a-day history app backend. FastAPI + Postgres + Firebase Cloud
Messaging, deployed on Railway with native cron.

The pipeline picks a Wikipedia article from a curated `(category, region, era)`
seed list, asks Gemini to extract one surprising fact in its own words, parks
the candidate in a review pool, and (after human approval) schedules it for a
specific calendar date. A daily cron pushes that day's fact to all FCM topic
subscribers; the Android app and the public `/today` endpoint both read the
same row. Generation runs ahead of delivery so a Gemini outage at 9am doesn't
miss a day — tomorrow's fact was already generated and approved hours or days
ago.

## Architecture at a Glance

- **FastAPI app** (`app/main.py`) — public read endpoints + structured JSON logging.
- **Postgres** (Railway addon) — `facts` (delivered, one per day) and `pool`
  (review queue) tables. SQLAlchemy 2.x ORM, Alembic migrations.
- **Wikipedia client** (`app/wikipedia.py`) — `categorymembers` + REST extract.
- **Model provider** (`app/model_provider.py`) — `Protocol` + Gemini (prod) and
  Ollama (local dev). See **D16**.
- **FCM push** (`app/fcm.py`) — single dual-platform `Message` (Android + APNS
  config) sent to the `daily-fact` topic. See **D17**, **D22**.
- **Generation pipeline** (`app/generation.py`) — orchestrates Wikipedia →
  provider → validation → `pool` insert. Variety-aware scheduler picks the
  next approved row out of `pool` into `facts`.
- **Admin endpoints** (`app/admin.py`) — bearer-auth review UI, manual
  schedule/retract, force-generate, push trigger.
- **Cron entry points** (`app/cron.py`) — `run_generation` (every 6h) and
  `run_push` (00:00 UTC). Run via `python -m app.cron <subcommand>` from
  Railway native cron.

See [`DECISIONS.md`](./DECISIONS.md) for architectural decisions and
**Notion → Backend Architecture** for the full spec.

## Local Development

### Prerequisites

- Python 3.12+
- Postgres 14+ (locally OR use Railway's `DATABASE_PUBLIC_URL` from the
  Postgres service)
- A Firebase project with a generated service account JSON
- A Gemini API key, OR an Ollama daemon running locally with `gemma4:latest`

### Setup

```bash
git clone git@github.com:juragear/history-bites-backend.git
cd history-bites-backend

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Fill in DATABASE_URL, WIKIPEDIA_USER_AGENT, GEMINI_API_KEY,
# FIREBASE_SERVICE_ACCOUNT_JSON, ADMIN_TOKEN at minimum.

alembic upgrade head
```

### Run tests

```bash
pytest -q
```

The suite is 80 tests, runs in ~2.5s, hits no external services. See
`tests/conftest.py` for the in-memory SQLite fixture wiring (StaticPool +
shared connection + a few SQLite-vs-Postgres compile hooks; see
**Troubleshooting**).

### Run locally

```bash
uvicorn app.main:app --reload
# → http://127.0.0.1:8000/health
```

## Deployment (Railway)

Push to `main` triggers a redeploy. The Railway-side `startCommand` runs
`alembic upgrade head` before booting uvicorn, so schema migrations land
automatically with each deploy.

```toml
# railway.toml
[deploy]
startCommand = "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT"
```

Two `[[cron]]` blocks fire `python -m app.cron run_generation` (every 6h) and
`python -m app.cron run_push` (00:00 UTC) in fresh containers that share the
deploy's image + env.

The repo uses an SSH remote (`git@github.com:juragear/...`). HTTPS picks up
the wrong GitHub credential on this machine — see Troubleshooting.

> **CRITICAL:** Set required env vars BEFORE pushing code that depends on
> them, using `--skip-deploys`. Otherwise the new pod crashes on `Settings()`
> boot. See Troubleshooting.

```bash
railway variables --set ADMIN_TOKEN=<value> --skip-deploys
railway variables --set FIREBASE_SERVICE_ACCOUNT_JSON='<compact-json>' --skip-deploys
git push origin main
```

## Environment Variables

`app/config.py` is the source of truth — pydantic-settings loads `.env` on
import and crashes loudly if a required var is missing.

| Name | Required | Default | Purpose |
|------|----------|---------|---------|
| `ENVIRONMENT` | no | `development` | Logged at startup; future env-switching label. |
| `LOG_LEVEL` | no | `INFO` | Python logging level. |
| `DATABASE_URL` | **yes** | — | Postgres connection string. `postgresql://` is rewritten to `postgresql+psycopg://` at boot. |
| `WIKIPEDIA_USER_AGENT` | **yes** | — | Required by Wikipedia's User-Agent policy. Format: `AppName/Version (contact)`. |
| `MODEL_PROVIDER` | no | `gemini` | `gemini` (prod) or `ollama` (local dev). See **D16**. |
| `GEMINI_API_KEY` | conditional | — | Required when `MODEL_PROVIDER=gemini`. |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | |
| `OLLAMA_BASE_URL` | no | `http://localhost:11434` | Used only when `MODEL_PROVIDER=ollama`. |
| `OLLAMA_MODEL` | no | `gemma4:latest` | |
| `PROMPT_VERSION` | no | `v1` | Stored on every generated fact. Bump and `/admin/flush-pool` after prompt edits. |
| `ADMIN_TOKEN` | **yes** | — | Bearer token for `/admin/*`. App refuses to boot without it. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | **yes** | — | Full service-account JSON as a single-line string. |
| `FCM_TOPIC` | no | `daily-fact` | Topic the daily push lands on. See **D17**. |
| `ALERT_WEBHOOK_URL` | no | (unset) | Slack/Discord-compatible webhook for cron alerts. |
| `REVIEW_QUEUE_TARGET` | no | `20` | Pending-review topup target for `run_generation`. |
| `APPROVED_ALERT_THRESHOLD` | no | `3` | Alert floor on approved pool count. See **D8**. |
| `CORS_ORIGINS` | no | `*` | Comma-separated origins. Tighten in production. |

## API Surface

### Public endpoints

- `GET /today` — today's fact, or the most recent past fact with
  `is_stale=true` if today's row is missing. Cached 5 min, keyed by ISO date
  (auto-evicts at midnight per **D21c**), busted on schedule/retract.
- `GET /archive?limit=30` — recent facts, newest first, retracted excluded.
  `limit` is 1..100.
- `GET /health` — `{ status, db, pool_pending_count, pool_approved_count,
  latest_scheduled_date, last_push_at }`. Returns 503 if the DB probe fails.

### Admin endpoints (Bearer auth)

Auth: `Authorization: Bearer $ADMIN_TOKEN`, or `?token=...` query param, or
hidden `token` form field (so the HTML review page can submit reviews).

- `POST /admin/generate` — force one pool generation cycle.
- `POST /admin/flush-pool` — delete all `pending_review` rows. Use after
  prompt changes.
- `POST /admin/schedule/{pool_id}/{date}` — pin a specific approved pool item
  to a specific date. Used during launch bootstrap (**D21d**).
- `POST /admin/retract/{date}` — set `is_retracted=TRUE` on the fact for that
  date. "No new views," not recall — see **D21d**.
- `GET /admin/review` — Jinja-rendered HTML review page.
- `POST /admin/review/{id}` — `{action: 'approve'|'reject'}` JSON, or
  `action=...` form post (HTML page uses 303 redirect).
- `POST /admin/push` — manually fire `run_push`.
- `POST /admin/cron/run-generation` — manually fire `run_generation`. Returns
  the same summary dict the cron logs.

## Cron

Two Railway native cron entries (see `railway.toml`):

- `run_generation` every 6 hours — schedules tomorrow if not already done,
  tops up the review queue to `REVIEW_QUEUE_TARGET`, alerts if approved drops
  below `APPROVED_ALERT_THRESHOLD`.
- `run_push` daily at 00:00 UTC — sends the FCM push for today's fact to the
  `daily-fact` topic.

Both are also runnable as `python -m app.cron run_generation|run_push`. Exit
codes: 0 on success (including `run_push` no-fact-today, which is just an
alert), 1 on unhandled exception, 2 on bad CLI args.

## Troubleshooting

Things that ate time during build-out and would eat time again next time.

- **GitHub HTTPS auth picks up the wrong account on this machine.** Always
  use the SSH remote: `git@github.com:juragear/history-bites-backend.git`.
- **Postgres driver scheme.** Railway's `DATABASE_URL` is `postgresql://...`
  but SQLAlchemy 2.x with psycopg v3 needs `postgresql+psycopg://...`.
  `app/db.py:_normalize_url` rewrites the prefix at engine creation. Don't
  remove that helper.
- **Required env vars crash boot if missing.** pydantic-settings validates on
  import. Always `railway variables --set NAME=value --skip-deploys` BEFORE
  pushing code that references the var, or the new pod crashlooks until you
  manually set it from the dashboard.
- **Firebase service-account JSON is multi-line.** Compact to a single line
  before pasting into Railway:
  ```bash
  python -c "import json; print(json.dumps(json.load(open('sa.json')), separators=(',', ':')))"
  ```
  The `private_key` field's `\n` escapes survive the round-trip and
  `json.loads()` decodes them at use time.
- **Railway CLI rejects empty strings.** `railway variables --set NAME=`
  errors out. For optional vars with `str | None = None` defaults
  (`ALERT_WEBHOOK_URL`, etc.), leave them unset entirely.
- **Tests use SQLite, prod uses Postgres.** A few divergences are absorbed in
  `tests/conftest.py`: a `@compiles(BigInteger, "sqlite")` hook so
  autoincrement PKs work, and explicit `is_retracted=False` in row helpers
  because SQLite stores `server_default="false"` as literal text rather than
  boolean `0`. `FOR UPDATE SKIP LOCKED` (D21a) is a no-op on SQLite — that
  race is only meaningful in production and isn't covered by unit tests.
- **`SessionLocal` namespace rebinding.** `cron.py` and `main.py` do
  `from app.db import SessionLocal` at import time, which copies the name
  into each module's globals. Tests that swap the engine must patch all
  three modules; patching `app.db.SessionLocal` alone doesn't reach the
  copies.

## Project Structure

```
historybites-backend/
  pyproject.toml
  alembic.ini
  railway.toml
  .env.example
  README.md
  DECISIONS.md
  app/
    __init__.py
    main.py            # FastAPI app, /today, /archive, /health, JSON logs, CORS
    config.py          # pydantic-settings
    db.py              # engine + SessionLocal + URL normalization
    models.py          # Fact, PoolFact ORM models
    schemas.py         # Pydantic request/response
    wikipedia.py       # categorymembers + extract client
    model_provider.py  # ModelProvider Protocol + Gemini + Ollama (D16)
    fcm.py             # Firebase Cloud Messaging dual-platform send (D17, D22)
    generation.py      # generate_one_pool_fact + schedule_tomorrows_fact
    cron.py            # run_generation, run_push, send_alert, _main CLI
    admin.py           # /admin/* router + bearer auth
    templates/
      review.html
  migrations/          # Alembic
  tests/
    conftest.py        # StaticPool SQLite + 6 fixtures + monkeypatch points
    unit/
    integration/
```

## See Also

- Notion workspace hub: [HistoryBites](https://www.notion.so/34a52c14aa5381b2a889e3569596cb18)
- Decisions log mirror: [`DECISIONS.md`](./DECISIONS.md)
- Architecture spec: Notion → Backend Architecture
- Build journal: Notion → Claude Code Log
