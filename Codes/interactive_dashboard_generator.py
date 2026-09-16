"""
interactive_dashboard_generator.py (Report Dashboard project) -- builds the
same pre-aggregated payload shapes (rows/tat_rows/creation_time_rows/
status_rows) as the original DATA MIGRATION project, from data read
directly out of Postgres's `business_report` materialized view instead of
a Business_Report.csv (local or S3). Produces a single self-contained
Transaction_Dashboard.html -- identical in structure/behavior to the
original, since interactive_dashboard_template.html itself is copied
unchanged (see this project's README).

Adapted from the original in two ways, both deliberate:

1. ML forecasting removed. The original called
   predictive_model.run_predictive_models() (LightGBM/XGBoost/CatBoost/
   Hybrid Boost/Prophet) here and embedded the result as the `forecast_ml`
   payload key. That module isn't part of this project yet (explicitly out
   of scope for now) -- forecast_ml is a small stub instead. The
   dashboard's Forecast tab's LIVE tier (Naive/Seasonal Naive/SES/
   Holt-Damped -- all client-side JS, no Python ML) is unaffected; only
   the precomputed Reference-tier rows will be empty.

2. Output is always a plain local file (AtomicFileWriter) -- no S3
   destination toggle, since this project has no S3 output at all.
"""

import json
import logging
from pathlib import Path

import pandas as pd

from analytics import AnalyticsEngine
from file_io_utils import AtomicFileWriter

logger = logging.getLogger(__name__)

# ============================================================
# DIMENSION CONFIG -- unchanged from the original project
# ============================================================

DIMENSION_COLUMNS = [
    "Year", "Month", "Day", "Month Name", "Year-Month",
    "Sending_Country", "Receiver_Country", "Corridors",
    "Payment_Type", "Agent_Name",
    "Transaction_Method", "Sending_Country_Currency", "Payout_Currency",
    "Currencies_Pair", "Volume_Bracket", "Agent_Segment",
]

NORMALIZED_COLUMNS = [
    "Sending_Country", "Receiver_Country", "Payment_Type", "transstatus",
    "Agent_Name", "Transaction_Method", "Sending_Country_Currency", "Payout_Currency",
]

PAYMENT_STATUS_VALUE = "PAYMENT"

# Same Year/Month/Year-Month clock every other payload groups by -- TAT
# Analysis buckets transactions the same way every other tab does; only
# the duration metrics themselves (Turn_Around_Time_Hours, TAT_Bucket,
# already computed in business_report from TRN_Date_Normalized -- see
# sql/003_business_report_mv.sql's header comment) use the normalized
# clock, not the Year/Month grouping.
TAT_DIMENSION_COLUMNS = [
    "Year", "Month", "Year-Month",
    "Receiver_Country", "Agent_Name", "Transaction_Method", "Payment_Type",
    "TAT_Bucket",
]

CREATION_TIME_DIMENSION_COLUMNS = [
    "Year", "Month", "Day", "Hour",
    "Sending_Country", "Receiver_Country", "Payment_Type",
    "Agent_Name", "Transaction_Method",
]

STATUS_DIMENSION_COLUMNS = [
    "Year", "Month", "Year-Month",
    "Sending_Country", "Receiver_Country", "Payment_Type", "Agent_Name",
    "Transaction_Method", "Sending_Country_Currency", "Payout_Currency",
    "transstatus", "Volume_Bracket", "Agent_Segment",
]

# Placeholder shape for the (currently unbuilt) ML Reference tier -- fields
# mirror the original project's forecast_ml keys so template code checking
# for their presence doesn't hit a raw KeyError; their VALUES are just
# empty, not a real forecast.
FORECAST_ML_STUB = {
    "forecast_status": "disabled",
    "model_comparison": {},
    "best_model": {},
    "forecast": {},
    "recommended_kpis": {},
    "agent_corridor_forecast": {},
    "country_transaction_method_forecast": {},
    "volume_liquidity_forecast": {},
    "volume_by_payout_partner": {},
    "validation_results": {},
    "warnings": [
        "ML forecasting is not included in this project yet -- Reference-tier rows are empty.",
    ],
    "limitations": [],
}


