# ============================================================
# ANALYTICS.PY
# Analytics layer: date/dimension derivation and all
# create_*_analysis summaries built on top of the unified
# business report produced by cleaning_functions.py.
# ============================================================

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ------------------------
# SHARED CONSTANTS
# ------------------------

VOLUME_BRACKET_BINS = [0, 500, 2500, 5000, 10000, 25000, np.inf]
VOLUME_BRACKET_LABELS = ["0-500", "500-2500", "2500-5000", "5000-10000", "10000-25000", "25000+"]
# Rows whose Transaction_Amount_USD could not be computed (e.g. a
# divide-by-zero-guarded exchange rate) get an explicit "Unknown" bucket
# instead of pd.cut()'s NaN, so groupby(observed=False) no longer silently
# drops them from create_volume_bracket_analysis.
BRACKET_ORDER = VOLUME_BRACKET_LABELS + ["Unknown"]

GCC_COUNTRIES = [
    "UNITED ARAB EMIRATES",
    "SAUDI ARABIA",
    "QATAR",
    "KUWAIT",
    "BAHRAIN",
    "OMAN",
]

# Turn_Around_Time_Hours can be null (not yet paid) or negative (see the
# Bhutan National Bank / PaasPay finding -- TRN_Date appears to reflect a
# later batch/settlement moment for that integration, under
# investigation). Both cases get their own explicit bucket rather than
# being silently dropped or lumped in with a real duration, mirroring the
# Volume_Bracket "Unknown" pattern above.
TAT_BUCKET_LABELS = ["0-1h", "1-6h", "6-24h", "1-3 Days", "3-7 Days", "7+ Days"]
TAT_BUCKET_ORDER = ["Negative (Data Flag)"] + TAT_BUCKET_LABELS + ["Not Yet Paid"]

# Business-defined agent segmentation (Segmentation Overview tab). Matched
# case/whitespace-insensitively against Agent_Name, mirroring the
# case-insensitive apply_mapping() pattern in cleaning_functions.py -- the
# named lists below are the "canonical" spellings, but real Agent_Name
# values may vary in casing.
_AGENT_SEGMENT_GROUPS = {
    "PaaSPay": [
        "Bhutan National Bank (PaasPay)",
        "Chariot Travels Private Limited",
        "SPEED REMIT (PAASPAY - AUD)",
        "SPEED REMIT (PAASPAY - SGD)",
    ],
    "iSend Biz": [
        "HAND MONEY PAYMENTS, S.A. de C.V.",
        "iSend BIZ SGP",
        "KiraFin"
    ],
    "iSend C2C": [
        "iSend App USA",
        "iSend AUS (Apps)",
        "ISEND PTE LTD (APPs)",
        "iSend Pte Ltd.",
        "ISEND PTE LTD-FREE FEE",
        "ISEND PTE LTD-HO",
        "ISEND PTE. LTD. (USD)",
        "ISEND_AUS_APPS (ISEND GLOBAL)",
        "Transcash International Pty Ltd",
        "TRANSCASH INTERNATIONAL PTY LTD (HO)",
        "Transcash International Pty. Ltd. (app)",
    ],
}
AGENT_SEGMENT_MAP = {
    name.strip().upper(): segment
    for segment, names in _AGENT_SEGMENT_GROUPS.items()
    for name in names
}
DEFAULT_AGENT_SEGMENT = "Last Mile Settlement"
AGENT_SEGMENT_ORDER = list(_AGENT_SEGMENT_GROUPS.keys()) + [DEFAULT_AGENT_SEGMENT]


