from datetime import date
from typing import Annotated, List

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import HistoricalSalesMonthly, PlacedOrder

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class TimeSeriesPoint(BaseModel):
    period: date
    fact: float = 0
    target: float = 0
    forecast_baseline: float = 0
    forecast_adjusted: float = 0
    available_stock: float = 0


class DashboardKPI(BaseModel):
    total_sales_value: float = 0
    total_orders_value: float = 0
    overstock_value: float = 0
    understock_value: float = 0
    out_of_stock_value: float = 0


class DashboardResponse(BaseModel):
    kpis: DashboardKPI
    timeseries: List[TimeSeriesPoint] = Field(default_factory=list)


@router.get("/overview", response_model=DashboardResponse)
async def get_dashboard_overview(
    db: DBSession,
    _user: CurrentUser,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> DashboardResponse:
    """
    Basic MVP dashboard endpoint.

    Currently returns simple aggregates from historical sales and placed orders;
    forecasting and advanced health metrics can be layered on later.
    """
    # Aggregate sales
    sales_stmt = select(func.coalesce(func.sum(HistoricalSalesMonthly.fact_quantity_in_mc), 0))
    if date_from:
        sales_stmt = sales_stmt.where(HistoricalSalesMonthly.date >= date_from)
    if date_to:
        sales_stmt = sales_stmt.where(HistoricalSalesMonthly.date <= date_to)
    sales_total = (await db.execute(sales_stmt)).scalar_one()

    # Aggregate orders
    orders_stmt = select(func.coalesce(func.sum(PlacedOrder.amount_kzt), 0))
    if date_from:
        orders_stmt = orders_stmt.where(PlacedOrder.creation_date >= date_from)
    if date_to:
        orders_stmt = orders_stmt.where(PlacedOrder.creation_date <= date_to)
    orders_total = (await db.execute(orders_stmt)).scalar_one()

    # Time series skeleton: group sales by month
    ts_stmt = (
        select(
            func.date_trunc("month", HistoricalSalesMonthly.date).label("period"),
            func.coalesce(func.sum(HistoricalSalesMonthly.fact_quantity_in_mc), 0).label(
                "fact"
            ),
        )
        .group_by("period")
        .order_by("period")
    )
    if date_from:
        ts_stmt = ts_stmt.where(HistoricalSalesMonthly.date >= date_from)
    if date_to:
        ts_stmt = ts_stmt.where(HistoricalSalesMonthly.date <= date_to)
    rows = (await db.execute(ts_stmt)).all()
    timeseries = [
        TimeSeriesPoint(period=r.period, fact=float(r.fact or 0)) for r in rows  # type: ignore[attr-defined]
    ]

    return DashboardResponse(
        kpis=DashboardKPI(
            total_sales_value=float(sales_total or 0),
            total_orders_value=float(orders_total or 0),
        ),
        timeseries=timeseries,
    )

