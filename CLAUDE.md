# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

PostgreSQL-centric ETL + dashboard, spun out from an earlier S3/CSV-based
"DATA MIGRATION" project. It reads two raw Postgres tables (`IPAYDATA`,
`iPayHub_Data`), cleans them via YAML-driven rules, materializes a unified
`business_report` view, and serves it to an (unchanged) dashboard HTML
either as a static pre-baked file or through a FastAPI layer with its own
login system. There is no test suite in this repo.

Related docs, different audiences: `README.md` (setup/run commands),
`WORKFLOW.md` (the architecture below, as diagrams), `DASHBOARD_GUIDE.md`
(non-technical, for people who just use the dashboard).

Class-based throughout, not a script collection: the ETL/cleaning side
(`Codes/cleaning_functions.py`, `Codes/analytics.py`,
`Codes/postgres_pipeline.py`, `Codes/file_io_utils.py`,
`Codes/interactive_dashboard_generator.py`) is synchronous OOP; the FastAPI
side (`Codes/api/`) is fully async OOP (async SQLAlchemy + `asyncpg`, not
`psycopg2`). `logging` (not `print`) everywhere, `.flake8` enforces PEP8
(max-line-length 100) — see "Coding standards" below for the reasoning and
the file-by-file class map.

## Commands

Setup — use a project-local `.venv`, not a shared/global Python
environment. Confirmed the hard way: installing this project's pinned
`requirements.txt` into a shared environment downgraded `starlette`/
`fastapi`/`SQLAlchemy` below what unrelated tooling in that same
environment needed.
```
python -m venv .venv
.venv\Scripts\activate      # Windows; source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

Connection info is **never** stored in the repo — set one of:
- `DATABASE_URL` (e.g. `postgresql+psycopg2://user:pass@host:5432/dbname`), or
- `PG_HOST` / `PG_USER` / `PG_PASSWORD` / `PG_DATABASE` (+ optional `PG_PORT`)

