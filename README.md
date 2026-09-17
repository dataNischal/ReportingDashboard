# Report Dashboard

PostgreSQL-centric ETL + dashboard, spun out as its own project from the
original S3/CSV-based DATA MIGRATION pipeline. Reads two raw Postgres
tables (`IPAYDATA`, `iPayHub_Data`), cleans them via the same YAML-driven
rules as the original project, materializes a unified `business_report`
view (plus four pre-aggregated dashboard views on top of it), and serves
it to the dashboard HTML through a FastAPI middleware layer with its own
login system. Class-based throughout (sync OOP for the ETL side, async OOP
for the API side) — see `CLAUDE.md` for the full architecture writeup.

**Not setting this up, just want to use the dashboard, or explain it to
someone who will?** See `DASHBOARD_GUIDE.md` (plain-language, tab by tab)
instead of this file. For a diagram-first look at how data flows through
the whole system, see `WORKFLOW.md`.

## Layout

```
cleaning_config.yml          # cleaning/mapping/timezone/business-reporting rules
requirements.txt
.env.example                 # template for the env vars below -- copy to .env, never commit .env itself
.gitignore / .dockerignore
Dockerfile / docker-compose.yml / Caddyfile   # see "Docker" below
sql/
  001_auth_and_watermark.sql        # dashboard_credential (users+roles), sessions, ETL watermark, forecast cache
  002_clean_tables.sql              # ipaydata_clean / ipayhubdata_clean (cloned from raw tables)
  003_business_report_mv.sql        # the business_report materialized view
  004_dashboard_payload_mvs.sql     # pre-aggregated mv_dashboard_rows/_tat_rows/_creation_time_rows/_status_rows
Codes/
  logging_config.py          # configure_logging() -- shared by the ETL CLI and the API
  config.py                  # DatabaseSettings (sync-side env-var settings + engine construction)
  cleaning_functions.py      # CleaningConfig, DataCleaner, BusinessReportBuilder
  postgres_pipeline.py       # PostgresETLPipeline -- raw Postgres -> clean tables -> MV refreshes
  run_migrations.py          # applies sql/*.sql in order, idempotently
  analytics.py               # AnalyticsEngine -- dimension derivation (Year/Month/Corridors/
                              # Volume_Bracket/TAT_Bucket/Agent_Segment) -- same logic sql/003 mirrors in SQL
  file_io_utils.py           # AtomicFileWriter (local-only, no S3 in this project)
  interactive_dashboard_generator.py  # DashboardGenerator -- static single-file dashboard build
  interactive_dashboard_template.html  # the dashboard shell -- fetches live data when served by
                              # the API, or reads embedded data when built by the static generator
  api/
    main.py                  # FastAPI app: /login, /dashboard, lifespan-managed Database, mounts routers
    config.py                 # Settings -- async-side env-var settings (separate class from Codes/config.py)
    db.py                     # Database -- async SQLAlchemy engine/session (asyncpg) + pool warm-up
    security.py                # SecurityService -- argon2id hashing + session management
    deps.py                    # get_current_user / require_role / require_admin
    repositories.py            # DashboardFilters, DashboardReportRepository (queries the pre-aggregated views)
    create_user.py             # UserProvisioningService -- CLI to provision an account
    routers/
      auth.py                  # POST /api/login, /api/logout, GET /api/me
      dashboard.py              # GET /api/meta, /rows, /tat_rows, /creation_time_rows,
                                 #     /status_rows, /filter_options, /forecast_ml
    static/
      login.html                # standalone login page
```

## Prerequisites

- A PostgreSQL server with `IPAYDATA` (Inficare) and `iPayHub_Data`
  (iSendHub) already populated with raw transaction data.
- Confirm `Transaction_Code` (Inficare) and `Tracking_No` (iSendHub) are
  genuinely unique per source before running the migrations — they become
  `UNIQUE` constraints on the clean tables. (Note: `Control_No` is
  deliberately NOT used as Inficare's key — confirmed against real data
  that it can collide across genuinely distinct transactions.) The
  incremental watermark uses each source's own business-date column
  (`TRN_Date` / `DOT(Date_Of_TXN)`), so no extra ingestion-timestamp column
  needs to be added to the raw tables.
- Either Docker (see below — the easiest path to actually viewing the
  dashboard), or Python 3.10+ locally (this project uses `zoneinfo`, stdlib
  since 3.9).

## Docker (recommended way to run this)

The whole stack — the FastAPI dashboard behind a TLS-terminating Caddy
reverse proxy, plus one-off jobs for migrations/ETL/account creation — is
defined in `docker-compose.yml`. It connects to your **external** Postgres
server (RDS or otherwise); it does not run Postgres itself, since this
project is meant to point at a real, already-populated database.

