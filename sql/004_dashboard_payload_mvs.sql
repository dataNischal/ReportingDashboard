-- ============================================================
-- 004_dashboard_payload_mvs.sql
-- Pre-aggregated materialized views, one per dashboard payload shape
-- (rows / tat_rows / creation_time_rows / status_rows), computed once per
-- ETL run instead of live-aggregated on every dashboard request.
--
-- WHY THIS EXISTS: Codes/api/repositories.py originally ran these same
-- GROUP BY queries directly against business_report on every request.
-- Confirmed directly against the live isendstaging data that this is too
-- slow to serve interactively: the unfiltered "rows" aggregation (16
-- dimensions over ~903K Payment-status rows -> 94,231 groups) took 30-40+
-- seconds end to end, and EXPLAIN ANALYZE showed why -- even a trivial
-- 2-column GROUP BY forces a full parallel sequential scan of
-- business_report plus a sort that spills to disk on this RDS instance
-- (work_mem is only 4MB there; bumping it to 256MB for the session only
-- brought the full query down to ~18-27s, still not interactive). This
-- isn't something a WHERE-clause index can fix -- a high-cardinality
-- multi-column GROUP BY over the whole table is fundamentally an
-- OLAP-shaped query a per-request live aggregation doesn't suit, no
-- matter how the SQL is tuned.
--
-- Each view here mirrors -- EXACTLY, including which filters are/aren't
-- semantically meaningful against it -- interactive_dashboard_generator.py's
-- DIMENSION_COLUMNS / TAT_DIMENSION_COLUMNS / CREATION_TIME_DIMENSION_
-- COLUMNS / STATUS_DIMENSION_COLUMNS (the static-file generator's own
-- payload shapes) and the dashboard template's documented client-side
-- filter contract (interactive_dashboard_template.html's
-- GLOBAL_FILTER_IDS/applyGenericFilters() comment: "transstatus is a
-- no-op against rawData ... Corridors doesn't apply to TAT", etc.) -- a
-- filter on a dimension a given view doesn't carry is simply not
-- applicable to it, exactly as today's client-side filtering (and the
-- original static-file design) already treats it, not a bug introduced
-- by pre-aggregating.
--
-- Casing normalization (upper(trim(...)) on Payment_Type, Agent_Name,
-- Transaction_Method, transstatus, Sending_Country_Currency,
-- Payout_Currency -- Sending_Country/Receiver_Country are already
-- normalized at clean-time) is baked into these views directly, computed
-- once here instead of on every request -- see Codes/api/repositories.py's
-- prior NORMALIZED_TEXT_COLUMNS comment for why this is needed at all
-- (confirmed against real data: Payment_Type has 5 raw values that
-- collapse to 4 once normalized; Transaction_Method 163 -> 161; several
-- transstatus values don't match the dashboard's uppercase-keyed
-- STATUS_COLOR_MAP without it).
--
-- Refreshed by Codes/postgres_pipeline.py's PostgresETLPipeline
-- immediately after business_report itself refreshes (these depend on
-- it), same REFRESH MATERIALIZED VIEW CONCURRENTLY mechanism -- each
-- needs its own unique index for that (below), since a GROUP BY result's
-- natural key IS its full dimension tuple.
-- ============================================================

-- ------------------------------------------------------------
-- mv_dashboard_rows -- mirrors Codes/api/repositories.py's get_rows() /
-- interactive_dashboard_generator.py's DIMENSION_COLUMNS. Payment-only.
-- ------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dashboard_rows AS
SELECT
    "Year", "Month", "Day", "Month Name", "Year-Month",
    upper(trim("Sending_Country"))          AS "Sending_Country",
    upper(trim("Receiver_Country"))         AS "Receiver_Country",
    "Corridors",
    upper(trim("Payment_Type"))             AS "Payment_Type",
    upper(trim("Agent_Name"))               AS "Agent_Name",
    upper(trim("Transaction_Method"))       AS "Transaction_Method",
    upper(trim("Sending_Country_Currency")) AS "Sending_Country_Currency",
    upper(trim("Payout_Currency"))          AS "Payout_Currency",
    "Currencies_Pair", "Volume_Bracket", "Agent_Segment",
    SUM("Transaction_Amount_USD") AS "Volume",
    COUNT(*)                      AS "Transactions"
FROM business_report
WHERE upper("transstatus") = 'PAYMENT'
GROUP BY
    "Year", "Month", "Day", "Month Name", "Year-Month",
    upper(trim("Sending_Country")), upper(trim("Receiver_Country")), "Corridors",
    upper(trim("Payment_Type")), upper(trim("Agent_Name")), upper(trim("Transaction_Method")),
    upper(trim("Sending_Country_Currency")), upper(trim("Payout_Currency")),
    "Currencies_Pair", "Volume_Bracket", "Agent_Segment";

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_dashboard_rows_pk ON mv_dashboard_rows (
    "Year", "Month", "Day", "Month Name", "Year-Month", "Sending_Country", "Receiver_Country",
    "Corridors", "Payment_Type", "Agent_Name", "Transaction_Method", "Sending_Country_Currency",
    "Payout_Currency", "Currencies_Pair", "Volume_Bracket", "Agent_Segment"
);
CREATE INDEX IF NOT EXISTS idx_mv_dashboard_rows_year_month ON mv_dashboard_rows ("Year", "Month");