Optionally also set `PG_SCHEMA` (e.g. `iPay_Backup_Data`) if the raw/clean
tables and `business_report` don't live in the `public` schema — every
engine-creation path in this codebase (`Codes/api/db.py`'s `Database`,
`Codes/postgres_pipeline.py`'s `PostgresETLPipeline._create_engine`,
`Codes/api/create_user.py`) applies it as a per-connection `search_path`,
so none of the SQL text needs schema qualification. Two details that broke
in practice and are worth knowing before touching this: (1) it MUST be a
real `SET search_path TO "..."` statement, never a `-c search_path=...`
libconnect/DSN option — that form is parsed as an unquoted identifier list
and silently lowercases a mixed-case schema name; (2) on the sync
(psycopg2) side specifically, that `SET` must be followed by an explicit
`commit()` — psycopg2 defaults to `autocommit=False`, so an uncommitted
`SET` sits inside an open transaction that a later read-only rollback
(e.g. `pandas.read_sql`'s connection close) silently reverts. asyncpg
avoids this entirely by passing the schema via `server_settings` in the
connection's startup packet instead of a post-connect `SET`.

Run SQL migrations, in order, against the target database (or just
`python Codes/run_migrations.py`, which applies every `sql/*.sql` file in
filename order, idempotently — every migration here is written as
`CREATE ... IF NOT EXISTS`, so re-running it against an already-migrated
database is a safe no-op):
```
sql/001_auth_and_watermark.sql
sql/002_clean_tables.sql
sql/003_business_report_mv.sql
sql/004_dashboard_payload_mvs.sql
```
(`002`'s natural-key constraint is on **`Transaction_Code`** for Inficare,
not `Control_No` — confirmed against real data that `Control_No` has
~264 groups of genuinely distinct transactions colliding on one value
(a float/scientific-notation precision-loss artifact upstream), while
`Transaction_Code` is 100% unique. `Tracking_No` for iSendHub is already
100% unique. Re-verify this if pointed at a different raw dataset — don't
assume `Control_No` is safe to key on without checking. Also: the
incremental watermark uses each source's own business-date column
(`TRN_Date` for Inficare, `DOT(Date_Of_TXN)` for iSendHub —
`PostgresETLPipeline.WATERMARK_COLUMN`) rather than an `_ingested_at`
column, since neither raw table has one; see that class's docstring for
the trade-off this accepts.)

ETL (raw → clean tables → refresh `business_report` MV):
```
python Codes/postgres_pipeline.py                # incremental, via etl_watermark
python Codes/postgres_pipeline.py --full-refresh # reprocess every raw row
```

Provision a stakeholder account (no self-service signup exists):
```
python Codes/api/create_user.py --email admin@yourcompany.com --name "Jane Doe" --role admin
```
(`--role` is one of `admin`/`stakeholder`/`business`/`accounts`, default `stakeholder`.)

Run the live API:
```
cd Codes/api && uvicorn main:app --host 0.0.0.0 --port 8000
```
Session cookies are `secure=True`, so a browser will refuse them over
plain HTTP — put a real reverse proxy / TLS in front in production, or
temporarily flip `secure` in `Codes/api/routers/auth.py` for local http dev.

Generate the static, self-contained dashboard instead (no API server needed):
```
python Codes/interactive_dashboard_generator.py
```
Writes `Dashboard/Transaction_Dashboard.html` from the current
`business_report` contents.

Manually refresh the materialized view without re-running the whole ETL:
`POST /api/admin/refresh-view` (requires an admin session) — the pipeline
already does this automatically at the end of every run.

## Architecture

### Two sources, one shape, everywhere

Everything downstream — cleaning, the SQL materialized view, the API, the
static generator — deals with two raw sources (`Inficare` /
`IPAYDATA`→`ipaydata_clean`, and `iSendHub` / `iPayHub_Data`→
`ipayhubdata_clean`) that get unioned into one common schema. That mapping
(which raw column becomes which unified column, e.g. Inficare's
`Control_No` vs iSendHub's `Tracking_No` → both become `Control_No`) is
defined once, in `cleaning_config.yml`'s `business_reporting` section, and
is **hand-duplicated** into `sql/003_business_report_mv.sql`'s two CTEs
(`inficare`, `isendhub`). If you change one, you must change the other —
the SQL comment block at the top of that file explains why this
duplication is accepted rather than eliminated (the SQL MV is what
actually runs; the YAML/pandas version remains the documented spec and
still runs when the legacy CSV/S3 pipeline path is used elsewhere).

The same duplication trap applies to `Codes/analytics.py`'s
`VOLUME_BRACKET_BINS/LABELS`, `TAT_BUCKET_LABELS`, and
`AGENT_SEGMENT_MAP` — these are mirrored as `CASE` expressions inside the
same SQL file. Changing a bucket boundary or a segment's member list means
editing both places.

### `cleaning_config.yml` is the single source of truth for cleaning

`Codes/cleaning_functions.py`'s `DataCleaner` (constructed from a
`CleaningConfig`, which loads + eagerly validates the YAML) is entirely
config-driven: `.clean()` walks `tables.<source>.transformations`
(timestamp parsing via `transform_ref`, value remapping via `mapping_ref`,
country-name normalization via `normalize_text`, an `override_from` escape
hatch) and `tables.<source>.defaults` for null-filling, all keyed off this
file — no column names or mapping tables are hardcoded in the Python.
Adding a new raw column mapping/default means editing the YAML, not the
class.

`.normalize_timezones()` is a separate, later pass driven by
`timezone_normalization` in the same file: every date column ends up
converted to one `target_timezone` (currently `Asia/Kuala_Lumpur`) except
columns explicitly marked `mode: skip`. Per-column mode is one of `fixed`
(whole column shares one source timezone), `by_country` (each row's
source timezone comes from a sibling country column, resolved through
`country_timezones`), or `skip`. **`iSendHub.DOT(Date_Of_TXN)` is
deliberately NOT listed under `timezone_normalization.tables` at all** —
see the next section for why; `Paid_Date` (both sources) is still
converted here exactly as before.

### `TRN_Date` vs `TRN_Date_Normalized` — reporting to match Accounts' MIS

`business_report` carries the transaction date TWICE, for two different
jobs, after a real reporting-accuracy bug was found and fixed in practice
(confirmed against a real transaction: raw Nepal time `2026-07-31 23:53`
was being shown as `2026-08-01 02:08` after conversion, pushing it into
the wrong day/month):

- **`TRN_Date`** — each source's transaction date exactly as recorded, no
  conversion at all. Inficare: unchanged (always was raw). iSendHub: raw
  `DOT(Date_Of_TXN)` (Asia/Kathmandu/Nepal wall-clock time) — this
  USED TO be converted to `Asia/Kuala_Lumpur` right here at ETL clean-time
  (permanently overwriting `ipayhubdata_clean`'s value), which is exactly
  what caused the bug: Nepal is UTC+5:45, Malaysia is UTC+8, so a +2h15m
  shift pushes late-day-of-month transactions into the next calendar
  day/month, disagreeing with the Accounts department's own MIS (which
  reports on the original Nepal-recorded date, and also uses this
  dashboard). `TRN_Date` is what every Volume/Transaction/Geography/
  Partners/Corridors/etc. Year/Month/Day/Hour dimension is built from —
  in both `sql/003_business_report_mv.sql` (the live path) and
  `Codes/analytics.py`'s `prepare_analytics_data()` (the static path).
- **`TRN_Date_Normalized`** — the OLD behavior, computed instead in
  `sql/003_business_report_mv.sql` (`... AT TIME ZONE 'Asia/Kathmandu' AT
  TIME ZONE 'Asia/Kuala_Lumpur'` for iSendHub; identical to `TRN_Date` for
  Inficare, which is already Asia/Kuala_Lumpur per this project's existing
  documented assumption). Used **only** for the duration calculation itself
  — `Turn_Around_Time_Hours` (`Paid_Date - TRN_Date_Normalized`, not `-
  TRN_Date`) and the `TAT_Bucket` CASE derived from it — since `Paid_Date`
  is already timezone-normalized for both sources (unchanged by this fix),
  and mixing it with a raw-clock `TRN_Date` would silently skew every
  iSendHub TAT value by Nepal-Malaysia's ~2h15m offset. **Deliberately NOT
  used for TAT's own Year/Month/Year-Month bucketing** —
  `mv_dashboard_tat_rows` and `interactive_dashboard_generator.py`'s
  `TAT_DIMENSION_COLUMNS` group by business_report's regular (raw-clock)
  `Year`/`Month`/`Year-Month`, same as every other tab, so a transaction
  lands in the same Year-Month in TAT Analysis as it does everywhere else
  in the dashboard — only the duration *value* itself is computed on the
  normalized clock, not which month it gets counted in.

### ETL pipeline (`Codes/postgres_pipeline.py`'s `PostgresETLPipeline`)

Incremental by default, using a per-source watermark row in
`etl_watermark` (the column checked is `WATERMARK_COLUMN` — see the
"SQL migrations" note above on why it's a business-date column, not
`_ingested_at`). Flow per source (`.run_source()`): read only rows newer
than the watermark → `DataCleaner.clean()` → `DataCleaner.normalize_timezones()`
→ `.upsert_clean_table()` (`INSERT ... ON CONFLICT (natural_key) DO
UPDATE`, batched via `psycopg2.extras.execute_values` — a naive one-row-
per-round-trip upsert took an estimated ~70 hours over 963K rows at this
project's real RDS latency, confirmed directly; batching page_size=1000
brought a full load down to minutes) → advance the watermark. After both
sources run, `.refresh_business_report()` does
`REFRESH MATERIALIZED VIEW CONCURRENTLY business_report` (requires the
unique index `idx_business_report_pk`; runs in its own transaction since
`CONCURRENTLY` can't be combined with other statements). `.run()` wraps
the whole sequence in a top-level try/except that logs and re-raises.

`BusinessReportBuilder` in `cleaning_functions.py` (YAML+pandas version of
the union/formula logic) is **not** called by this pipeline — that logic
now lives in the SQL MV instead (see above). It's kept for the legacy
CSV/S3 pipeline path and as the documented spec for the SQL.

Deliberately stays synchronous (`psycopg2`, not `asyncpg`) — see the
"Coding standards" section below for why.

### Two ways to view the dashboard, same data underneath

Both inject data into the same, unmodified-in-structure
`interactive_dashboard_template.html` (same `dashboard-data-<key>`
`<script>` contract as the original project) — but they get that data two
different ways, and the template itself detects which one it's in:

1. **Static, batch-generated** (`Codes/interactive_dashboard_generator.py`'s
   `DashboardGenerator`): loads the *entire* `business_report` view into
   pandas, runs `AnalyticsEngine.prepare_analytics_data()` to derive
   dimensions, builds the `rows` / `tat_rows` / `creation_time_rows` /
   `status_rows` payloads, and embeds them as inline JSON
   (`DASHBOARD_DATA_BLOCKS_PLACEHOLDER` → one `<script id="dashboard-data-
   <key>">` per section) into one self-contained HTML file. No API server
   needed, but the file is large (the original project's version of this
   ran to ~180MB embedded).
2. **Live, API-driven** (`Codes/api/` + `uvicorn main:app`): serves the
   *unmodified* template (no injection at all) at `GET /dashboard`. The
   template's bootstrap script (top of the big inline `<script>` in
   `interactive_dashboard_template.html`, wrapped in an `async function()`
   IIFE) checks whether `#dashboard-data-meta` exists in the DOM; if not
   (this path), it `fetch()`es `/api/meta` + `/api/rows` + `/api/tat_rows`
   + `/api/creation_time_rows` + `/api/status_rows` + `/api/forecast_ml`
   once, in parallel, and seeds `payloadCache` from the results — so every
   one of the ~40 chart-rendering functions and their `getPayloadSection()`
   calls work completely unchanged regardless of which path supplied the
   data. A handful of functions referenced by inline `onclick`/`onchange`
   HTML attributes (`switchTab`, `resetFilters`, etc. — currently 19) are
   explicitly re-exposed on `window` inside the IIFE, since function
   declarations inside it aren't globally reachable otherwise.

`Codes/api/repositories.py`'s `DashboardReportRepository` (used by
`routers/dashboard.py`'s endpoints: `/rows`, `/tat_rows`,
`/creation_time_rows`, `/status_rows`, `/filter_options`) queries four
**pre-aggregated materialized views** (`sql/004_dashboard_payload_mvs.sql`:
`mv_dashboard_rows`/`_tat_rows`/`_creation_time_rows`/`_status_rows`), NOT
`business_report` directly — confirmed directly against the live RDS
instance that live-aggregating business_report per request (the original
design) is far too slow to serve interactively (the unfiltered `rows`
query alone took 30-40+ seconds; `EXPLAIN ANALYZE` showed a forced full
parallel sequential scan plus a disk-spilling sort for even a 2-column
GROUP BY, and bumping `work_mem` only partially helped). These MVs mirror
`interactive_dashboard_generator.py`'s `DIMENSION_COLUMNS`/
`TAT_DIMENSION_COLUMNS`/etc. exactly, including which filters are/aren't
meaningful against which shape (`APPLICABLE_ROWS_FILTERS` etc. in
`repositories.py` — e.g. `Corridors` was never a TAT-tab dimension, so a
Corridors filter has always been a no-op there; pre-aggregating didn't
change that contract, just moved the aggregation from "every request" to
"once per ETL run"). Casing normalization (`upper(trim(...))` on
`Payment_Type`/`Agent_Name`/`Transaction_Method`/`transstatus`/currency
columns — confirmed against real data these have inconsistent raw casing,
e.g. `Payment_Type` collapses from 5 raw values to 4 once normalized) is
baked into the views themselves, not recomputed per request.

