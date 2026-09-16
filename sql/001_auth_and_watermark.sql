-- ============================================================
-- 001_auth_and_watermark.sql
-- Foundational tables: authentication + ETL watermark tracking.
-- Run once, before the clean-table/materialized-view migrations.
-- ============================================================

-- gen_random_uuid() for session IDs; citext for case-insensitive email match
-- (so "Nischal@isend.com.sg" and "nischal@isend.com.sg" collide correctly).
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ------------------------------------------------------------
-- USERS
-- password_hash stores an argon2id hash (see Codes/api/security.py) --
-- NEVER a plaintext or reversibly-encrypted password.
--
-- `role` is a plain CHECK-constrained TEXT column (not a native ENUM
-- type) on purpose -- adding a role later is one ALTER TABLE ... DROP
-- CONSTRAINT / ADD CONSTRAINT, vs. ALTER TYPE ... ADD VALUE's restrictions
-- (can't run inside a transaction, can't be removed).
--
-- NOTE on row-level security: PostgreSQL's actual ROW LEVEL SECURITY
-- feature (CREATE POLICY / ENABLE ROW LEVEL SECURITY) only applies to
-- ordinary tables, NOT to materialized views -- so it cannot be attached
-- directly to `business_report` (see sql/003_business_report_mv.sql).
-- `role` is stored here so that, when per-role row filtering is needed,
-- it can be enforced the same way this project already enforces every
-- other filter: as an additional SQL WHERE predicate built from the
-- authenticated user's role inside Codes/api/routers/dashboard.py (see
-- Codes/api/deps.py's require_role()) -- e.g. an "accounts" role only
-- ever seeing rows for its own Agent_Name(s). That per-role visibility
-- mapping doesn't exist yet; wire it in dashboard.py's build_where() once
-- the business rule for who-sees-what is defined.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dashboard_credential (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT,
    email          CITEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'stakeholder'
                       CHECK (role IN ('admin', 'stakeholder', 'business', 'accounts')),
    is_active      BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dashboard_credential_role ON dashboard_credential (role);

-- ------------------------------------------------------------
-- SESSIONS
-- Server-side session store (not JWT): a session is a random opaque token
-- (the row's PK) handed to the browser as an HttpOnly/Secure cookie. Every
-- authenticated request looks the token up here, checks expires_at/revoked,
-- and slides expires_at forward -- this is what makes the session NOT die
-- on a fixed timer the way the old S3 presigned URLs did (see project
-- history: the whole reason this auth system exists). Immediately
-- revocable (DELETE or set revoked=true) since nothing is self-contained
-- the way a JWT is.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       BIGINT NOT NULL REFERENCES dashboard_credential(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked       BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user_id ON app_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expires_at ON app_sessions (expires_at);

-- Periodic cleanup of dead sessions (run manually or via pg_cron/the API's
-- own housekeeping) -- expired rows are always rejected regardless, this
-- just keeps the table small:
--   DELETE FROM app_sessions WHERE expires_at < now() OR revoked;

-- ------------------------------------------------------------
-- ETL WATERMARK
-- One row per raw source table, tracking the last-processed cutoff so
-- Codes/postgres_pipeline.py can read only NEW rows on each run instead of
-- rescanning IPAYDATA/iPayHub_Data from scratch every time (the old S3/CSV
-- pipeline could afford a full stateless rebuild every run because CSV
-- exports are small/finite; a live, growing Postgres table is not).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etl_watermark (
    table_name  TEXT PRIMARY KEY,
    last_value  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- FORECAST CACHE
-- predictive_model.run_predictive_models() is expensive (LightGBM/XGBoost/
-- CatBoost/Prophet walk-forward validation) and was always meant to run
-- once per batch, never live per-request (see ARCHITECTURE.md section 3.1).
-- That doesn't change just because the transport changed from an embedded
-- JSON blob to an API -- this table holds the single latest precomputed
-- result so GET /api/forecast_ml is a cheap row lookup, not a live retrain.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS forecast_cache (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- single-row table
    payload      JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
