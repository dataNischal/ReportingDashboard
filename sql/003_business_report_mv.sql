-- ============================================================
-- 003_business_report_mv.sql
-- The `business_report` materialized view: one row per transaction, unioning
-- both cleaned sources into the SAME unified schema
-- Codes/cleaning_functions.py's create_business_report() +
-- Codes/analytics.py's prepare_analytics_data() already produce today,
-- translated 1:1 from cleaning_config.yml's `business_reporting` section
-- and analytics.py's bucket/segment constants (VOLUME_BRACKET_BINS/LABELS,
-- TAT_BUCKET_LABELS, AGENT_SEGMENT_MAP) so dashboard output is unchanged.
--
-- Deliberately kept at transaction grain (not pre-aggregated) so it can
-- back all four dashboard payload shapes (rows / tat_rows /
-- creation_time_rows / status_rows) via different GROUP BY + WHERE
-- combinations at the API layer -- see Codes/api/routers/dashboard.py.
--
-- IMPORTANT -- duplication this migration deliberately accepts: the divide/
-- duration_hours formulas and the bucket/segment thresholds below are
-- hand-translated from cleaning_config.yml and analytics.py into SQL, so
-- there are now two places that encode this business logic (Python, for
-- anyone still running the legacy CSV/S3 pipeline in parallel, and this
-- SQL, for the live Postgres path). If either the formulas, exclude list,
-- or bucket boundaries ever change, BOTH must be updated together. Retiring
-- the legacy file-based pipeline once this is live removes the duplication
-- entirely -- see the migration plan's Phase 6.
-- ============================================================