**Even pre-aggregated, a fresh/cold pooled connection's FIRST query
against one of these views is measurably slower than a later one on the
same connection** (confirmed: one run went 22s → 16s → 9s → 7s across 4
sequential calls on a freshly-created engine) — some combination of
Postgres-side query planning and connection/TLS setup cost on this RDS
instance that pre-aggregation doesn't eliminate, only amortizes.
`Codes/api/db.py`'s `Database.warm_up()` runs each of the 4 payload
queries twice, concurrently, in `main.py`'s lifespan startup (adding
~20-25s to app startup, before it accepts any traffic) specifically so
the first real user's dashboard load doesn't pay that cost cold. Real
measured end-to-end load time even after warm-up: ~7-15s per endpoint
once a connection is warm, briefly longer (~15-37s to fully render) for
requests that land on a still-cold pooled connection — a real, substantial
improvement over the original 30-40+s-every-time design, but not
instant; if this needs to get faster still, the next lever is either a
smaller/fully-pre-warmed connection pool, a persistent external pooler
(pgbouncer), or bounding the default (unfiltered) view to a smaller time
window client-side.

**Dockerized, these numbers get measurably worse** — confirmed directly:
a raw `SELECT 1` round trip that costs ~280ms from the host costs ~865ms
from inside `report-dashboard-api-1` (~3x), and a full concurrent
dashboard load (all 6 endpoints, matching the frontend's `Promise.all()`)
that took ~20-30s from the host took ~42s from a freshly-warmed container
in the same test — Docker Desktop's virtualized network path (WSL2 NAT on
Windows) adds real overhead, not just a fixed latency constant, for bulk
transfers specifically (the two biggest endpoints, `/api/rows` and
`/api/creation_time_rows`, are hit hardest; trivial ones like `/api/meta`
barely move). **This compounds badly under repeated page reloads**: each
dashboard load fires all 6 endpoints concurrently, `uvicorn` runs as a
single worker/single event loop (no `--workers N` in the `Dockerfile`
`CMD`), and a request a browser has already abandoned (the tab was
reloaded) does NOT necessarily stop executing server-side — so several
reloads in a row, each waiting ~40s+ and then reloading again out of
impatience, pile up concurrent large-payload work faster than it drains,
which is what a "stuck on Loading dashboard data for minutes" report
usually is in practice, confirmed by finding several old `idle` sessions
and no actual DB-side blocking locks. `docker compose restart api` clears
a pile-up immediately (confirmed: reduced a multi-minute stall back down
to one clean ~42s load) — try that first before assuming new code broke
something. Isolated, single-request timing rules out the response
pipeline itself as the culprit: `TypeAdapter(List[RowsRow]).
validate_python()` (~0.3s) + `jsonable_encoder()` (~2.4s) + `json.dumps()`
(~0.3s) for `mv_dashboard_rows`'s full ~99K rows is negligible next to the
~30-55s DB fetch itself — the bottleneck is the container-to-RDS network
path, not FastAPI/Pydantic.