1. `cp .env.example .env` and fill in your real `PG_*` values (or
   `DATABASE_URL`) plus `PG_SCHEMA` if needed. Never commit this file.
2. Optionally set `DOMAIN=your.real.domain` in `.env` if this host is
   reachable from the internet with DNS pointed at it — Caddy will get you
   a real Let's Encrypt certificate automatically. Leave it unset to fall
   back to Caddy's self-signed certificate against `localhost` (fine for
   viewing it yourself right away; browsers will flag it as untrusted
   until you set a real domain).
3. Build and start the always-on services:
   ```
   docker compose up -d --build
   ```
   This starts `api` (FastAPI, not published directly) and `caddy`
   (published on 80/443, proxying to `api`).
4. Apply migrations (safe to re-run — every migration is idempotent):
   ```
   docker compose run --rm migrate
   ```
5. Load data (first run: full reprocess; later runs: just `postgres_pipeline.py`
   with no flag, incremental via the watermark):
   ```
   docker compose run --rm etl python Codes/postgres_pipeline.py --full-refresh
   ```
6. Create an account (interactive password prompt — needs a real
   terminal, which `docker compose run` provides):
   ```
   docker compose run --rm create-user
   ```
   (edit the `--email`/`--role` in `docker-compose.yml`'s `create-user`
   service, or override inline: `docker compose run --rm create-user python
   Codes/api/create_user.py --email admin@yourcompany.com --role admin`)
7. Open `https://<DOMAIN-or-localhost>/` in a browser — redirects to
   `/login`, then `/dashboard` once authenticated.

