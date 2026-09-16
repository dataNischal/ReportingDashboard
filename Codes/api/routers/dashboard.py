"""
routers/dashboard.py -- the data endpoints replacing the old dashboard's
embedded `rows`/`tat_rows`/`creation_time_rows`/`status_rows`/`forecast_ml`
JSON blobs (previously ~180MB, computed once and shipped whole to every
viewer). Each endpoint's aggregation logic lives in repositories.py
(DashboardReportRepository); this module just wires FastAPI request
parsing -> repository call -> typed response.

Every endpoint is protected by get_current_user -- no dashboard data is
served to an unauthenticated request. Every response declares a
response_model so the auto-generated OpenAPI schema documents actual
response shapes instead of an untyped blob (see /docs, /openapi.json).
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from db import Database
from deps import get_current_user, get_database, require_admin
from repositories import DashboardFilters, DashboardReportRepository

# analytics.py lives in Codes/, not Codes/api/routers/ -- main.py already
# appends Codes/ to sys.path before importing this module, but that's
# insurance for the (rare) case this module gets imported directly without
# going through main.py first (e.g. an ad-hoc script), mirroring main.py's
# own path-setup comment. dirname x3: routers/ -> api/ -> Codes/.
_codes_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _codes_dir not in sys.path:
    sys.path.append(_codes_dir)
from analytics import AnalyticsEngine  # noqa: E402

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _filters_dependency(
    sending_country: Optional[List[str]] = Query(None),
    receiver_country: Optional[List[str]] = Query(None),
    corridors: Optional[List[str]] = Query(None),
    payment_type: Optional[List[str]] = Query(None),
    transstatus: Optional[List[str]] = Query(None),
    agent_name: Optional[List[str]] = Query(None),
    transaction_method: Optional[List[str]] = Query(None),
    sending_country_currency: Optional[List[str]] = Query(None),
    payout_currency: Optional[List[str]] = Query(None),
    year: Optional[List[int]] = Query(None),
    month: Optional[List[int]] = Query(None),
    day: Optional[List[int]] = Query(None),
    date_start: Optional[str] = Query(None, description="YYYY-MM-DD, inclusive"),
    date_end: Optional[str] = Query(None, description="YYYY-MM-DD, inclusive"),
) -> DashboardFilters:
    return DashboardFilters(
        sending_country=sending_country, receiver_country=receiver_country, corridors=corridors,
        payment_type=payment_type, transstatus=transstatus, agent_name=agent_name,
        transaction_method=transaction_method, sending_country_currency=sending_country_currency,
        payout_currency=payout_currency, year=year, month=month, day=day,
        date_start=date_start, date_end=date_end,
    )


async def _repository(db: Database = Depends(get_database)) -> DashboardReportRepository:
    async with db.session() as session:
        yield DashboardReportRepository(session)


# ------------------------
# RESPONSE MODELS
# Field names with a space/hyphen (as the frontend's existing
# getPayloadSection() contract expects, e.g. "Year-Month") are declared via
# alias= -- FastAPI's default response_model_by_alias=True serializes
# using the alias, so the JSON shape stays byte-for-byte what the
# unmodified dashboard template already expects.
# ------------------------

class RowsRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    Year: int
    Month: int
    Day: int
    Month_Name: str = Field(alias="Month Name")
    Year_Month: str = Field(alias="Year-Month")
    Sending_Country: str
    Receiver_Country: str
    Corridors: str
    Payment_Type: str
    Agent_Name: str
    Transaction_Method: str
    Sending_Country_Currency: str
    Payout_Currency: str
    Currencies_Pair: str
    Volume_Bracket: str
    Agent_Segment: str
    Volume: Optional[float] = None
    Transactions: int


class TatRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    Year: int
    Month: int
    Year_Month: str = Field(alias="Year-Month")
    Receiver_Country: str
    Agent_Name: str
    Transaction_Method: str
    Payment_Type: str
    TAT_Bucket: str
    TAT_Sum: Optional[float] = None
    TAT_Count: int
    Transactions: int
    Volume: Optional[float] = None


class CreationTimeRow(BaseModel):
    Year: int
    Month: int
    Day: int
    Hour: int
    Sending_Country: str
    Receiver_Country: str
    Payment_Type: str
    Agent_Name: str
    Transaction_Method: str
    Transactions: int
    Volume: Optional[float] = None


class StatusRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    Year: int
    Month: int
    Year_Month: str = Field(alias="Year-Month")
    Sending_Country: str
    Receiver_Country: str
    Payment_Type: str
    Agent_Name: str
    Transaction_Method: str
    Sending_Country_Currency: str
    Payout_Currency: str
    transstatus: str
    Volume_Bracket: str
    Agent_Segment: str
    Volume: Optional[float] = None
    Transactions: int


class RefreshViewResponse(BaseModel):
    status: str


class MetaResponse(BaseModel):
    gcc_countries: List[str]
    bracket_order: List[str]
    tat_bucket_order: List[str]
    agent_segment_order: List[str]
    payment_status_value: str


# ------------------------
# ENDPOINTS
# ------------------------

@router.get("/meta", response_model=MetaResponse)
async def get_meta(user: dict = Depends(get_current_user)) -> MetaResponse:
    """
    Fixed reference/ordering metadata the dashboard's charts need (GCC
    country list, bracket/TAT-bucket/agent-segment display order, the
    "payment" status value) -- constants from AnalyticsEngine, not a
    business_report query. Mirrors the "meta" key
    interactive_dashboard_generator.py embeds for the static-file mode, so
    the live-fetched dashboard gets the exact same values from the same
    source of truth.
    """
    return MetaResponse(
        gcc_countries=AnalyticsEngine.GCC_COUNTRIES,
        bracket_order=AnalyticsEngine.BRACKET_ORDER,
        tat_bucket_order=AnalyticsEngine.TAT_BUCKET_ORDER,
        agent_segment_order=AnalyticsEngine.AGENT_SEGMENT_ORDER,
        payment_status_value="PAYMENT",
    )


@router.get("/rows", response_model=List[RowsRow])
async def get_rows(
    filters: DashboardFilters = Depends(_filters_dependency),
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(get_current_user),
) -> List[dict]:
    return await repo.get_rows(filters)


@router.get("/tat_rows", response_model=List[TatRow])
async def get_tat_rows(
    filters: DashboardFilters = Depends(_filters_dependency),
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(get_current_user),
) -> List[dict]:
    return await repo.get_tat_rows(filters)


@router.get("/creation_time_rows", response_model=List[CreationTimeRow])
async def get_creation_time_rows(
    filters: DashboardFilters = Depends(_filters_dependency),
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(get_current_user),
) -> List[dict]:
    return await repo.get_creation_time_rows(filters)


@router.get("/status_rows", response_model=List[StatusRow])
async def get_status_rows(
    filters: DashboardFilters = Depends(_filters_dependency),
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(get_current_user),
) -> List[dict]:
    return await repo.get_status_rows(filters)


@router.get("/filter_options", response_model=Dict[str, List[Any]])
async def get_filter_options(
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(get_current_user),
) -> dict:
    return await repo.get_filter_options()


@router.get("/forecast_ml", response_model=Dict[str, Any])
async def get_forecast_ml(
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Serves the precomputed Reference-tier forecast blob (LightGBM/XGBoost/
    CatBoost/Hybrid Boost/Prophet, walk-forward validation, drill-down
    forecasts) from forecast_cache. See the migration plan's Phase 3 on
    how/when this table gets (re)populated.
    """
    return await repo.get_forecast()


@router.post("/admin/refresh-view", response_model=RefreshViewResponse)
async def admin_refresh_view(
    repo: DashboardReportRepository = Depends(_repository),
    user: dict = Depends(require_admin),
) -> RefreshViewResponse:
    """Manual on-demand refresh, for ops convenience -- the primary refresh
    path is still Codes/postgres_pipeline.py running after every ETL append."""
    await repo.refresh_materialized_view()
    return RefreshViewResponse(status="refreshed")