**A default-recent-window load (last 12 months, "Load Full History" on
demand) was tried here and deliberately REVERTED** — every live-fetch load
always fetches full, unbounded history again
(`interactive_dashboard_template.html`'s `loadDashboardData()` takes no
date-window argument). Not because it was broken — a real headless-browser
test confirmed it correctly loaded true full history (verified against
`business_report`'s actual range, January 2025 - August 2026) once given
time to finish — but because this is a financial-reporting tool that has
to match Accounts' own MIS exactly, and a design that silently hides older
data behind a button is a real risk if anyone forgets to click it, or
gives up waiting during a slow patch (see the RDS-contention note below)
and assumes they're looking at complete numbers when they aren't. Load
time was judged not worth that risk for this tool. If bounding the default
view is revisited later: the backend already supports `date_start`/
`date_end` query params end-to-end (`repositories.py`'s
`DashboardFilters.build_where()`, wired into all 4 payload endpoints) —
re-adding it to the frontend is mechanical, but the previous
implementation's own git history / this file's prior revision has the
worked-through approach (`rawData` needs to be `let` not `const` so a
full-history reload reaches every function that closed over it; refresh
`populateDropdowns()`/`populateGlobalDateFilters()` after any reload, or
filter option lists like Agent_Name/Year stay stale to whatever was
loaded first).

**This surfaced a real, previously-dormant bug that's still fixed** (kept
even though the feature that exposed it was reverted, since it's a
genuine correctness issue for ANY future `date_start`/`date_end` caller):
`mv_dashboard_tat_rows` and `mv_dashboard_status_rows` carry no `"Day"`
column at all (see `sql/004_dashboard_payload_mvs.sql`) — `build_where()`'s
old date-range SQL referenced `COALESCE("Day", 1)` unconditionally, which
errors against a view where `"Day"` doesn't exist (`COALESCE` can
substitute a NULL, not a nonexistent column). Fixed by reusing the
existing `"day" in applicable_keys` signal
(`APPLICABLE_TAT_ROWS_FILTERS`/`APPLICABLE_STATUS_ROWS_FILTERS` already
omit `"day"`) to pick between `_date_key()` (day-precision) and
`_year_month_key()` (month-precision, for the two views without `"Day"`)
— verify any new caller of `build_where()` with a date range against a
view lacking `"Day"` still goes through this branch rather than assuming
every view has one.

