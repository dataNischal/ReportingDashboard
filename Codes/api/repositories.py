"""
repositories.py -- data-access layer for the dashboard's aggregated data
endpoints, separated out from routers/dashboard.py's route wiring.

Each endpoint queries one of the pre-aggregated materialized views in
sql/004_dashboard_payload_mvs.sql (mv_dashboard_rows / _tat_rows /
_creation_time_rows / _status_rows) instead of live-aggregating
business_report on every request -- confirmed directly against the live
isendstaging data that the live-aggregation approach this module used to
take was too slow to serve interactively (30-40+ seconds for the
unfiltered "rows" query alone; see that SQL file's header comment for the
full story). The GROUP BY / casing-normalization logic that used to live
here at query time now lives in those materialized views instead, computed
once per ETL run (Codes/postgres_pipeline.py's PostgresETLPipeline).
"""

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Query-param name -> business_report/pre-aggregated-view column. Every one
# of the dashboard's original 9 dropdown filters. NOT every column applies
# to every payload shape -- each get_*_rows() method below passes its own
# APPLICABLE_* subset to build_where(), matching exactly which dimensions
# interactive_dashboard_generator.py's DIMENSION_COLUMNS/TAT_DIMENSION_
# COLUMNS/CREATION_TIME_DIMENSION_COLUMNS/STATUS_DIMENSION_COLUMNS (and the
# dashboard template's own documented client-side filter contract) already
# treat as meaningful for that shape -- e.g. Corridors was never a TAT-tab
# dimension, so a Corridors filter has always been a no-op there, on the
# static-file path AND the client-side-filtered path alike; this preserves
# that exact behavior rather than silently changing it.
FILTERABLE_COLUMNS = {
    "sending_country": "Sending_Country",
    "receiver_country": "Receiver_Country",
    "corridors": "Corridors",
    "payment_type": "Payment_Type",
    "transstatus": "transstatus",
    "agent_name": "Agent_Name",
    "transaction_method": "Transaction_Method",
    "sending_country_currency": "Sending_Country_Currency",
    "payout_currency": "Payout_Currency",
    "year": "Year",
    "month": "Month",
    "day": "Day",
}

# Per-payload-shape applicable filter keys -- subsets of FILTERABLE_COLUMNS
# matching each materialized view's actual dimension set (see
# sql/004_dashboard_payload_mvs.sql).
APPLICABLE_ROWS_FILTERS = {
    "sending_country", "receiver_country", "corridors", "payment_type", "agent_name",
    "transaction_method", "sending_country_currency", "payout_currency", "year", "month", "day",
}
APPLICABLE_TAT_ROWS_FILTERS = {
    "receiver_country", "agent_name", "transaction_method", "payment_type", "year", "month",
}
APPLICABLE_CREATION_TIME_ROWS_FILTERS = {
    "sending_country", "receiver_country", "payment_type", "agent_name", "transaction_method",
    "year", "month", "day",
}
APPLICABLE_STATUS_ROWS_FILTERS = {
    "sending_country", "receiver_country", "payment_type", "agent_name", "transaction_method",
    "sending_country_currency", "payout_currency", "transstatus", "year", "month",
}

# Columns normalized (upper(trim(...))) inside the materialized views
# themselves -- filter VALUES for these need the same normalization
# applied in Python before binding, since a caller's filter value isn't
# guaranteed to already be uppercase (the dashboard's own dropdowns are,
# since interactive_dashboard_template.html reads them from these same
# normalized payloads, but this doesn't assume that).
NORMALIZED_TEXT_COLUMNS = {
    "Sending_Country", "Receiver_Country", "Payment_Type", "transstatus",
    "Agent_Name", "Transaction_Method", "Sending_Country_Currency", "Payout_Currency",
}


