-- ============================================================
-- 002_clean_tables.sql
-- Creates ipaydata_clean / ipayhubdata_clean by CLONING the existing raw
-- tables' structure (LIKE ... INCLUDING ALL), rather than hand-transcribing
-- a column list. This is deliberate, not lazy: Codes/cleaning_functions.py's
-- clean_data() never adds or drops columns -- it only rewrites values in
-- place (timestamp parsing, mapping_ref lookups, defaults) -- so the clean
-- table's column set is BY DEFINITION identical to the raw table's. Cloning
-- guarantees exact parity with whatever IPAYDATA/iPayHub_Data's real schema
-- is, without this migration silently drifting out of sync with it.
--
-- WATERMARK: neither IPAYDATA nor iPayHub_Data has a monotonically
-- increasing "_ingested_at"-style insertion timestamp (confirmed directly
-- against isendstaging's real schema), and the existing etl_sync_logs
-- table there is a DIFFERENT, external process's own watermark into these
-- tables -- not a per-row insertion log this project's own incremental
-- reads could use. Rather than ALTER TABLE the raw tables to add one,
-- Codes/postgres_pipeline.py's PostgresETLPipeline.WATERMARK_COLUMN uses
-- each source's own primary business-date column instead (TRN_Date for
-- Inficare, DOT(Date_Of_TXN) for iSendHub) -- confirmed these are the same
-- columns an external upstream sync process already treats as
-- authoritative for "what's new" (their current max exactly matched that
-- process's own recorded cursor at the time this was checked). Trade-off
-- accepted: a row inserted later with an OLDER business date than
-- what's already been read (a backdated correction) would be missed by a
-- plain incremental run and would need a --full-refresh to be picked up.
-- If a true monotonic insertion-order column is ever added to these raw
-- tables, switch WATERMARK_COLUMN to use it instead.
-- ============================================================

CREATE TABLE IF NOT EXISTS ipaydata_clean (LIKE "IPAYDATA" INCLUDING ALL);
CREATE TABLE IF NOT EXISTS ipayhubdata_clean (LIKE "iPayHub_Data" INCLUDING ALL);

-- Natural keys for idempotent UPSERT (Codes/postgres_pipeline.py's
-- upsert_clean_table() does INSERT ... ON CONFLICT (<this column>) DO
-- UPDATE).
--
-- Inficare's natural key is "Transaction_Code", NOT "Control_No" --
-- verified against the real isendstaging data: Control_No has ~264 groups
-- of colliding values (up to 9 genuinely distinct transactions -- different
-- sender/receiver/amount/time -- sharing one Control_No string, apparently
-- from a float/scientific-notation precision loss upstream, e.g.
-- "2.025090108e+14"). Transaction_Code is confirmed 100% unique across all
-- 903,851 IPAYDATA rows. Using Control_No here would silently collapse
-- those ~264 groups down to one row each on every upsert -- real
-- transactions disappearing from ipaydata_clean/business_report with no
-- error raised. Control_No is untouched everywhere else (still a normal,
-- non-unique column, still the business_reporting output field) -- see
-- sql/003_business_report_mv.sql's Source_Row_Key for how the materialized
-- view's own identity index accounts for this.
--
-- iSendHub's Tracking_No, by contrast, IS confirmed 100% unique per-row --
-- unchanged.
--
-- Wrapped in DO blocks -- unlike CREATE TABLE/INDEX, Postgres has no
-- ADD CONSTRAINT IF NOT EXISTS, so a plain ALTER TABLE ADD CONSTRAINT is
-- NOT safe to re-run against an already-migrated database (confirmed
-- directly: re-running this file a second time fails with "relation
-- ipaydata_clean_transaction_code_key already exists" -- Postgres raises
-- duplicate_table here, not duplicate_object, since the constraint's
-- backing unique index shares the table/index relation namespace).
DO $$ BEGIN
    ALTER TABLE ipaydata_clean
        ADD CONSTRAINT ipaydata_clean_transaction_code_key UNIQUE ("Transaction_Code");
EXCEPTION
    WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE ipayhubdata_clean
        ADD CONSTRAINT ipayhubdata_clean_tracking_no_key UNIQUE ("Tracking_No");
EXCEPTION
    WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

-- Audit column: when this row was last (re)cleaned -- not in the raw
-- table, added here since it's clean-table-specific bookkeeping.
ALTER TABLE ipaydata_clean ADD COLUMN IF NOT EXISTS _cleaned_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE ipayhubdata_clean ADD COLUMN IF NOT EXISTS _cleaned_at timestamptz NOT NULL DEFAULT now();

-- Indexes supporting the business_report materialized view's refresh scan
-- and any direct queries against the clean tables themselves.
CREATE INDEX IF NOT EXISTS idx_ipaydata_clean_agent_name ON ipaydata_clean ("Agent_Name");
CREATE INDEX IF NOT EXISTS idx_ipayhubdata_clean_agent_name ON ipayhubdata_clean ("Sending_Agent_Name");