**Dockerized container performance has been observed to degrade over the
course of a session** (warm-up time climbing 28s → 75s → 250.9s across
successive restarts, unrelated to any code change) — confirmed the cause:
`isendstaging` is a genuinely **shared** RDS instance, not dedicated to
this project. `pg_stat_activity` at the worst point showed 69 total
connections, including **46 from a completely unrelated application**
(`usename = isend_db_user`, no `application_name` set) plus live
`pgAdmin 4`/`DBeaver` sessions — real external load this app has no
control over. `pg_locks` showed no actual blocking (nothing was
deadlocked), just many concurrently-active queries genuinely fighting for
the same RDS instance's CPU/IO — `wait_event = ClientWrite` on the app's
own bulk queries during the worst episode meant Postgres had results
ready and was trying to push them over the wire, backed up behind that
contention.

**This directly explains "This page isn't working" after a restart**:
`main.py`'s `lifespan()` blocks the whole startup (including `warm_up()`)
before uvicorn starts accepting connections at all, so during that entire
window Caddy gets `dial tcp ...: connect: connection refused` and returns
502 to the browser (confirmed directly in `docker compose logs caddy`) --
not a bug, just means a restart's "site is briefly down" window is exactly
as long as `warm_up()` takes, which is no longer a safe-to-assume
30-60s -- it can now run to several minutes if the shared instance happens
to be under heavy external load at that moment. **Avoid restarting the API
container unless actually necessary** for exactly this reason -- a restart
while the site is already up trades a working (if slow) dashboard for a
guaranteed outage window of unpredictable length. If the dashboard is slow
without restarting anything, that's very likely this same external
contention, not something an app-side restart fixes. To check contention
directly: `SELECT usename, count(*) FROM pg_stat_activity GROUP BY
usename` -- a large, unfamiliar `usename`/connection count is the signal,
same as the `isend_db_user` case here. If a restart is genuinely needed,
check `docker compose logs api` for the most recent `Database connection
pool warmed up` duration as a rough live signal of how contended the
instance currently is before deciding whether now is a good time.