class DashboardFilters:
    """Plain parameter bag for the shared filter set -- constructed by
    routers/dashboard.py's FastAPI Query() dependency, consumed here."""

    def __init__(
        self,
        sending_country: Optional[List[str]] = None,
        receiver_country: Optional[List[str]] = None,
        corridors: Optional[List[str]] = None,
        payment_type: Optional[List[str]] = None,
        transstatus: Optional[List[str]] = None,
        agent_name: Optional[List[str]] = None,
        transaction_method: Optional[List[str]] = None,
        sending_country_currency: Optional[List[str]] = None,
        payout_currency: Optional[List[str]] = None,
        year: Optional[List[int]] = None,
        month: Optional[List[int]] = None,
        day: Optional[List[int]] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
    ):
        self.values = {
            "sending_country": sending_country, "receiver_country": receiver_country,
            "corridors": corridors, "payment_type": payment_type, "transstatus": transstatus,
            "agent_name": agent_name, "transaction_method": transaction_method,
            "sending_country_currency": sending_country_currency,
            "payout_currency": payout_currency,
            "year": year, "month": month, "day": day,
        }
        self.date_start = date_start
        self.date_end = date_end

    def build_where(self, extra_conditions, applicable_keys=None):
        """
        Builds a parameterized WHERE clause. List-valued filters bind as
        Postgres arrays for `column = ANY(:param)` (asyncpg adapts a Python
        list to an ARRAY automatically) -- never string-formatted into the
        query, so this stays injection-safe regardless of filter content.

        applicable_keys restricts which FILTERABLE_COLUMNS keys are even
        considered -- a filter for a dimension the target view doesn't
        carry (e.g. corridors against mv_dashboard_tat_rows) is silently
        skipped rather than referencing a nonexistent column, matching
        this dashboard's long-documented "not every filter narrows every
        payload shape" contract. Defaults to every key (used only for
        ad-hoc queries against the full business_report table, not the
        pre-aggregated views below).

        For NORMALIZED_TEXT_COLUMNS, incoming filter values are uppercased
        in Python before binding, matching the same normalization already
        baked into the materialized views themselves.
        """
        conditions = list(extra_conditions)
        params = {}
        keys = FILTERABLE_COLUMNS.keys() if applicable_keys is None else applicable_keys

        for key in keys:
            column = FILTERABLE_COLUMNS[key]
            values = self.values.get(key)
            if values:
                bind_name = f"f_{key}"
                quoted = f'"{column}"'
                if column in NORMALIZED_TEXT_COLUMNS:
                    params[bind_name] = [str(v).strip().upper() for v in values]
                else:
                    params[bind_name] = list(values)
                conditions.append(f'{quoted} = ANY(:{bind_name})')

        # mv_dashboard_tat_rows and mv_dashboard_status_rows carry no "Day"
        # column at all (see sql/004_dashboard_payload_mvs.sql) -- COALESCE
        # can only substitute a NULL, not a column that doesn't exist, so
        # referencing "Day" unconditionally here would raise a Postgres
        # undefined-column error against those two views. "day" being
        # absent from applicable_keys already signals exactly this (see
        # APPLICABLE_TAT_ROWS_FILTERS/APPLICABLE_STATUS_ROWS_FILTERS
        # above), so it's reused here rather than adding a second flag
        # that could drift out of sync with it. Confirmed previously
        # dormant: nothing called build_where() with date_start/date_end
        # set against these two views until interactive_dashboard_
        # template.html's default-recent-window load started doing so.
        has_day_column = "day" in keys
        if self.date_start:
            if has_day_column:
                conditions.append(
                    '"Year" * 10000 + "Month" * 100 + COALESCE("Day", 1) >= :date_start_key'
                )
                params["date_start_key"] = _date_key(self.date_start)
            else:
                conditions.append('"Year" * 100 + "Month" >= :date_start_key')
                params["date_start_key"] = _year_month_key(self.date_start)
        if self.date_end:
            if has_day_column:
                conditions.append(
                    '"Year" * 10000 + "Month" * 100 + COALESCE("Day", 28) <= :date_end_key'
                )
                params["date_end_key"] = _date_key(self.date_end)
            else:
                conditions.append('"Year" * 100 + "Month" <= :date_end_key')
                params["date_end_key"] = _year_month_key(self.date_end)

        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        return where_clause, params


def _date_key(date_str: str) -> int:
    """'YYYY-MM-DD' -> YYYYMMDD int, for a view that carries a "Day"
    column (mv_dashboard_rows, mv_dashboard_creation_time_rows)."""
    year, month, day = (int(p) for p in date_str.split("-")[:3])
    return year * 10000 + month * 100 + day


def _year_month_key(date_str: str) -> int:
    """'YYYY-MM-DD' -> YYYYMM int (day discarded), for a view with no
    "Day" column at all (mv_dashboard_tat_rows, mv_dashboard_status_rows)
    -- month-level granularity is the best a date range can do there,
    mirroring the dashboard template's own rowMatchesDateRange()
    month-overlap degradation client-side for the same two payloads."""
    year, month = (int(p) for p in date_str.split("-")[:2])
    return year * 100 + month