**Refreshing later**: re-run step 5 (drop `--full-refresh` for the normal
incremental case) whenever new raw data lands — `postgres_pipeline.py`
refreshes `business_report` and all four dashboard views automatically at
the end of every run. Schedule step 5 externally (host cron / Task
Scheduler / your orchestrator's own scheduled-job feature calling `docker
compose run --rm etl ...`) at whatever cadence new raw data arrives — this
project doesn't run a scheduler itself, by design (see `docker-compose.yml`'s
comments).

**Deploying a CODE change (not a data refresh)**: the running `api`
container is an image built at a point in time — editing files under
`Codes/`, `sql/`, or `cleaning_config.yml` does NOT reach the live site on
its own. Rebuild and recreate it:

```bash
docker compose build api
docker compose up -d api
```

A `Caddyfile` change (e.g. its `encode` line) doesn't need a rebuild —
it's bind-mounted — but Caddy still needs telling to re-read it:
`docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`
(on Windows Git Bash specifically, prefix that with `MSYS_NO_PATHCONV=1`
or the `/etc/caddy/...` path gets silently mangled into a Windows path).
See `CLAUDE.md`'s "Deploying a code change (Docker)" section for more.

**Performance note**: even with the pre-aggregated views (see below), a
freshly-started `api` container spends real time during startup warming
its connection pool before it serves any traffic — anywhere from ~20s to
several minutes, depending entirely on how busy your Postgres instance
happens to be at that moment (confirmed directly: if it's a shared
instance with other applications/tools also connected, warm-up time and
per-request load times both climb with that external load, sometimes
substantially — this is Postgres-side contention, not something this
codebase controls). `docker compose logs api` showing a `Database
connection pool warmed up` line is your signal that it's actually ready to
serve; hitting the site before that during a restart gets a `502` from
Caddy, not a bug, just genuinely-not-ready-yet. **Avoid restarting the API
container unless you actually need to** — a restart trades a working (if
slow) dashboard for a guaranteed outage window of unpredictable length
while it re-warms. Once warm, a typical full dashboard load (fetching all
6 payload endpoints, always the full history — see `CLAUDE.md` for why
this project deliberately does NOT bound the default view to save load
time) runs anywhere from ~20s to a couple of minutes depending on that
same instance-contention factor and, if running under Docker Desktop on
Windows, its own virtualized-networking overhead on top (~2-4x slower
than bare-metal to the same database, confirmed directly). Responses are
gzip-compressed (a ~90%+ size reduction on the two largest endpoints),
which helps meaningfully but doesn't eliminate either factor above. See
`CLAUDE.md`'s "Two ways to view the dashboard" section for the full
performance investigation, numbers, and how to check current Postgres
contention directly if a load feels unusually slow.

## Setup (without Docker)

1. Create and activate a project-local virtualenv before installing
   anything — this project's pinned dependency versions can otherwise
   downgrade packages in a shared/global Python environment and break
   unrelated tooling there (this happened in practice: installing into a
   shared environment downgraded `starlette` below what an unrelated
   package needed):
   ```
   python -m venv .venv
   .venv\Scripts\activate      # Windows
   source .venv/bin/activate   # macOS/Linux
   ```
   Then: `pip install -r requirements.txt`
2. Set connection env vars: either `DATABASE_URL` (e.g.
   `postgresql+psycopg2://user:pass@host:5432/dbname`), or
   `PG_HOST`/`PG_USER`/`PG_PASSWORD`/`PG_DATABASE` (+ optional `PG_PORT`).
   If `IPAYDATA`/`iPayHub_Data` (and the tables this project creates) live
   in a non-`public` schema, also set `PG_SCHEMA` (e.g. `iPay_Backup_Data`)
   — every unqualified table reference in this codebase then resolves
   inside that schema via a per-connection `search_path`, with no SQL
   changes needed. Never put any of these in a file in this repo.
3. Run the SQL migrations **in order** against your database (or just
   `python Codes/run_migrations.py`, which applies all of them
   idempotently): `sql/001_auth_and_watermark.sql` → `sql/002_clean_tables.sql`
   → `sql/003_business_report_mv.sql` → `sql/004_dashboard_payload_mvs.sql`.
4. First data load (full reprocess): `python Codes/postgres_pipeline.py --full-refresh`
   Later runs (incremental, via the watermark): `python Codes/postgres_pipeline.py`
5. Create at least one account: `python Codes/api/create_user.py --email admin@yourcompany.com --role admin`
   (`--role` is one of `admin`/`stakeholder`/`business`/`accounts` — see
   `dashboard_credential` in `sql/001_auth_and_watermark.sql`; defaults to
   `stakeholder` if omitted.)
6. Run the API: `cd Codes/api && uvicorn main:app --host 0.0.0.0 --port 8000`
   (put a real reverse proxy / TLS in front in production — session
   cookies are `secure=True` by default and are refused by browsers over
   plain HTTP; set `SESSION_COOKIE_SECURE=false` only for local http-only
   testing with no reverse proxy — see `Codes/api/config.py`).
7. Open `http://<host>:8000/` — redirects to `/login`, then `/dashboard`
   once authenticated.

## Two ways to view the dashboard

Both read from the same `business_report`-derived data, just sourced two
different ways — the *same* `interactive_dashboard_template.html` detects
which one it's in at load time (see `CLAUDE.md` for the exact mechanism):

1. **Live API-driven** (`Codes/api/` + `uvicorn main:app`, or the Docker
   setup above) — the normal way to run this. FastAPI serves the
   unmodified dashboard shell, which `fetch()`es its data from
   `/api/meta`/`/api/rows`/`/api/tat_rows`/`/api/creation_time_rows`/
   `/api/status_rows`/`/api/forecast_ml` once at page load. Those
   endpoints query four pre-aggregated materialized views
   (`sql/004_dashboard_payload_mvs.sql`), not `business_report` directly —
   live-aggregating it per request was measured too slow to serve
   interactively (30-40+ seconds); see `CLAUDE.md` for the investigation.
2. **Batch-generated static HTML** (`python
   Codes/interactive_dashboard_generator.py`) — reads the entire
   `business_report` view, builds the same pre-aggregated payloads, and
   embeds them directly into a single self-contained
   `Dashboard/Transaction_Dashboard.html`. Needs no API server and no
   network access to view afterward, but the file is large (the original
   project's version of this ran to ~180MB). Useful for an offline export;
   not the primary way this project is meant to be used.

**ML forecasting is excluded from both modes for now** (per current scope) —
`interactive_dashboard_generator.py`'s `forecast_ml` payload is a stub with
empty Reference-tier data, and `GET /api/forecast_ml` returns
`not_yet_generated`. The Forecast tab's Live tier (Naive/Seasonal Naive/SES/
Holt-Damped — plain client-side JS, no Python ML) is unaffected either way;
only the precomputed Reference-tier rows will be empty.

## Refreshing data

`postgres_pipeline.py` refreshes `business_report` and all four dashboard
payload views automatically at the end of every run — schedule it
(cron/Task Scheduler, or `docker compose run --rm etl ...` from an external
scheduler) at whatever cadence new raw data arrives. A manual fallback
exists too: `POST /api/admin/refresh-view` (admin session required).

## Not included yet

- **Forecasting**: the original project's `predictive_model.py` (LightGBM/
  XGBoost/CatBoost/Prophet) isn't part of this project. `GET /api/forecast_ml`
  reads from a `forecast_cache` table that nothing currently populates —
  wiring `predictive_model.run_predictive_models()`'s output into that table
  (one `INSERT ... ON CONFLICT (id) DO UPDATE`) is the one remaining piece.