**Responses are gzip-compressed** (`GZipMiddleware` in `main.py`, plus
`encode gzip zstd` in the `Caddyfile` for the reverse-proxy hop) —
confirmed directly that `/api/rows`/`/api/creation_time_rows`'s 47-60MB
JSON responses (highly repetitive: the same column names across tens of
thousands of near-identical objects) compress to ~3-4MB, a real win on a
link where round-trip latency is already high. `compresslevel=6`
(zlib/nginx's own default), not Starlette's default of `9` — confirmed `9`
costs ~7x the CPU time for ~6% smaller output on a payload this size
(2.3s vs 0.3s to compress ~40MB), and `GZipMiddleware` compresses
synchronously in the request coroutine, not in a thread pool, so that time
blocks the single event loop for every OTHER concurrent request too. Using
`9` here measurably made concurrent dashboard loads *slower*, not faster,
than shipping uncompressed — confirmed by direct A/B timing before landing
on `6`. Don't bump this back up without re-timing concurrent load, not
just checking output size.

Each router endpoint declares a `response_model` (`routers/dashboard.py`'s
`RowsRow`/`TatRow`/etc., using `Field(alias=...)` for space/hyphen column
names like `"Year-Month"`) so the OpenAPI schema documents real response
shapes — verify any new/changed column against the model's field list, or
FastAPI will silently drop it from the response.

**Refreshing**: `Codes/postgres_pipeline.py`'s `PostgresETLPipeline.
refresh_business_report()` refreshes `business_report` AND all 4
`mv_dashboard_*` views, in that order (each depends on business_report
already being current) — automatically at the end of every ETL run, or
on demand via `POST /api/admin/refresh-view`
(`DashboardReportRepository.refresh_materialized_view()`, same order).
Each `REFRESH MATERIALIZED VIEW CONCURRENTLY` needs its own committed
transaction (Postgres rejects `CONCURRENTLY` as a non-first statement in
an open transaction block) — the async side does this via an explicit
`session.commit()` after each one; the sync side gets it for free from a
separate `engine.begin()` context per view.

**ML forecasting is out of scope for now** in both modes: the static
generator embeds a `forecast_ml` stub (`_FORECAST_ML_STUB`) with empty
Reference-tier data, and `GET /api/forecast_ml` returns
`{"forecast_status": "not_yet_generated"}` when `forecast_cache` (a
single-row cache table meant to be populated by a `predictive_model.py`
this project doesn't yet have) is empty. The dashboard's client-side-only
Forecast "Live" tier (Naive/Seasonal Naive/SES/Holt-Damped) is unaffected
by any of this Reference-tier gap on its own.

**The Forecast tab itself is currently disabled in the dashboard UI**,
though — a deliberate, explicit choice (not a bug, and not the same thing
as the Reference-tier gap above): the tab button in
`interactive_dashboard_template.html`'s `.tab-bar` was removed (replaced
with an HTML comment explaining why and how to reverse it), and
`#tab-forecast`'s panel `div` has a `hidden` attribute added defensively
on top of that. Nothing else was deleted — the panel's full HTML, its
`TAB_RENDERERS['tab-forecast']` entry, `renderForecastTab()`, and every
other Forecast-tab function are all still intact, just unreachable
because nothing in the file can `switchTab()` to it without a button
wired to call that. This means the working client-side "Live" tier is
ALSO currently hidden from users, not just the empty Reference tier —
re-enabling is a matter of restoring the removed button (and dropping
`hidden` from the panel), not rebuilding anything.

### Auth (`Codes/api/`)

Server-side opaque session tokens in Postgres (`app_sessions`), not JWTs —
by design, so a session is instantly revocable and its expiry **slides
forward** on every authenticated request (`SecurityService.
validate_and_refresh_session`) up to a hard `absolute_max` cap, rather
than dying on a fixed timer (the explicit fix for the old project's S3
presigned-URL expiry problem). Passwords are argon2id (`argon2-cffi`),
hashed/verified via `asyncio.to_thread` since Argon2 is deliberately
CPU-expensive and would otherwise stall the event loop for every other
concurrent request. No self-service signup — accounts are provisioned
only via `create_user.py`'s `UserProvisioningService`. `GET /dashboard`
redirects unauthenticated browsers to `/login`; API data endpoints instead
return 401 JSON via the `get_current_user` dependency, which the frontend
is expected to treat as "go to /login".

**`validate_and_refresh_session` is a single statement, run over an
AUTOCOMMIT connection, deliberately.** This runs on EVERY authenticated
request — confirmed directly against the live RDS instance that each
network round trip here costs a fixed ~270-300ms (this app's DB host is
geographically distant from where it's served), so the original
SELECT-then-UPDATE-under-an-explicit-transaction version cost even a
trivial call like `GET /api/me` five serial round trips (pool
pre-ping + BEGIN + SELECT + UPDATE + COMMIT, ~1.9-2.9s observed) where two
suffice. The fix has three parts, all in `security.py`/`db.py`/`deps.py`:
(1) the SELECT and UPDATE are merged into one `WITH ... UPDATE ...
RETURNING ... SELECT` CTE (the idle_timeout/absolute_max sliding-expiry
cap is computed in SQL via `LEAST(...)`/`make_interval(...)`, not read
back into Python first — watch for stray `::casts` glued directly onto a
SQLAlchemy `text()` bind parameter name, e.g. `:now::timestamptz`; that
specific pattern silently breaks `text()`'s parameter substitution); (2)
`Database.autocommit_engine` (`db.py`) is the same engine/pool as
`Database.engine` (`Engine.execution_options()` returns a shallow copy
sharing the connection pool, not a second pool) with
`isolation_level="AUTOCOMMIT"`, so there's no explicit BEGIN/COMMIT round
trip around a statement that's already atomic on its own; `deps.py`'s
`get_current_user` uses it instead of `db.session()` for exactly this
reason. (3) `Database.warm_up_auth_check()`, called from `main.py`'s
lifespan alongside the existing `warm_up()`, separately pre-warms this
specific query — confirmed asyncpg caches a prepared statement PER
PHYSICAL CONNECTION, not once for the whole pool, and `warm_up()` never
touches this query (only the 4 dashboard-view SELECTs), so without this a
real user's first several authenticated requests after startup would each
land on a not-yet-primed pooled connection and pay an extra round trip
until enough of the pool had cycled through it naturally. Net effect,
confirmed end to end: every authenticated request's floor dropped from
~1.9-2.9s to a consistent ~1.1s. This floor is still real network-latency
distance, not something further query optimization alone can remove.

Users live in `dashboard_credential` (email/password_hash/name/`role`),
not a boolean admin flag — `role` is one of `admin`, `stakeholder`,
`business`, `accounts` (CHECK-constrained in
`sql/001_auth_and_watermark.sql`). `Codes/api/deps.py`'s `require_role(*roles)`
is a dependency factory for gating an endpoint to specific roles;
`require_admin` is just `require_role("admin")` kept as a ready instance
since `routers/dashboard.py`'s `/api/admin/refresh-view` already depends
on it. **Postgres's real ROW LEVEL SECURITY feature cannot attach to
`business_report`** (RLS policies only apply to ordinary tables, not
materialized views) — so per-role row filtering, if/when a business rule
for it exists, has to be enforced as an extra SQL `WHERE` predicate inside
`DashboardFilters.build_where()` (repositories.py), the same way every
other filter already works there, not as database-level RLS.

The dashboard's header (`interactive_dashboard_template.html`) has a
**Log Out button** next to Reset Filters — calls `POST /api/logout`
(revoking that session server-side via `revoke_session`, not just
discarding the cookie client-side) then redirects to `/login` regardless
of whether the revoke call succeeded, so a backend hiccup never traps the
user on the dashboard. The `logout()` JS function and its `window.logout`
export live right next to `fetchJSON`'s existing 401→`/login` redirect,
since both are the same "session ended, go to /login" concern.

### Deploying a code change (Docker)

**The running `report-dashboard-api-1` container is an image built at a
point in time (`Dockerfile`'s `COPY Codes ./Codes`), not a live view of
this checkout** — confirmed the hard way: after an earlier round of
changes here (login page redesign, gzip, the auth round-trip fix, even
the TRN_Date fix), `docker compose ps` still showed the API container
`Up ... 9 days` on the original image, so none of those changes were
reachable at `localhost/` despite being correct on disk. After ANY change
under `Codes/`, `sql/`, or `cleaning_config.yml` that should reach the
live site:

```
docker compose build api
docker compose up -d api        # recreates the container from the new image
```

A `Caddyfile` change (e.g. the `encode gzip zstd` line) doesn't need an
image rebuild — it's bind-mounted (`docker-compose.yml`'s
`./Caddyfile:/etc/caddy/Caddyfile:ro`) — but Caddy still needs telling to
re-read it: `docker compose exec caddy caddy reload --config
/etc/caddy/Caddyfile` (from Git Bash on Windows specifically, prefix with
`MSYS_NO_PATHCONV=1` or that `/etc/caddy/...` path gets silently mangled
into a Windows path before it ever reaches the container).

### Coding standards (`Codes/`)

This codebase deliberately follows: modular files/classes, config-driven
behavior (env vars + `cleaning_config.yml`, never hardcoded), OOP
throughout (no bare top-level business-logic functions — see the class map
below), PEP 8 enforced by `.flake8` (`max-line-length = 100`; run `flake8
--max-line-length=100 Codes/` before committing), `logging` (never
`print`) via `Codes/logging_config.py`'s `configure_logging()`, ACID
transactions around every DB write (see each class's docstrings for the
transaction boundary), try/except with `logger.exception(...)` around I/O
that can fail, and OpenAPI-documented FastAPI responses (every route
declares a Pydantic `response_model`).

**Async is API-only, deliberately.** `Codes/api/` is fully async
(SQLAlchemy's async engine + `asyncpg`, `async def` throughout) because it
serves concurrent HTTP requests — real payoff from not blocking the event
loop. `Codes/postgres_pipeline.py` stays synchronous (`psycopg2`) because
it's a single sequential batch job with nothing else running concurrently
— there's no blocked event loop to avoid, so async there would only add
complexity. Don't "async-ify" the ETL pipeline without a concrete
concurrent-workload reason to.

Class map, since imports don't always make this obvious:
- `Codes/cleaning_functions.py`: `CleaningConfig` (loads/validates
  `cleaning_config.yml`), `DataCleaner` (`.clean()`/`.normalize_timezones()`),
  `BusinessReportBuilder` (legacy CSV-path union/formula logic)
- `Codes/analytics.py`: `AnalyticsEngine` (`.prepare_analytics_data()` +
  every `create_*_analysis` method)
- `Codes/postgres_pipeline.py`: `PostgresETLPipeline` (the whole ETL
  orchestration — see above)
- `Codes/file_io_utils.py`: `AtomicFileWriter`
- `Codes/interactive_dashboard_generator.py`: `DashboardGenerator`
- `Codes/config.py`: `DatabaseSettings` (sync-side env-var settings)
- `Codes/api/config.py`: `Settings` (async-side env-var settings — a
  **separate class in a same-named but different file**; `Codes/api/`
  must be first on `sys.path` or `import config` resolves to the wrong
  one — see `main.py`/`create_user.py`'s path-setup comments)
- `Codes/api/db.py`: `Database` (async engine/session factory, held on
  `app.state.db`, created once in `main.py`'s lifespan)
- `Codes/api/security.py`: `SecurityService`
- `Codes/api/repositories.py`: `DashboardFilters`, `DashboardReportRepository`
- `Codes/api/create_user.py`: `UserProvisioningService`

`Codes/run_migrations.py` is the one deliberate exception to "class-based
throughout" above — it's a thin, sequential file-I/O + SQL-execution
script (read `sql/*.sql` in order, `execute()` each), not business logic,
so a class would just add ceremony around a single `main()` with nothing
else to encapsulate.

### File layout

```
cleaning_config.yml                     # cleaning/mapping/timezone/business-reporting rules (source of truth)
.flake8                                 # PEP8 config (max-line-length=100)
sql/001_auth_and_watermark.sql          # dashboard_credential (users+roles), sessions, etl_watermark, forecast_cache
sql/002_clean_tables.sql                # ipaydata_clean / ipayhubdata_clean, cloned from raw tables
sql/003_business_report_mv.sql          # business_report materialized view (hand-mirrors the YAML + analytics.py)
sql/004_dashboard_payload_mvs.sql       # pre-aggregated mv_dashboard_rows/_tat_rows/_creation_time_rows/_status_rows
Codes/logging_config.py                 # configure_logging() -- shared by the ETL CLI and the API
Codes/config.py                         # DatabaseSettings -- sync-side (ETL) env-var settings
Codes/cleaning_functions.py             # CleaningConfig, DataCleaner, BusinessReportBuilder
Codes/postgres_pipeline.py              # PostgresETLPipeline -- the ETL entrypoint
Codes/run_migrations.py                 # applies sql/*.sql in filename order, idempotently
Codes/analytics.py                      # AnalyticsEngine -- dimension derivation + create_*_analysis
Codes/file_io_utils.py                  # AtomicFileWriter
Codes/interactive_dashboard_generator.py# DashboardGenerator -- static single-file dashboard build
Codes/interactive_dashboard_template.html # dashboard shell, copied unchanged from the original project
Codes/api/main.py                       # FastAPI app: /login, /dashboard, lifespan-managed Database, mounts routers
Codes/api/config.py                     # Settings -- async-side (API) env-var settings (see class map above)
Codes/api/db.py                         # Database -- async SQLAlchemy engine/session (asyncpg)
Codes/api/security.py                   # SecurityService -- argon2id hashing + session management
Codes/api/deps.py                       # get_current_user / require_role / require_admin (async)
Codes/api/repositories.py               # DashboardFilters, DashboardReportRepository
Codes/api/create_user.py                # UserProvisioningService -- CLI to provision an account
Codes/api/routers/auth.py               # POST /api/login, /api/logout, GET /api/me
Codes/api/routers/dashboard.py          # GET /api/rows, /tat_rows, /creation_time_rows, /status_rows, /filter_options, /forecast_ml
Codes/api/static/login.html             # standalone login page -- matches the dashboard's own
                                         # "cloud" theme/iSend branding, not a generic form
```
