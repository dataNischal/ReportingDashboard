"""
postgres_pipeline.py -- incremental, YAML-driven ETL from the two raw
Postgres tables (IPAYDATA, iPayHub_Data) into their cleaned counterparts
(ipaydata_clean, ipayhubdata_clean), then a refresh of the business_report
materialized view (sql/003_business_report_mv.sql).

Deliberately reuses Codes/cleaning_functions.py's DataCleaner UNCHANGED --
the cleaning rules (mapping_ref lookups, timezone conversion, defaults) are
exactly the ones already tested against the CSV/S3 pipeline
(transaction_report.py), driven by the same cleaning_config.yml. Only the
I/O layer differs: a DataFrame comes from a SQL SELECT instead of
pd.read_csv, and goes back via an UPSERT instead of a CSV write.
BusinessReportBuilder is NOT used here -- that formula/exclude logic now
lives in the business_report materialized view's SQL definition instead
(see sql/003_business_report_mv.sql's own docstring for why that
duplication is accepted rather than avoided).

Credentials are NEVER stored in this repo -- DatabaseSettings.from_env()
reads entirely from environment variables (DATABASE_URL, or the individual
PG* variables), mirroring this project's existing convention for S3
(s3_writer.py).

Kept a synchronous, single-threaded batch script (psycopg2, not asyncpg)
deliberately -- this runs as one sequential offline job with nothing else
happening concurrently, so there's no blocked event loop to avoid the way
there is in the FastAPI app (see Codes/api/db.py's async engine); adding
asyncio here would add complexity with no concurrency to actually exploit.

Usage:
    python Codes/postgres_pipeline.py              # incremental (watermark-based)
    python Codes/postgres_pipeline.py --full-refresh  # reprocess every raw row
"""

import logging
import os
import sys

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, os.path.dirname(__file__))
from cleaning_functions import CleaningConfig, DataCleaner  # noqa: E402
from config import DatabaseSettings  # noqa: E402
from logging_config import configure_logging  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logger = logging.getLogger(__name__)