class DashboardReportRepository:
    """Async data access against the pre-aggregated dashboard materialized
    views (sql/004_dashboard_payload_mvs.sql) plus business_report itself
    for filter_options/admin operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _run(self, query: str, params: dict) -> List[dict]:
        try:
            result = await self.session.execute(text(query), params)
            return [dict(row._mapping) for row in result]
        except Exception:
            logger.exception("Dashboard query failed: %s", query.strip().splitlines()[0])
            raise

    async def get_rows(self, filters: DashboardFilters) -> List[dict]:
        """Mirrors the `rows` payload: Payment-only, 16-dimension set --
        now a plain filtered SELECT against mv_dashboard_rows, no
        aggregation at request time."""
        where_clause, params = filters.build_where([], APPLICABLE_ROWS_FILTERS)
        return await self._run(f"SELECT * FROM mv_dashboard_rows WHERE {where_clause}", params)

    async def get_tat_rows(self, filters: DashboardFilters) -> List[dict]:
        """Mirrors the `tat_rows` payload (Payment-only, TAT-anomaly-free
        -- both already baked into mv_dashboard_tat_rows's definition)."""
        where_clause, params = filters.build_where([], APPLICABLE_TAT_ROWS_FILTERS)
        return await self._run(f"SELECT * FROM mv_dashboard_tat_rows WHERE {where_clause}", params)

    async def get_creation_time_rows(self, filters: DashboardFilters) -> List[dict]:
        """Mirrors the `creation_time_rows` payload (Payment-only, hour-of-day)."""
        where_clause, params = filters.build_where([], APPLICABLE_CREATION_TIME_ROWS_FILTERS)
        query = f"SELECT * FROM mv_dashboard_creation_time_rows WHERE {where_clause}"
        return await self._run(query, params)

    async def get_status_rows(self, filters: DashboardFilters) -> List[dict]:
        """Mirrors the `status_rows` payload -- UNFILTERED by status (every
        transstatus value, already reflected in mv_dashboard_status_rows)."""
        where_clause, params = filters.build_where([], APPLICABLE_STATUS_ROWS_FILTERS)
        query = f"SELECT * FROM mv_dashboard_status_rows WHERE {where_clause}"
        return await self._run(query, params)

    async def get_filter_options(self) -> dict:
        """
        Populates the 9 dropdown filters -- 12 cheap DISTINCT scans against
        the small pre-aggregated views instead of business_report itself.
        sending_country/receiver_country/corridors/payment_type/agent_name/
        transaction_method/sending_country_currency/payout_currency/year/
        month/day are scanned from mv_dashboard_rows (Payment-only),
        matching interactive_dashboard_template.html's populateDropdowns()
        -- which has always derived these from rawData (also Payment-only),
        not the unfiltered dataset. transstatus is scanned from
        mv_dashboard_status_rows instead, matching that same function's
        documented exception ("transstatus isn't shipped in rawData ... so
        the Status dropdown is populated from statusRows instead").
        """
        source_view = {
            "transstatus": "mv_dashboard_status_rows",
        }
        options = {}
        for key, column in FILTERABLE_COLUMNS.items():
            view = source_view.get(key, "mv_dashboard_rows")
            quoted = f'"{column}"'
            result = await self.session.execute(
                text(f'SELECT DISTINCT {quoted} FROM {view} WHERE {quoted} IS NOT NULL ORDER BY 1')
            )
            options[key] = [row[0] for row in result]
        return options

    async def get_forecast(self) -> dict:
        """
        Serves the precomputed Reference-tier forecast blob from
        forecast_cache -- a cheap row lookup, never a live retrain per
        request. See the migration plan's Phase 3 on how/when this table
        gets (re)populated by predictive_model.run_predictive_models().
        """
        result = await self.session.execute(
            text("SELECT payload, generated_at FROM forecast_cache WHERE id = 1")
        )
        row = result.first()
        if row is None:
            return {"forecast_status": "not_yet_generated"}
        return {**row.payload, "generated_at": row.generated_at.isoformat()}

    async def refresh_materialized_view(self) -> None:
        """Manual on-demand refresh, for ops convenience -- the primary
        refresh path is still Codes/postgres_pipeline.py running after
        every ETL append. Refreshes business_report AND the 4 dependent
        dashboard payload views, in order (each depends on business_report
        already being current). Each REFRESH gets its own committed
        transaction via an explicit commit() right after -- Postgres
        rejects CONCURRENTLY if it's not the only statement in its
        transaction block, so five of these back to back on one session
        (which auto-begins one open transaction and keeps it until
        committed) would otherwise fail on the second one."""
        await self.session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY business_report"))
        await self.session.commit()
        for view in (
            "mv_dashboard_rows", "mv_dashboard_tat_rows",
            "mv_dashboard_creation_time_rows", "mv_dashboard_status_rows",
        ):
            await self.session.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}"))
            await self.session.commit()
