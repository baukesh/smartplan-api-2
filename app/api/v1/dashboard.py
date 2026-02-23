from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.api.v1.inventory_health import _build_category_summary, _compute_inventory_metrics
from app.models.data_uploads import HistoricalSalesMonthly
from app.models.derived import ForecastSalesMonthly

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _round1(value: float) -> float:
    return round(float(value or 0.0), 1)


def _safe_percent_change(actual: float, target: float) -> float:
    if abs(target) < 1e-9:
        return 0.0
    return _round1(((actual - target) / target) * 100.0)


def _validate_range(date_from: date | None, date_to: date | None) -> None:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )


def _scope_stmt(stmt, model, user: CurrentUser):
    if not is_admin(user):
        stmt = stmt.where(model.owner_user_id == user.id)
    return stmt


async def _max_historical_date(db: DBSession, user: CurrentUser) -> date | None:
    stmt = _scope_stmt(select(func.max(HistoricalSalesMonthly.date)), HistoricalSalesMonthly, user)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _resolve_last_year_period(
    db: DBSession, user: CurrentUser, date_from: date | None, date_to: date | None
) -> tuple[date | None, date | None]:
    _validate_range(date_from, date_to)
    max_date = await _max_historical_date(db, user)
    if max_date is None:
        return None, None
    resolved_to = _month_start(date_to or max_date)
    resolved_from = _month_start(date_from or _add_months(resolved_to, -12))
    return resolved_from, resolved_to


class SalesOverviewResponse(BaseModel):
    view_type: str
    total_target_value: float
    total_fact_value: float
    trend_percent: float


class InventoryIssueOverviewResponse(BaseModel):
    view_type: str
    total_value: float
    sales_share_percent: float


class OverstockOverviewResponse(BaseModel):
    view_type: str
    total_overstock_value: float
    overstock_sales_share_percent: float


class UnderstockOverviewResponse(BaseModel):
    view_type: str
    total_understock_value: float
    understock_sales_share_percent: float


class OutOfStockOverviewResponse(BaseModel):
    view_type: str
    total_out_of_stock_value: float
    out_of_stock_sales_share_percent: float


class StockCoverageRow(BaseModel):
    health_index_label: str
    stock_coverage_percent: float


class StockCoverageResponse(BaseModel):
    items: list[StockCoverageRow] = Field(default_factory=list)


class HistoricalPlotPoint(BaseModel):
    date: date
    total_fact_value: float
    total_target_value: float
    total_past_available_stock: float


class ForecastPlotPoint(BaseModel):
    date: date
    total_baseline_forecast_value: float
    total_adjusted_forecast_value: float
    total_future_available_stock: float


class DashboardPlotDataResponse(BaseModel):
    view_type: str
    aggregated_historical_sales_data: list[HistoricalPlotPoint] = Field(default_factory=list)
    aggregated_forecast_sales_data: list[ForecastPlotPoint] = Field(default_factory=list)


def _normalize_dashboard_view_type(view_type: str) -> str:
    normalized = (view_type or "").strip().lower()
    if normalized not in {"dsp", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="view_type must be either 'DSP' or 'Cases'",
        )
    return normalized


async def _inventory_issue_overview(
    db: DBSession,
    user: CurrentUser,
    issue_type: str,
    view_type: str,
    date_from: date | None,
    date_to: date | None,
) -> InventoryIssueOverviewResponse:
    normalized_view_type = _normalize_dashboard_view_type(view_type)
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return InventoryIssueOverviewResponse(
            view_type=normalized_view_type, total_value=0.0, sales_share_percent=0.0
        )
    metrics = await _compute_inventory_metrics(
        db=db,
        user=user,
        view_type=view_type,
        branch_names=None,
        date_from=resolved_from,
        date_to=resolved_to,
    )
    if issue_type == "overstock":
        filtered = [m for m in metrics if m.health_index >= 100.0]
    elif issue_type == "understock":
        filtered = [m for m in metrics if 0.0 < m.health_index < 100.0]
    else:
        filtered = [m for m in metrics if abs(m.health_index) < 1e-9]
    if normalized_view_type == "dsp":
        total_value = sum(m.sales_dsp for m in filtered)
        all_value = sum(m.sales_dsp for m in metrics)
    else:
        total_value = sum(m.sales_qty for m in filtered)
        all_value = sum(m.sales_qty for m in metrics)
    share = (total_value / all_value * 100.0) if all_value > 0 else 0.0
    return InventoryIssueOverviewResponse(
        view_type=normalized_view_type,
        total_value=round(total_value, 2),
        sales_share_percent=_round1(share),
    )


async def _inventory_category_summary(
    db: DBSession,
    user: CurrentUser,
    category: str,
    view_type: str,
    date_from: date | None,
    date_to: date | None,
):
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return _build_category_summary([], category, view_type)
    metrics = await _compute_inventory_metrics(
        db=db,
        user=user,
        view_type=view_type,
        branch_names=None,
        date_from=resolved_from,
        date_to=resolved_to,
    )
    return _build_category_summary(metrics, category, view_type)