class AnalyticsEngine:
    """
    Dimension derivation (prepare_analytics_data) and every create_*_analysis
    summary built on top of the unified business report. Stateless (every
    method takes the DataFrame it operates on and returns a new one) --
    grouped into a class because it's one cohesive unit callers depend on
    together (Codes/interactive_dashboard_generator.py), not because these
    transforms hold any instance state of their own.
    """

    VOLUME_BRACKET_BINS = VOLUME_BRACKET_BINS
    VOLUME_BRACKET_LABELS = VOLUME_BRACKET_LABELS
    BRACKET_ORDER = BRACKET_ORDER
    GCC_COUNTRIES = GCC_COUNTRIES
    TAT_BUCKET_LABELS = TAT_BUCKET_LABELS
    TAT_BUCKET_ORDER = TAT_BUCKET_ORDER
    AGENT_SEGMENT_MAP = AGENT_SEGMENT_MAP
    DEFAULT_AGENT_SEGMENT = DEFAULT_AGENT_SEGMENT
    AGENT_SEGMENT_ORDER = AGENT_SEGMENT_ORDER

    @staticmethod
    def compute_agent_segment(agent_name_series: pd.Series) -> pd.Series:
        keys = agent_name_series.astype("string").str.strip().str.upper()
        return keys.map(AGENT_SEGMENT_MAP).fillna(DEFAULT_AGENT_SEGMENT)

    # ------------------------
    # ANALYTICS DATA PREP
    # ------------------------

    def prepare_analytics_data(self, report_df: pd.DataFrame) -> pd.DataFrame:
        """Prepare analytics data for reporting."""
        df = report_df.copy()

        # Date Dimension -- TRN_Date here is each source's transaction date
        # exactly as recorded (Inficare unchanged; iSendHub's DOT(Date_Of_TXN)
        # also raw, no longer converted to Asia/Kuala_Lumpur at ETL clean
        # time -- see cleaning_config.yml and sql/003_business_report_mv.sql's
        # header comment for why: that conversion was pushing late-day-of-
        # month iSendHub transactions into the next calendar day/month,
        # disagreeing with the Accounts department's own MIS). Every
        # Year/Month/Day/Hour dimension below -- used throughout the
        # dashboard, TAT payload included -- is built from this raw clock;
        # only Turn_Around_Time_Hours/TAT_Bucket themselves (already computed
        # in business_report from TRN_Date_Normalized -- see that file's
        # header comment) use the normalized one, not the Year/Month grouping.
        df["TRN_Date"] = pd.to_datetime(df["TRN_Date"], errors="coerce")
        df["Year"] = df["TRN_Date"].dt.year
        df["Quarter"] = df["TRN_Date"].dt.quarter
        df["Month"] = df["TRN_Date"].dt.month
        df["Month Name"] = df["TRN_Date"].dt.strftime("%B")
        df["Year-Month"] = df["TRN_Date"].dt.to_period("M").astype(str)
        df["Day"] = df["TRN_Date"].dt.day
        df["Hour"] = df["TRN_Date"].dt.hour

        # CORRIDORS -- fillna("UNKNOWN") before concatenation so rows with a
        # missing Sending_Country/Receiver_Country still get grouped into a
        # visible "UNKNOWN" bucket instead of silently producing NaN and
        # disappearing from every downstream groupby().
        sending_country = df["Sending_Country"].fillna("UNKNOWN")
        receiver_country = df["Receiver_Country"].fillna("UNKNOWN")
        df["Corridors"] = sending_country + " - " + receiver_country

        # CURRENCY PAIRS
        sending_currency = df["Sending_Country_Currency"].fillna("UNKNOWN")
        payout_currency = df["Payout_Currency"].fillna("UNKNOWN")
        df["Currencies_Pair"] = sending_currency + " - " + payout_currency

        # VOLUME BRACKETS
        df["Volume_Bracket"] = pd.cut(
            df["Transaction_Amount_USD"],
            bins=self.VOLUME_BRACKET_BINS,
            labels=self.VOLUME_BRACKET_LABELS,
            include_lowest=True,
        )
        # Give NaN amounts (divide-by-zero-guarded rows) an explicit
        # "Unknown" bracket so they still reconcile with overall totals
        # instead of vanishing from create_volume_bracket_analysis.
        df["Volume_Bracket"] = (
            df["Volume_Bracket"].cat.add_categories(["Unknown"]).fillna("Unknown")
        )

        # TAT BUCKET -- conditions are checked in order and the first match
        # wins, so a null TAT is always "Not Yet Paid" regardless of any
        # numeric comparison (comparing NaN < 0 etc. is well-defined in
        # pandas -- it's just False, not an error -- but checking isna()
        # first keeps the intent explicit).
        tat = pd.to_numeric(df["Turn_Around_Time_Hours"], errors="coerce")
        df["TAT_Bucket"] = np.select(
            condlist=[tat.isna(), tat < 0, tat <= 1, tat <= 6, tat <= 24, tat <= 72, tat <= 168],
            choicelist=["Not Yet Paid", "Negative (Data Flag)"] + self.TAT_BUCKET_LABELS[:-1],
            default=self.TAT_BUCKET_LABELS[-1],
        )

        # AGENT SEGMENT (Segmentation Overview tab)
        df["Agent_Segment"] = self.compute_agent_segment(df["Agent_Name"])

        return df

    # ------------------------
    # METRICS
    # ------------------------

    @staticmethod
    def calculate_metrics(df: pd.DataFrame) -> pd.Series:
        """Calculate metrics for reporting."""
        transactions = len(df)
        volume = pd.to_numeric(df["Transaction_Amount_USD"], errors="coerce").sum()
        ticket_size = (volume / transactions) if transactions > 0 else 0
        return pd.Series({
            "Transactions": transactions, "Volume": volume, "Ticket_Size": round(ticket_size, 2),
        })

    # ------------------------
    # SHARED GROUP-SUMMARY HELPER
    # ------------------------

    @staticmethod
    def _summarize(df, group_cols, *, add_percentages=False, sort_by_volume=True, observed=True):
        """
        Vectorized replacement for the old groupby(...).apply(calculate_metrics)
        pattern. Uses "size" (not "count") for Transactions so rows with a NaN
        Transaction_Amount_USD are still counted, matching calculate_metrics()'s
        len(df) semantics; Volume still just sums the numeric column, so NaN
        amounts contribute 0 to Volume as before.
        """
        grouped = (
            df.groupby(group_cols, observed=observed)
            .agg(
                Transactions=("Transaction_Amount_USD", "size"),
                Volume=("Transaction_Amount_USD", "sum"),
            )
            .reset_index()
        )
        grouped["Ticket_Size"] = (grouped["Volume"] / grouped["Transactions"]).round(2)

        if add_percentages:
            total_transactions = grouped["Transactions"].sum()
            total_volume = grouped["Volume"].sum()
            grouped["Transaction_%"] = (grouped["Transactions"] / total_transactions * 100).round(2)
            grouped["Volume_%"] = (grouped["Volume"] / total_volume * 100).round(2)

        if sort_by_volume:
            grouped = grouped.sort_values("Volume", ascending=False)

        return grouped.reset_index(drop=True)

    # ------------------------
    # DIMENSION SUMMARIES
    # ------------------------

    def create_monthly_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        """Create monthly trend analysis with variance metrics."""
        group_cols = ["Year-Month", "Year", "Month", "Month Name"]
        monthly = self._summarize(df, group_cols, sort_by_volume=False)
        monthly = monthly.sort_values(["Year", "Month"])

        monthly["Transaction_Variance_%"] = (monthly["Transactions"].pct_change() * 100).round(2)
        monthly["Volume_Variance_%"] = (monthly["Volume"].pct_change() * 100).round(2)
        monthly["Ticket_Size_Variance_%"] = (monthly["Ticket_Size"].pct_change() * 100).round(2)

        return monthly[[
            "Year-Month", "Year", "Month", "Month Name",
            "Transactions", "Transaction_Variance_%",
            "Volume", "Volume_Variance_%",
            "Ticket_Size", "Ticket_Size_Variance_%",
        ]]

    def create_country_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Sending_Country", add_percentages=True)

    def create_receiver_country_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Receiver_Country", add_percentages=True)

    def create_partner_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Agent_Name")

    def create_payout_partner_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Transaction_Method")

    def create_corridor_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Corridors")

    def create_status_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        # NOTE: preserved from the original -- this one is intentionally NOT
        # sorted by Volume (returns in groupby key order).
        return self._summarize(df, "transstatus", add_percentages=True, sort_by_volume=False)

    def create_currency_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Currencies_Pair")

    def create_volume_bracket_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._summarize(df, "Volume_Bracket", sort_by_volume=False, observed=False)

    @staticmethod
    def create_top_bottom_analysis(summary_df: pd.DataFrame, n: int = 10):
        """
        Slice top-N / bottom-N rows by Volume from an ALREADY-SUMMARIZED
        DataFrame (e.g. the output of create_country_analysis). This performs
        no groupby of its own -- callers must pass a pre-aggregated frame,
        avoiding a redundant full-dataset regroup.
        """
        top_n = summary_df.sort_values("Volume", ascending=False).head(n).reset_index(drop=True)
        bottom_n = summary_df.sort_values("Volume", ascending=True).head(n).reset_index(drop=True)
        return top_n, bottom_n

    def create_gcc_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        return df[df["Sending_Country"].isin(self.GCC_COUNTRIES)].copy()

    def create_gcc_monthly_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        """Monthly Volume/Transactions for GCC-origin transactions."""
        gcc_df = self.create_gcc_analysis(df)
        monthly = self._summarize(gcc_df, "Year-Month", sort_by_volume=False)
        return monthly.sort_values("Year-Month").reset_index(drop=True)

    def create_gcc_corridor_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        """Full ranked GCC corridor summary (Transactions/Volume/Ticket_Size), sorted by Volume."""
        gcc_df = self.create_gcc_analysis(df)
        return self._summarize(gcc_df, "Corridors")
