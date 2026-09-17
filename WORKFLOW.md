# Project Workflow

A visual walkthrough of how data moves through this project, end to end —
from the two raw source tables to what a stakeholder sees in their
browser. Written for anyone getting oriented on this codebase; see
`CLAUDE.md` for the deep implementation detail behind each step, and
`README.md` for the exact commands.

## 1. The big picture

```mermaid
flowchart LR
    subgraph Sources["Raw data (already exists, never modified)"]
        A1[("IPAYDATA<br/>(Inficare)")]
        A2[("iPayHub_Data<br/>(iSendHub)")]
    end

    subgraph ETL["ETL — Codes/postgres_pipeline.py (scheduled, not continuous)"]
        B1["Clean + normalize<br/>(cleaning_config.yml rules)"]
        B2["Clean tables<br/>ipaydata_clean / ipayhubdata_clean"]
    end

    subgraph Reporting["Reporting layer (Postgres materialized views)"]
        C1[["business_report<br/>(unified, one row per transaction)"]]
        C2[["mv_dashboard_rows"]]
        C3[["mv_dashboard_tat_rows"]]
        C4[["mv_dashboard_creation_time_rows"]]
        C5[["mv_dashboard_status_rows"]]
    end

    subgraph Serving["Serving layer"]
        D1["FastAPI (Codes/api/)<br/>login + /api/* endpoints"]
        D2["Dashboard HTML<br/>(interactive_dashboard_template.html)"]
    end

    E["Browser<br/>(stakeholder / Accounts / business user)"]

    A1 --> B1
    A2 --> B1
    B1 --> B2
    B2 --> C1
    C1 --> C2 & C3 & C4 & C5
    C2 & C3 & C4 & C5 --> D1
    D1 --> D2
    D2 --> E
    E -- "login, filters" --> D1
```

**In plain terms:** the two raw tables are never touched directly by the
dashboard. A scheduled job (the ETL) copies and cleans their data into
Postgres's own storage, builds one unified table (`business_report`), then
pre-computes four ready-to-chart summary tables from it. The dashboard's
web server only ever reads those four summary tables — never the raw
ones, and never `business_report` directly (see step 3 for why).

## 2. ETL run, in detail

```mermaid
flowchart TD
    Start(["docker compose run --rm etl<br/>python Codes/postgres_pipeline.py"]) --> Watermark{"Full refresh,<br/>or incremental?"}
    Watermark -- "--full-refresh" --> ReadAll["Read every raw row"]
    Watermark -- "(default)" --> ReadNew["Read only rows newer<br/>than etl_watermark"]
    ReadAll --> Clean
    ReadNew --> Clean["DataCleaner.clean()<br/>(cleaning_config.yml: remap values,<br/>parse timestamps, fill defaults)"]
    Clean --> TZ["DataCleaner.normalize_timezones()<br/>(Paid_Date only — see CLAUDE.md's<br/>TRN_Date vs TRN_Date_Normalized note)"]
    TZ --> Upsert["Batched upsert into<br/>ipaydata_clean / ipayhubdata_clean<br/>(INSERT ... ON CONFLICT DO UPDATE)"]
    Upsert --> Advance["Advance etl_watermark"]
    Advance --> BothSources{"Both sources<br/>(Inficare + iSendHub) done?"}
    BothSources -- no --> Watermark
    BothSources -- yes --> Refresh["REFRESH MATERIALIZED VIEW CONCURRENTLY<br/>business_report"]
    Refresh --> Refresh4["REFRESH all 4<br/>mv_dashboard_* views"]
    Refresh4 --> Done(["Dashboard now shows<br/>the new data"])
```

**In plain terms:** this is the only step that changes what the dashboard
shows. Nothing updates live as new transactions happen — someone (or a
scheduled task) has to run the ETL, which takes the latest raw data,
cleans it, and refreshes the reporting tables. Until that runs, the
dashboard keeps showing whatever it showed after the last run.

## 3. Why the dashboard reads pre-computed summaries, not raw data live