-- Source_Row_Key (below) is the view's real per-row identity, used only
-- for idx_business_report_pk -- NOT the same thing as the "Control_No"
-- column still exposed for reporting. Inficare's Control_No is not
-- reliably unique per transaction (confirmed against real data: ~264
-- groups of distinct transactions collide on one Control_No string, a
-- float/scientific-notation precision-loss artifact upstream) while
-- Transaction_Code is 100% unique -- see sql/002_clean_tables.sql's
-- comment on ipaydata_clean's own natural-key constraint for the full
-- finding. iSendHub's Tracking_No is already 100% unique, so it plays both
-- roles there (as it always has).
--
-- "TRN_Date" vs "TRN_Date_Normalized": two columns, two different jobs.
-- "TRN_Date" is each source's transaction date exactly as recorded --
-- Inficare's TRN_Date unchanged, iSendHub's DOT(Date_Of_TXN) ALSO now
-- unchanged (raw Asia/Kathmandu/Nepal wall-clock time, straight from
-- ipayhubdata_clean -- see cleaning_config.yml's timezone_normalization
-- for why that column is deliberately no longer converted at clean time).
-- This is the column every Volume/Transaction/Geography/Partners/etc.
-- Year/Month/Day/Hour dimension is built from below -- confirmed in
-- practice that converting iSendHub's date to Asia/Kuala_Lumpur (Malaysia,
-- this project's target_timezone) BEFORE this view ever saw it was pushing
-- late-day-of-month transactions into the next calendar day/month (Nepal
-- is UTC+5:45, Malaysia is UTC+8 -- a +2h15m shift), so the dashboard's
-- day/month reporting no longer matched the Accounts department's own MIS,
-- which reports on each transaction's original (Nepal) recorded date.
--
-- "TRN_Date_Normalized" is the OLD behavior, computed here instead:
-- Inficare unchanged (already Asia/Kuala_Lumpur, per this project's
-- existing documented assumption -- cleaning_config.yml's `mode: skip`);
-- iSendHub's raw DOT(Date_Of_TXN) converted Asia/Kathmandu -> Asia/
-- Kuala_Lumpur right here in SQL. Used ONLY for Turn_Around_Time_Hours
-- (and mv_dashboard_tat_rows' own Year/Month/Year-Month bucketing --
-- sql/004_dashboard_payload_mvs.sql) -- TAT is an elapsed-duration metric
-- between two timestamps, and mixing a raw-clock TRN_Date with an
-- already-normalized Paid_Date (Paid_Date is unchanged by this fix -- both
-- sources' Paid_Date was already being timezone-normalized at clean time,
-- and that's still correct) would silently skew every iSendHub TAT value
-- by that same ~2h15m.
CREATE MATERIALIZED VIEW IF NOT EXISTS business_report AS

WITH inficare AS (
    SELECT
        "Control_No",
        "Transaction_Code"                                      AS "Source_Row_Key",
        "TRN_Date"::timestamp                                   AS "TRN_Date",
        "TRN_Date"::timestamp                                   AS "TRN_Date_Normalized",
        "Agent_Name",
        "Transaction_Method",
        "Payment_Type",
        "Sending_Country",
        "Sending_Country_Currency",
        "Receiver_Country",
        "Payout_Currency",
        CASE
            WHEN "Exchange_Rate" IS NULL OR "Exchange_Rate"::numeric = 0 THEN NULL
            ELSE "Transaction_Amount"::numeric / "Exchange_Rate"::numeric
        END                                                      AS "Transaction_Amount_USD",
        "transstatus",
        "Paid_Date"::timestamp                                  AS "Paid_Date",
        'Inficare'                                               AS source_system
    FROM ipaydata_clean
    WHERE lower(trim("Agent_Name")) NOT IN (
        lower('INTERNAL SGP TEST'), lower('IPAY INTERNAL TEST'), lower('TEST PATNER')
    )
),

isendhub AS (
    SELECT
        "Tracking_No"                                            AS "Control_No",
        "Tracking_No"                                            AS "Source_Row_Key",
        "DOT(Date_Of_TXN)"::timestamp                            AS "TRN_Date",
        (("DOT(Date_Of_TXN)"::timestamp AT TIME ZONE 'Asia/Kathmandu')
            AT TIME ZONE 'Asia/Kuala_Lumpur')                    AS "TRN_Date_Normalized",
        "Sending_Agent_Name"                                      AS "Agent_Name",
        "Payout_Agent_Name"                                       AS "Transaction_Method",
        "Payment_Type",
        "Send_Country"                                            AS "Sending_Country",
        "Sending_Currency"                                        AS "Sending_Country_Currency",
        "Payout_Country"                                          AS "Receiver_Country",
        "Payout_Currency",
        CASE
            WHEN "Send_Cost_Rate" IS NULL OR "Send_Cost_Rate"::numeric = 0 THEN NULL
            ELSE "Send_Amount"::numeric / "Send_Cost_Rate"::numeric
        END                                                       AS "Transaction_Amount_USD",
        "Status"                                                  AS "transstatus",
        "Paid_Date"::timestamp                                   AS "Paid_Date",
        'iSendHub'                                                AS source_system
    FROM ipayhubdata_clean
    WHERE lower(trim("Sending_Agent_Name")) NOT IN (
        lower('INTERNAL SGP TEST'), lower('IPAY INTERNAL TEST'), lower('TEST PATNER')
    )
),

unioned AS (
    SELECT * FROM inficare
    UNION ALL
    SELECT * FROM isendhub
)

SELECT
    "Control_No",
    "Source_Row_Key",
    "TRN_Date",
    "TRN_Date_Normalized",
    EXTRACT(YEAR  FROM "TRN_Date")::int            AS "Year",
    EXTRACT(MONTH FROM "TRN_Date")::int            AS "Month",
    EXTRACT(DAY   FROM "TRN_Date")::int            AS "Day",
    EXTRACT(HOUR  FROM "TRN_Date")::int            AS "Hour",
    to_char("TRN_Date", 'FMMonth')                 AS "Month Name",
    to_char("TRN_Date", 'YYYY-MM')                 AS "Year-Month",

    "Sending_Country",
    "Receiver_Country",
    COALESCE("Sending_Country", 'UNKNOWN') || ' - ' || COALESCE("Receiver_Country", 'UNKNOWN')
                                                    AS "Corridors",
    "Payment_Type",
    "Agent_Name",
    "Transaction_Method",
    "Sending_Country_Currency",
    "Payout_Currency",
    COALESCE("Sending_Country_Currency", 'UNKNOWN') || ' - ' || COALESCE("Payout_Currency", 'UNKNOWN')
                                                    AS "Currencies_Pair",

    "Transaction_Amount_USD",

    -- Volume_Bracket -- mirrors analytics.py's VOLUME_BRACKET_BINS/LABELS
    -- ([0, 500, 2500, 5000, 10000, 25000, inf], include_lowest=True).
    CASE
        WHEN "Transaction_Amount_USD" IS NULL      THEN 'Unknown'
        WHEN "Transaction_Amount_USD" <= 500       THEN '0-500'
        WHEN "Transaction_Amount_USD" <= 2500      THEN '500-2500'
        WHEN "Transaction_Amount_USD" <= 5000      THEN '2500-5000'
        WHEN "Transaction_Amount_USD" <= 10000     THEN '5000-10000'
        WHEN "Transaction_Amount_USD" <= 25000     THEN '10000-25000'
        ELSE '25000+'
    END                                             AS "Volume_Bracket",

    -- Agent_Segment -- mirrors analytics.py's AGENT_SEGMENT_MAP (case/
    -- whitespace-insensitive match against 3 named groups; default "Last
    -- Mile Settlement" for anything unmatched).
    CASE
        WHEN upper(trim("Agent_Name")) IN (
            'BHUTAN NATIONAL BANK (PAASPAY)', 'CHARIOT TRAVELS PRIVATE LIMITED',
            'SPEED REMIT (PAASPAY - AUD)', 'SPEED REMIT (PAASPAY - SGD)'
        ) THEN 'PaaSPay'
        WHEN upper(trim("Agent_Name")) IN (
            'HAND MONEY PAYMENTS, S.A. DE C.V.', 'ISEND BIZ SGP'
        ) THEN 'iSend Biz'
        WHEN upper(trim("Agent_Name")) IN (
            'ISEND APP USA', 'ISEND AUS (APPS)', 'ISEND PTE LTD (APPS)', 'ISEND PTE LTD.',
            'ISEND PTE LTD-FREE FEE', 'ISEND PTE LTD-HO', 'ISEND PTE. LTD. (USD)',
            'ISEND_AUS_APPS (ISEND GLOBAL)', 'TRANSCASH INTERNATIONAL PTY LTD',
            'TRANSCASH INTERNATIONAL PTY LTD (HO)', 'TRANSCASH INTERNATIONAL PTY. LTD. (APP)'
        ) THEN 'iSend C2C'
        ELSE 'Last Mile Settlement'
    END                                             AS "Agent_Segment",

    "transstatus",
    "Paid_Date",

    -- Deliberately TRN_Date_Normalized here, NOT TRN_Date -- see this
    -- file's header comment. Paid_Date is already timezone-normalized for
    -- both sources (unchanged by this fix), so subtracting the ALSO-
    -- normalized TRN_Date_Normalized keeps both operands on the same
    -- clock, giving a correct elapsed duration regardless of source system.
    (EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0)::numeric
                                                    AS "Turn_Around_Time_Hours",

    -- TAT_Bucket -- mirrors analytics.py's np.select over
    -- TAT_BUCKET_LABELS ("0-1h","1-6h","6-24h","1-3 Days","3-7 Days","7+ Days"),
    -- with "Not Yet Paid" (null) and "Negative (Data Flag)" (<0) called out
    -- explicitly, evaluated in the same order.
    CASE
        WHEN "Paid_Date" IS NULL OR "TRN_Date_Normalized" IS NULL             THEN 'Not Yet Paid'
        WHEN EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0 < 0    THEN 'Negative (Data Flag)'
        WHEN EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0 <= 1   THEN '0-1h'
        WHEN EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0 <= 6   THEN '1-6h'
        WHEN EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0 <= 24  THEN '6-24h'
        WHEN EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0 <= 72  THEN '1-3 Days'
        WHEN EXTRACT(EPOCH FROM ("Paid_Date" - "TRN_Date_Normalized")) / 3600.0 <= 168 THEN '3-7 Days'
        ELSE '7+ Days'
    END                                             AS "TAT_Bucket",

    source_system
FROM unioned;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY (Codes/postgres_
-- pipeline.py's refresh_business_report()) -- without a unique index,
-- Postgres can only do a full-lock refresh, which would block every
-- dashboard query for the refresh's duration. Keyed on Source_Row_Key, NOT
-- Control_No -- see this file's header comment and sql/002_clean_tables.sql
-- for why Control_No alone is not a safe uniqueness guarantee for Inficare.
CREATE UNIQUE INDEX IF NOT EXISTS idx_business_report_pk
    ON business_report (source_system, "Source_Row_Key");

-- Supporting indexes for the dashboard API's common filter/group-by
-- columns (Codes/api/routers/dashboard.py) -- add more here as real query
-- plans (EXPLAIN ANALYZE) show they're needed; this covers the filters
-- every payload type shares.
CREATE INDEX IF NOT EXISTS idx_business_report_trn_date   ON business_report ("TRN_Date");
CREATE INDEX IF NOT EXISTS idx_business_report_transstatus ON business_report ("transstatus");
CREATE INDEX IF NOT EXISTS idx_business_report_year_month ON business_report ("Year", "Month");
CREATE INDEX IF NOT EXISTS idx_business_report_agent_name ON business_report ("Agent_Name");
CREATE INDEX IF NOT EXISTS idx_business_report_receiver_country ON business_report ("Receiver_Country");