class PostgresETLPipeline:
    """
    Orchestrates one ETL run: raw Postgres tables -> DataCleaner -> clean
    tables (idempotent upsert) -> business_report materialized view
    refresh. Holds the engine/config for the pipeline's lifetime; a fresh
    instance is created per CLI invocation (see main()).
    """

    RAW_TABLE = {"Inficare": '"IPAYDATA"', "iSendHub": '"iPayHub_Data"'}
    CLEAN_TABLE = {"Inficare": "ipaydata_clean", "iSendHub": "ipayhubdata_clean"}

    # Neither raw table has an "_ingested_at"-style insertion timestamp
    # (checked directly against isendstaging's IPAYDATA/iPayHub_Data --
    # confirmed absent, and the existing etl_sync_logs table is a
    # DIFFERENT, external process's watermark into these tables, not a
    # per-row insertion log our own incremental reads could use). Rather
    # than ALTER TABLE the raw tables to add one, this uses each source's
    # own primary business-date column, whose current max already matches
    # etl_sync_logs.last_processed_id_or_date exactly for both tables --
    # confirming these are the same columns the upstream sync already
    # treats as authoritative for "what's new". Trade-off accepted: a row
    # inserted later with an OLDER business date than what's already been
    # read (a backdated correction) would be missed by a plain incremental
    # run and would need a --full-refresh to be picked up. Revisit if/when
    # a true monotonic insertion-order column is added to these tables.
    # Values are pre-quoted for SQL use (matching RAW_TABLE's convention).
    WATERMARK_COLUMN = {"Inficare": '"TRN_Date"', "iSendHub": '"DOT(Date_Of_TXN)"'}

    # Inficare's business-facing "Control_No" is NOT reliably unique per
    # transaction -- confirmed against real data: ~264 groups of genuinely
    # distinct transactions (different sender/receiver/amount/time) share
    # one Control_No value, apparently from a float/scientific-notation
    # precision loss somewhere upstream (e.g. "2.025090108e+14").
    # "Transaction_Code" is 100% unique across all 903,851 IPAYDATA rows
    # and is the real per-row identity -- used here as the natural key
    # instead so upsert never silently collapses distinct transactions
    # into one row. Control_No is untouched everywhere else (still the
    # business_reporting column downstream) -- see
    # sql/003_business_report_mv.sql's Source_Row_Key. iSendHub's
    # Tracking_No, by contrast, IS already 100% unique -- unchanged.
    NATURAL_KEY = {"Inficare": "Transaction_Code", "iSendHub": "Tracking_No"}

    UPSERT_PAGE_SIZE = 1000

    def __init__(self, settings: DatabaseSettings, cleaning_config: CleaningConfig):
        self.settings = settings
        self.cleaner = DataCleaner(cleaning_config)
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        """Lazily created via DatabaseSettings.create_engine() -- the one
        place PG_SCHEMA search_path handling lives, shared with
        Codes/run_migrations.py rather than duplicated here."""
        if self._engine is None:
            self._engine = self.settings.create_engine()
        return self._engine

    # ------------------------
    # WATERMARK (ACID: each read/write below is one explicit transaction)
    # ------------------------

    def _get_watermark(self, table_name: str):
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT last_value FROM etl_watermark WHERE table_name = :t"),
                {"t": table_name},
            ).fetchone()
        return row[0] if row else None

    def _set_watermark(self, table_name: str, value) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO etl_watermark (table_name, last_value, updated_at)
                    VALUES (:t, :v, now())
                    ON CONFLICT (table_name)
                    DO UPDATE SET last_value = EXCLUDED.last_value, updated_at = now()
                """),
                {"t": table_name, "v": value},
            )

    # ------------------------
    # READ
    # ------------------------

    def load_new_raw_rows(self, source_name: str, full_refresh: bool = False) -> pd.DataFrame:
        """
        Reads only rows newer than this source's watermark (incremental
        append), or every row when full_refresh=True (e.g. the first-ever
        run, or a deliberate reprocess after a cleaning_config.yml change).
        Returns a plain DataFrame with the raw columns, nothing pre-cleaned.
        """
        raw_table = self.RAW_TABLE[source_name]
        watermark_col = self.WATERMARK_COLUMN[source_name]

        if full_refresh:
            query, params = f"SELECT * FROM {raw_table} ORDER BY {watermark_col}", {}
        else:
            watermark = self._get_watermark(source_name)
            if watermark is None:
                query, params = f"SELECT * FROM {raw_table} ORDER BY {watermark_col}", {}
            else:
                query = (
                    f"SELECT * FROM {raw_table} WHERE {watermark_col} > :wm "
                    f"ORDER BY {watermark_col}"
                )
                params = {"wm": watermark}

        try:
            return pd.read_sql(text(query), self.engine, params=params)
        except SQLAlchemyError:
            logger.exception("[%s] Failed to read raw rows from %s", source_name, raw_table)
            raise

    # ------------------------
    # WRITE (ACID: one committed transaction per call; idempotent via
    # ON CONFLICT, so a crash between this and _set_watermark just means
    # the next run's re-read overlaps harmlessly rather than corrupting data)
    # ------------------------

    def upsert_clean_table(self, df: pd.DataFrame, clean_table: str, natural_key: str) -> int:
        """
        INSERT ... ON CONFLICT (natural_key) DO UPDATE -- idempotent, so a
        watermark read that overlaps the previous run's last row (or a
        --full-refresh over already-clean data) never produces duplicates.

        Batches UPSERT_PAGE_SIZE rows into one multi-row INSERT per round
        trip (psycopg2.extras.execute_values), instead of one round trip
        per row. Real-world latency to a remote Postgres server (confirmed:
        ~260ms/round-trip to this project's RDS instance from a typical
        client network) makes a naive one-row-per-round-trip upsert take
        HOURS over hundreds of thousands of rows -- e.g. ~963K rows at that
        latency would be ~70 hours one-row-at-a-time, vs. minutes batched.
        """
        if df.empty:
            logger.info("[%s] No new/changed rows.", clean_table)
            return 0

        cols = list(df.columns)
        col_list = ", ".join(f'"{c}"' for c in cols)
        update_cols = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != natural_key)
        row_template = "(" + ", ".join(["%s"] * len(cols)) + ", now())"

        upsert_sql = f"""
            INSERT INTO {clean_table} ({col_list}, "_cleaned_at")
            VALUES %s
            ON CONFLICT ("{natural_key}") DO UPDATE SET {update_cols}, "_cleaned_at" = now()
        """

        # .astype(object) BEFORE .where() is required, not cosmetic: pandas
        # 3.x's default "str" column dtype (e.g. Paid_Date after
        # normalize_timezones()'s .dt.strftime() on an all-NaT group, which
        # yields a real float NaN per pandas' own documented quirk)
        # silently refuses to hold `None` via .where(mask, None) and keeps
        # the original NaN in place -- verified directly against this
        # pandas version. Casting to plain object dtype first makes the
        # replacement actually take effect; otherwise NaN reaches
        # execute_values as a literal float and fails with "column ... is
        # of type timestamp without time zone but expression is of type
        # double precision".
        #
        # to_dict() (not .values.tolist()) so numpy/pandas scalars are
        # boxed to native Python types (int/float/str/datetime/None) --
        # psycopg2 has no built-in adapter for numpy.int64/float64, and
        # execute_values talks to it directly (bypassing SQLAlchemy's own
        # DBAPI type coercion layer).
        obj_df = df.astype(object)
        records_by_col = obj_df.where(pd.notnull(obj_df), None).to_dict(orient="records")
        records = [tuple(row[c] for c in cols) for row in records_by_col]

        raw_conn = self.engine.raw_connection()
        try:
            with raw_conn.cursor() as cur:
                execute_values(
                    cur, upsert_sql, records,
                    template=row_template, page_size=self.UPSERT_PAGE_SIZE,
                )
            raw_conn.commit()
        except Exception:
            raw_conn.rollback()
            logger.exception("[%s] Upsert failed -- transaction rolled back.", clean_table)
            raise
        finally:
            raw_conn.close()

        return len(records)

    # Refreshed in this order, each depending on business_report already
    # being current -- see sql/004_dashboard_payload_mvs.sql's header
    # comment for why these exist (the API's dashboard endpoints used to
    # live-aggregate business_report on every request; confirmed too slow
    # to serve interactively, so the same GROUP BY logic now runs once
    # here instead).
    DASHBOARD_PAYLOAD_VIEWS = (
        "mv_dashboard_rows", "mv_dashboard_tat_rows",
        "mv_dashboard_creation_time_rows", "mv_dashboard_status_rows",
    )

    def refresh_business_report(self) -> None:
        """
        CONCURRENTLY avoids locking business_report (and each dashboard
        payload view) against dashboard reads for the duration of the
        refresh -- requires the unique index created alongside each view.
        Cannot run inside the same transaction as other statements
        (Postgres restriction on CONCURRENTLY), hence one engine.begin()
        block per view -- each commits independently before the next
        starts, called only after both raw sources are upserted.
        """
        try:
            with self.engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY business_report"))
            logger.info("business_report materialized view refreshed.")

            for view in self.DASHBOARD_PAYLOAD_VIEWS:
                with self.engine.begin() as conn:
                    conn.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}"))
                logger.info("%s materialized view refreshed.", view)
        except SQLAlchemyError:
            logger.exception("Failed to refresh one or more dashboard materialized views.")
            raise

    # ------------------------
    # ORCHESTRATION
    # ------------------------

    def run_source(self, source_name: str, full_refresh: bool = False) -> None:
        mode = "full refresh" if full_refresh else "incremental"
        logger.info("=== %s: loading new raw rows (%s) ===", source_name, mode)
        raw_df = self.load_new_raw_rows(source_name, full_refresh=full_refresh)
        logger.info("[%s] %d raw row(s) to process.", source_name, len(raw_df))

        if raw_df.empty:
            return

        # WATERMARK_COLUMN's values are pre-quoted for SQL (e.g.
        # '"TRN_Date"', matching RAW_TABLE's convention) -- but here it
        # indexes the raw_df DataFrame, whose column names are the bare,
        # unquoted names as returned by the driver, so the literal quote
        # characters must be stripped first.
        watermark_col = self.WATERMARK_COLUMN[source_name].strip('"')
        new_watermark = raw_df[watermark_col].max()

        cleaned = self.cleaner.clean(raw_df, source_name)
        cleaned = self.cleaner.normalize_timezones(cleaned, source_name)

        clean_table = self.CLEAN_TABLE[source_name]
        n = self.upsert_clean_table(cleaned, clean_table, self.NATURAL_KEY[source_name])
        logger.info("[%s] Upserted %d row(s) into %s.", source_name, n, clean_table)

        if not full_refresh:
            self._set_watermark(source_name, new_watermark)

    def run(self, full_refresh: bool = False) -> None:
        try:
            for source_name in ("Inficare", "iSendHub"):
                self.run_source(source_name, full_refresh=full_refresh)
            self.refresh_business_report()
        except Exception:
            logger.exception("ETL pipeline run failed.")
            raise


def main(full_refresh: bool = False) -> None:
    configure_logging()
    cleaning_config = CleaningConfig.load(os.path.join(PROJECT_ROOT, "cleaning_config.yml"))
    settings = DatabaseSettings.from_env()
    pipeline = PostgresETLPipeline(settings, cleaning_config)
    pipeline.run(full_refresh=full_refresh)


if __name__ == "__main__":
    main(full_refresh="--full-refresh" in sys.argv)