```mermaid
flowchart LR
    subgraph Rejected["Tried and rejected — too slow"]
        R1["Browser asks for a chart"] --> R2["API aggregates business_report<br/>(~1M+ rows) on the spot"]
        R2 --> R3["30-40+ seconds per chart"]
    end
    subgraph Actual["What actually happens"]
        S1["ETL run finishes"] --> S2["4 mv_dashboard_* views<br/>already hold the aggregated numbers"]
        S2 --> S3["Browser asks for a chart"]
        S3 --> S4["API reads the pre-aggregated table<br/>— no computation needed"]
        S4 --> S5["Seconds, not tens of seconds"]
    end
```

**In plain terms:** aggregating a million-plus rows every time someone
opens a tab was measured directly and found far too slow. So that
aggregation work happens once, right after the ETL run, and the dashboard
just reads the already-summarized result.

## 4. Logging in and loading the dashboard

```mermaid
sequenceDiagram
    participant U as Browser
    participant C as Caddy (TLS)
    participant A as FastAPI (api container)
    participant P as Postgres

    U->>C: GET /
    C->>A: GET /
    A-->>U: redirect to /login
    U->>C: POST /api/login (email, password)
    C->>A: POST /api/login
    A->>P: verify password (argon2), create session
    P-->>A: session token
    A-->>U: Set-Cookie: session_id (HttpOnly, Secure)
    U->>C: GET /dashboard
    C->>A: GET /dashboard (cookie attached)
    A->>P: validate session
    A-->>U: dashboard page (empty shell)
    par All 6 fetched together
        U->>A: GET /api/meta
        U->>A: GET /api/rows
        U->>A: GET /api/tat_rows
        U->>A: GET /api/creation_time_rows
        U->>A: GET /api/status_rows
        U->>A: GET /api/forecast_ml
    end
    A->>P: read mv_dashboard_* views
    P-->>A: pre-aggregated rows
    A-->>U: JSON (gzip-compressed)
    U->>U: render charts/tables in the browser
```

**In plain terms:** the page itself loads almost instantly — it's an
empty shell. The moment it appears, the browser fires off six requests at
once for the actual numbers, and only once those come back do the charts
draw themselves in. That wait is what a "Loading dashboard data…" screen
is showing.

## 5. How the whole thing is deployed (Docker)

```mermaid
flowchart LR
    Internet(("You, in a browser")) -- "HTTPS :443" --> Caddy["Caddy container<br/>TLS + gzip"]
    Caddy -- "plain HTTP, internal" --> API["api container<br/>FastAPI + uvicorn"]
    API -- "asyncpg, over the internet" --> RDS[("External Postgres<br/>(e.g. AWS RDS —<br/>this project doesn't run its own DB)")]

    subgraph OneOff["One-off jobs (run on demand, not always-on)"]
        Migrate["migrate<br/>applies sql/*.sql"]
        ETLJob["etl<br/>postgres_pipeline.py"]
        CreateUser["create-user<br/>provisions a login"]
    end
    OneOff -.-> RDS
```

**In plain terms:** only two containers run continuously — `caddy`
(handles HTTPS) and `api` (serves the dashboard). Everything else
(migrations, ETL, creating an account) is run by hand, once, whenever
it's needed, against the same external database. See `README.md`'s
Docker section for the exact commands, and its "Deploying a CODE change"
note — editing code doesn't reach the running `api` container until it's
rebuilt.

## 6. What each document in this repo is for

| Document | Audience | What it's for |
|---|---|---|
| `README.md` | Anyone setting this up | Step-by-step install/run instructions |
| `CLAUDE.md` | Developers/maintainers | Full architecture, every design decision and why, gotchas |
| `WORKFLOW.md` (this file) | Anyone getting oriented | The same flow as `CLAUDE.md`, but as pictures |
| `DASHBOARD_GUIDE.md` | Dashboard end-users (non-technical) | What the dashboard shows, tab by tab, in plain language |