-- ------------------------------------------------------------
-- mv_dashboard_tat_rows -- mirrors get_tat_rows() / TAT_DIMENSION_COLUMNS.
-- Payment-only AND TAT-anomaly-free (null/negative Turn_Around_Time_Hours
-- excluded, matching the live query's exact WHERE conditions).
--
-- Year/Month/Year-Month here are business_report's regular (raw-TRN_Date-
-- based) columns -- same clock as every other dashboard tab, so a
-- transaction lands in the same Year-Month here as it does everywhere
-- else. Only the actual duration metrics (Turn_Around_Time_Hours,
-- TAT_Bucket, both already computed in sql/003_business_report_mv.sql
-- from TRN_Date_Normalized) use the normalized clock -- see that file's
-- header comment for why mixing a raw-clock date with the already-
-- normalized Paid_Date would silently skew iSendHub TAT values.
-- ------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dashboard_tat_rows AS
SELECT
    "Year", "Month", "Year-Month",
    upper(trim("Receiver_Country"))   AS "Receiver_Country",
    upper(trim("Agent_Name"))         AS "Agent_Name",
    upper(trim("Transaction_Method")) AS "Transaction_Method",
    upper(trim("Payment_Type"))       AS "Payment_Type",
    "TAT_Bucket",
    SUM("Turn_Around_Time_Hours")   AS "TAT_Sum",
    COUNT("Turn_Around_Time_Hours") AS "TAT_Count",
    COUNT(*)                        AS "Transactions",
    SUM("Transaction_Amount_USD")   AS "Volume"
FROM business_report
WHERE upper("transstatus") = 'PAYMENT'
  AND "Turn_Around_Time_Hours" IS NOT NULL
  AND "Turn_Around_Time_Hours" >= 0
GROUP BY
    "Year", "Month", "Year-Month",
    upper(trim("Receiver_Country")), upper(trim("Agent_Name")), upper(trim("Transaction_Method")),
    upper(trim("Payment_Type")), "TAT_Bucket";

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_dashboard_tat_rows_pk ON mv_dashboard_tat_rows (
    "Year", "Month", "Year-Month", "Receiver_Country", "Agent_Name", "Transaction_Method",
    "Payment_Type", "TAT_Bucket"
);

-- ------------------------------------------------------------
-- mv_dashboard_creation_time_rows -- mirrors get_creation_time_rows() /
-- CREATION_TIME_DIMENSION_COLUMNS. Payment-only, hour-of-day granularity.
-- ------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dashboard_creation_time_rows AS
SELECT
    "Year", "Month", "Day", "Hour",
    upper(trim("Sending_Country"))    AS "Sending_Country",
    upper(trim("Receiver_Country"))   AS "Receiver_Country",
    upper(trim("Payment_Type"))       AS "Payment_Type",
    upper(trim("Agent_Name"))         AS "Agent_Name",
    upper(trim("Transaction_Method")) AS "Transaction_Method",
    COUNT(*)                      AS "Transactions",
    SUM("Transaction_Amount_USD") AS "Volume"
FROM business_report
WHERE upper("transstatus") = 'PAYMENT'
GROUP BY
    "Year", "Month", "Day", "Hour",
    upper(trim("Sending_Country")), upper(trim("Receiver_Country")), upper(trim("Payment_Type")),
    upper(trim("Agent_Name")), upper(trim("Transaction_Method"));

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_dashboard_creation_time_rows_pk ON mv_dashboard_creation_time_rows (
    "Year", "Month", "Day", "Hour", "Sending_Country", "Receiver_Country", "Payment_Type",
    "Agent_Name", "Transaction_Method"
);
CREATE INDEX IF NOT EXISTS idx_mv_dashboard_creation_time_rows_year_month
    ON mv_dashboard_creation_time_rows ("Year", "Month");

-- ------------------------------------------------------------
-- mv_dashboard_status_rows -- mirrors get_status_rows() /
-- STATUS_DIMENSION_COLUMNS. UNFILTERED by status (every transstatus
-- value, not just Payment) -- powers the status donut / Status-per-
-- Bracket / Segmentation-by-status charts.
-- ------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dashboard_status_rows AS
SELECT
    "Year", "Month", "Year-Month",
    upper(trim("Sending_Country"))          AS "Sending_Country",
    upper(trim("Receiver_Country"))         AS "Receiver_Country",
    upper(trim("Payment_Type"))             AS "Payment_Type",
    upper(trim("Agent_Name"))               AS "Agent_Name",
    upper(trim("Transaction_Method"))       AS "Transaction_Method",
    upper(trim("Sending_Country_Currency")) AS "Sending_Country_Currency",
    upper(trim("Payout_Currency"))          AS "Payout_Currency",
    upper(trim("transstatus"))              AS "transstatus",
    "Volume_Bracket", "Agent_Segment",
    SUM("Transaction_Amount_USD") AS "Volume",
    COUNT(*)                      AS "Transactions"
FROM business_report
GROUP BY
    "Year", "Month", "Year-Month",
    upper(trim("Sending_Country")), upper(trim("Receiver_Country")), upper(trim("Payment_Type")),
    upper(trim("Agent_Name")), upper(trim("Transaction_Method")),
    upper(trim("Sending_Country_Currency")), upper(trim("Payout_Currency")),
    upper(trim("transstatus")), "Volume_Bracket", "Agent_Segment";

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_dashboard_status_rows_pk ON mv_dashboard_status_rows (
    "Year", "Month", "Year-Month", "Sending_Country", "Receiver_Country", "Payment_Type",
    "Agent_Name", "Transaction_Method", "Sending_Country_Currency", "Payout_Currency",
    "transstatus", "Volume_Bracket", "Agent_Segment"
);
CREATE INDEX IF NOT EXISTS idx_mv_dashboard_status_rows_year_month ON mv_dashboard_status_rows ("Year", "Month");