class DashboardGenerator:
    """
    Builds the single consolidated static dashboard HTML from
    business_report -- either an in-memory DataFrame already run through
    AnalyticsEngine.prepare_analytics_data(), or (via
    from_postgres()/generate_from_postgres()) straight from the live view.
    """

    def __init__(
        self, analytics: AnalyticsEngine | None = None, writer: AtomicFileWriter | None = None,
    ):
        self.analytics = analytics or AnalyticsEngine()
        self.writer = writer or AtomicFileWriter()

    @staticmethod
    def _normalize_common_columns(analytics_df: pd.DataFrame) -> pd.DataFrame:
        df = analytics_df.copy()

        df["Year"] = df["Year"].fillna(1970).astype(int)
        df["Month"] = df["Month"].fillna(1).astype(int)
        df["Day"] = df["Day"].fillna(1).astype(int)
        df["Hour"] = df["Hour"].fillna(0).astype(int)
        df["Month Name"] = df["Month Name"].fillna("Unknown")
        df["Year-Month"] = df["Year-Month"].replace("NaT", "Unknown")

        for col in NORMALIZED_COLUMNS:
            df[col] = df[col].fillna("UNKNOWN").astype(str).str.strip().str.upper()

        return df

    @staticmethod
    def _build_payload(df: pd.DataFrame) -> list:
        logger.info("Grouping and aggregating metrics...")
        grouped = (
            df.groupby(DIMENSION_COLUMNS, observed=True)
            .agg(
                Volume=("Transaction_Amount_USD", "sum"),
                Transactions=("Transaction_Amount_USD", "size"),
            )
            .reset_index()
        )
        grouped["Volume_Bracket"] = grouped["Volume_Bracket"].astype(str)
        rows = grouped.to_dict(orient="records")
        logger.info("Generated %d pre-aggregated dimension groups.", len(rows))
        return rows

    @staticmethod
    def _build_tat_payload(df: pd.DataFrame) -> list:
        logger.info("Grouping and aggregating TAT metrics...")
        grouped = (
            df.groupby(TAT_DIMENSION_COLUMNS, observed=True)
            .agg(
                TAT_Sum=("Turn_Around_Time_Hours", "sum"),
                TAT_Count=("Turn_Around_Time_Hours", "count"),
                Transactions=("Turn_Around_Time_Hours", "size"),
                Volume=("Transaction_Amount_USD", "sum"),
            )
            .reset_index()
        )
        rows = grouped.to_dict(orient="records")
        logger.info("Generated %d pre-aggregated TAT dimension groups.", len(rows))
        return rows

    @staticmethod
    def _build_creation_time_payload(df: pd.DataFrame) -> list:
        logger.info("Grouping and aggregating creation-time metrics...")
        grouped = (
            df.groupby(CREATION_TIME_DIMENSION_COLUMNS, observed=True)
            .agg(
                Transactions=("Turn_Around_Time_Hours", "size"),
                Volume=("Transaction_Amount_USD", "sum"),
            )
            .reset_index()
        )
        rows = grouped.to_dict(orient="records")
        logger.info("Generated %d pre-aggregated creation-time dimension groups.", len(rows))
        return rows

    @staticmethod
    def _build_status_payload(df_unfiltered: pd.DataFrame) -> list:
        logger.info("Grouping and aggregating status-breakdown metrics (all statuses)...")
        grouped = (
            df_unfiltered.groupby(STATUS_DIMENSION_COLUMNS, observed=True)
            .agg(
                Volume=("Transaction_Amount_USD", "sum"),
                Transactions=("Transaction_Amount_USD", "size"),
            )
            .reset_index()
        )
        grouped["Volume_Bracket"] = grouped["Volume_Bracket"].astype(str)
        rows = grouped.to_dict(orient="records")
        logger.info("Generated %d pre-aggregated status dimension groups.", len(rows))
        return rows

    def build_payload(self, analytics_df: pd.DataFrame) -> dict:
        df = self._normalize_common_columns(analytics_df)
        df_payment = df[df["transstatus"] == PAYMENT_STATUS_VALUE].copy()

        tat_numeric = pd.to_numeric(df_payment["Turn_Around_Time_Hours"], errors="coerce")
        df_tat = df_payment[tat_numeric.notna() & (tat_numeric >= 0)].copy()

        return {
            "meta": {
                "gcc_countries": self.analytics.GCC_COUNTRIES,
                "bracket_order": self.analytics.BRACKET_ORDER,
                "tat_bucket_order": self.analytics.TAT_BUCKET_ORDER,
                "agent_segment_order": self.analytics.AGENT_SEGMENT_ORDER,
                "payment_status_value": PAYMENT_STATUS_VALUE,
            },
            "rows": self._build_payload(df_payment),
            "tat_rows": self._build_tat_payload(df_tat),
            "creation_time_rows": self._build_creation_time_payload(df_payment),
            "status_rows": self._build_status_payload(df),
            "forecast_ml": FORECAST_ML_STUB,
        }

    def generate(self, analytics_df: pd.DataFrame, template_path, output_dir,
                 file_name: str = "Transaction_Dashboard.html") -> str:
        """
        Build the single consolidated dashboard from an in-memory
        analytics_df (the output of AnalyticsEngine.prepare_analytics_data())
        and write it to a plain local `output_dir`.
        """
        payload = self.build_payload(analytics_df)

        logger.info("Reading HTML template...")
        with open(template_path, "r", encoding="utf-8") as f:
            template_html = f.read()

        logger.info("Injecting data payload (one script block per section)...")
        total_bytes = 0
        data_blocks = []
        for key, value in payload.items():
            section_json = json.dumps(value).replace("</script", "<\\/script")
            total_bytes += len(section_json)
            logger.info("  dashboard-data-%s: %d bytes", key, len(section_json))
            data_blocks.append(
                f'<script id="dashboard-data-{key}" type="application/json">{section_json}</script>'
            )
        logger.info("Total payload size: %d bytes", total_bytes)

        output_html = template_html.replace(
            "DASHBOARD_DATA_BLOCKS_PLACEHOLDER", "\n".join(data_blocks),
        )
        written_to = self.writer.write_text(output_html, output_dir, file_name)

        logger.info("Interactive dashboard compiled successfully!")
        return written_to

    def generate_from_postgres(self, engine, template_path, output_dir) -> str:
        """
        Reads the FULL business_report materialized view straight out of
        Postgres (see sql/003_business_report_mv.sql) and generates the
        dashboard from it.
        """
        logger.info("Loading business_report from Postgres...")
        df = pd.read_sql("SELECT * FROM business_report", engine)
        logger.info("Loaded %d row(s) from business_report.", len(df))

        analytics_df = self.analytics.prepare_analytics_data(df)
        return self.generate(analytics_df, template_path, output_dir)


if __name__ == "__main__":
    import sys

    from logging_config import configure_logging

    configure_logging()

    sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))
    from db import get_engine  # noqa: E402

    project_root = Path(__file__).resolve().parent.parent
    template = Path(__file__).resolve().parent / "interactive_dashboard_template.html"
    output_dir = project_root / "Dashboard"

    DashboardGenerator().generate_from_postgres(get_engine(), template, output_dir)