@router.get("/sales-overview", response_model=SalesOverviewResponse)
async def get_sales_overview(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> SalesOverviewResponse:
    normalized_view_type = view_type.strip().lower()
    if normalized_view_type not in {"dsp", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="view_type must be either 'DSP' or 'Cases'",
        )
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return SalesOverviewResponse(
            view_type=normalized_view_type,
            total_target_value=0.0,
            total_fact_value=0.0,
            trend_percent=0.0,
        )
    stmt = _scope_stmt(
        select(
            func.coalesce(func.sum(HistoricalSalesMonthly.target_quantity_in_mc), 0.0),
            func.coalesce(func.sum(HistoricalSalesMonthly.target_amount_kzt), 0.0),
            func.coalesce(func.sum(HistoricalSalesMonthly.fact_quantity_in_mc), 0.0),
            func.coalesce(func.sum(HistoricalSalesMonthly.fact_amount_kzt), 0.0),
        ).where(
            HistoricalSalesMonthly.date >= resolved_from,
            HistoricalSalesMonthly.date <= resolved_to,
        ),
        HistoricalSalesMonthly,
        user,
    )
    row = (await db.execute(stmt)).one()
    target_qty = float(row[0] or 0.0)
    target_amount = float(row[1] or 0.0)
    fact_qty = float(row[2] or 0.0)
    fact_amount = float(row[3] or 0.0)
    if normalized_view_type == "dsp":
        total_target_value = target_amount
        total_fact_value = fact_amount
    else:
        total_target_value = target_qty
        total_fact_value = fact_qty

    return SalesOverviewResponse(
        view_type=normalized_view_type,
        total_target_value=round(total_target_value, 2),
        total_fact_value=round(total_fact_value, 2),
        trend_percent=_safe_percent_change(total_fact_value, total_target_value),
    )


@router.get("/inventory-health-overview/overstock", response_model=OverstockOverviewResponse)
async def get_dashboard_overstock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> OverstockOverviewResponse:
    overview = await _inventory_issue_overview(
        db=db,
        user=user,
        issue_type="overstock",
        view_type=view_type,
        date_from=date_from,
        date_to=date_to,
    )
    return OverstockOverviewResponse(
        view_type=overview.view_type,
        total_overstock_value=overview.total_value,
        overstock_sales_share_percent=overview.sales_share_percent,
    )


@router.get("/inventory-health-overview/understock", response_model=UnderstockOverviewResponse)
async def get_dashboard_understock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> UnderstockOverviewResponse:
    overview = await _inventory_issue_overview(
        db=db,
        user=user,
        issue_type="understock",
        view_type=view_type,
        date_from=date_from,
        date_to=date_to,
    )
    return UnderstockOverviewResponse(
        view_type=overview.view_type,
        total_understock_value=overview.total_value,
        understock_sales_share_percent=overview.sales_share_percent,
    )


@router.get("/inventory-health-overview/out-of-stock", response_model=OutOfStockOverviewResponse)
async def get_dashboard_out_of_stock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> OutOfStockOverviewResponse:
    overview = await _inventory_issue_overview(
        db=db,
        user=user,
        issue_type="out-of-stock",
        view_type=view_type,
        date_from=date_from,
        date_to=date_to,
    )
    return OutOfStockOverviewResponse(
        view_type=overview.view_type,
        total_out_of_stock_value=overview.total_value,
        out_of_stock_sales_share_percent=overview.sales_share_percent,
    )


@router.get("/inventory-health-overview/category_a")
async def get_dashboard_category_a(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
):
    return await _inventory_category_summary(db, user, "A", view_type, date_from, date_to)


@router.get("/inventory-health-overview/category_b")
async def get_dashboard_category_b(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
):
    return await _inventory_category_summary(db, user, "B", view_type, date_from, date_to)


@router.get("/inventory-health-overview/category_c")
async def get_dashboard_category_c(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
):
    return await _inventory_category_summary(db, user, "C", view_type, date_from, date_to)


@router.get("/stock-coverage", response_model=StockCoverageResponse)
async def get_stock_coverage(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> StockCoverageResponse:
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return StockCoverageResponse(items=[])
    metrics = await _compute_inventory_metrics(
        db=db,
        user=user,
        view_type=view_type,
        branch_names=None,
        date_from=resolved_from,
        date_to=resolved_to,
    )
    total_stock = sum(m.stock for m in metrics)
    buckets = {"Healthy": 0.0, "Normal": 0.0, "Critical": 0.0}
    for m in metrics:
        distance = abs(m.health_index - 100.0)
        if distance <= 10.0:
            label = "Healthy"
        elif distance <= 30.0:
            label = "Normal"
        else:
            label = "Critical"
        buckets[label] += float(m.stock)
    items = [
        StockCoverageRow(
            health_index_label=label,
            stock_coverage_percent=_round1((stock / total_stock * 100.0) if total_stock > 0 else 0.0),
        )
        for label, stock in buckets.items()
    ]
    return StockCoverageResponse(items=items)


@router.get("/plot-data", response_model=DashboardPlotDataResponse)
async def get_plot_data(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> DashboardPlotDataResponse:
    normalized_view_type = _normalize_dashboard_view_type(view_type)
    # Kept for API compatibility; this response includes both quantity and amount series.
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return DashboardPlotDataResponse(view_type=normalized_view_type)

    hist_stmt = _scope_stmt(
        select(HistoricalSalesMonthly).where(
            HistoricalSalesMonthly.date >= resolved_from,
            HistoricalSalesMonthly.date <= resolved_to,
        ),
        HistoricalSalesMonthly,
        user,
    )
    hist_rows = (await db.execute(hist_stmt)).scalars().all()
    hist_buckets: dict[date, dict[str, float]] = {}
    for row in hist_rows:
        key = _month_start(row.date)
        b = hist_buckets.setdefault(
            key,
            {
                "fact_qty": 0.0,
                "fact_amount": 0.0,
                "target_qty": 0.0,
                "target_amount": 0.0,
                "past_stock": 0.0,
            },
        )
        b["fact_qty"] += float(row.fact_quantity_in_mc or 0.0)
        b["fact_amount"] += float(row.fact_amount_kzt or 0.0)
        b["target_qty"] += float(row.target_quantity_in_mc or 0.0)
        b["target_amount"] += float(row.target_amount_kzt or 0.0)
        b["past_stock"] += float(row.past_available_stock or 0.0)
    historical_data = [
        HistoricalPlotPoint(
            date=d,
            total_fact_value=round(v["fact_amount"], 2)
            if normalized_view_type == "dsp"
            else round(v["fact_qty"], 2),
            total_target_value=round(v["target_amount"], 2)
            if normalized_view_type == "dsp"
            else round(v["target_qty"], 2),
            total_past_available_stock=round(v["past_stock"], 2),
        )
        for d, v in sorted(hist_buckets.items(), key=lambda x: x[0])
    ]

    max_hist_date = await _max_historical_date(db, user)
    if max_hist_date is None:
        return DashboardPlotDataResponse(
            view_type=normalized_view_type,
            aggregated_historical_sales_data=historical_data,
            aggregated_forecast_sales_data=[],
        )
    planning_month = _add_months(_month_start(max_hist_date), 1)
    max_forecast_to = _add_months(planning_month, 11)
    requested_forecast_to = _month_start(date_to) if date_to else _add_months(planning_month, 5)
    forecast_to = min(requested_forecast_to, max_forecast_to)

    forecast_data: list[ForecastPlotPoint] = []
    if forecast_to >= planning_month:
        fc_stmt = _scope_stmt(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.date >= planning_month,
                ForecastSalesMonthly.date <= forecast_to,
            ),
            ForecastSalesMonthly,
            user,
        )
        fc_rows = (await db.execute(fc_stmt)).scalars().all()
        fc_buckets: dict[date, dict[str, float]] = {}
        for row in fc_rows:
            key = _month_start(row.date)
            b = fc_buckets.setdefault(
                key,
                {
                    "baseline_qty": 0.0,
                    "baseline_amount": 0.0,
                    "adjusted_qty": 0.0,
                    "adjusted_amount": 0.0,
                    "future_stock": 0.0,
                },
            )
            baseline_qty = float(row.baseline_forecast_quantity_in_mc or 0.0)
            baseline_amount = float(row.baseline_forecast_amount_kzt or 0.0)
            adjusted_qty = (
                float(row.adjusted_forecast_quantity_in_mc)
                if row.adjusted_forecast_quantity_in_mc is not None
                else baseline_qty
            )
            adjusted_amount = (
                float(row.adjusted_forecast_amount_kzt)
                if row.adjusted_forecast_amount_kzt is not None
                else baseline_amount
            )
            b["baseline_qty"] += baseline_qty
            b["baseline_amount"] += baseline_amount
            b["adjusted_qty"] += adjusted_qty
            b["adjusted_amount"] += adjusted_amount
            b["future_stock"] += float(row.future_available_stock or 0.0)
        forecast_data = [
            ForecastPlotPoint(
                date=d,
                total_baseline_forecast_value=round(v["baseline_amount"], 2)
                if normalized_view_type == "dsp"
                else round(v["baseline_qty"], 2),
                total_adjusted_forecast_value=round(v["adjusted_amount"], 2)
                if normalized_view_type == "dsp"
                else round(v["adjusted_qty"], 2),
                total_future_available_stock=round(v["future_stock"], 2),
            )
            for d, v in sorted(fc_buckets.items(), key=lambda x: x[0])
        ]

    return DashboardPlotDataResponse(
        view_type=normalized_view_type,
        aggregated_historical_sales_data=historical_data,
        aggregated_forecast_sales_data=forecast_data,
    )


@router.get("/overview", response_model=SalesOverviewResponse)
async def get_dashboard_overview_compat(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> SalesOverviewResponse:
    # Backward-compatible alias of the new sales overview.
    return await get_sales_overview(
        db=db,
        user=user,
        view_type=view_type,
        date_from=date_from,
        date_to=date_to,
    )

