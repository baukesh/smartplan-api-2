from datetime import date

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin
from app.api.v1.inventory_health import (
    _branch_filters_from_referer,
    _build_category_summary,
    _compute_inventory_metrics,
    _merge_branch_filters,
)
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product
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
            detail="Параметр date_from не может быть больше date_to",
        )


def _scope_stmt(stmt, model, user: CurrentUser):
    if not is_admin(user):
        stmt = stmt.where(model.owner_user_id == user.id)
    return stmt


async def _max_historical_date(db: DBSession, user: CurrentUser) -> date | None:
    stmt = _scope_stmt(select(func.max(HistoricalSalesMonthly.date)), HistoricalSalesMonthly, user)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _resolve_last_year_period(
    db: DBSession, user: CurrentUser, date_from: str | date | None, date_to: str | date | None
) -> tuple[date | None, date | None]:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    _validate_range(parsed_date_from, parsed_date_to)
    max_date = await _max_historical_date(db, user)
    if max_date is None:
        return None, None
    max_month = _month_start(max_date)
    requested_to = _month_start(parsed_date_to or max_date)
    # Dashboard should remain informative even when frontend requests a future month.
    # Clamp upper bound to the latest available historical month.
    resolved_to = min(requested_to, max_month)
    resolved_from = _month_start(parsed_date_from or _add_months(resolved_to, -12))
    if resolved_from > resolved_to:
        resolved_from = _add_months(resolved_to, -12)
    return resolved_from, resolved_to


class SalesOverviewResponse(BaseModel):
    view_type: str
    total_target_value: float
    total_fact_value: float
    trend_percent: float


class BranchSalesPerformanceRow(BaseModel):
    branch_name: str
    fact_value_per_branch: int
    sales_performance: int


class BranchSalesOverviewResponse(BaseModel):
    view_type: str
    top_branches: list[BranchSalesPerformanceRow] = Field(default_factory=list)
    bottom_branches: list[BranchSalesPerformanceRow] = Field(default_factory=list)


class SkuSalesPerformanceRow(BaseModel):
    sku_name: str
    fact_value_per_sku: int
    sales_performance_per_sku: int


class SkuSalesOverviewResponse(BaseModel):
    view_type: str
    top_skus: list[SkuSalesPerformanceRow] = Field(default_factory=list)


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
    planning_month: date | None = None
    aggregated_historical_sales_data: list[HistoricalPlotPoint] = Field(default_factory=list)
    aggregated_forecast_sales_data: list[ForecastPlotPoint] = Field(default_factory=list)


def _normalize_dashboard_view_type(view_type: str) -> str:
    normalized = (view_type or "").strip().lower()
    if normalized not in {"dsp", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр view_type должен быть одним из: DSP или Cases",
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
    metrics = await _compute_inventory_metrics_safe_for_dashboard(
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
    branch_names: list[str] | None = None,
):
    normalized_view_type = _normalize_dashboard_view_type(view_type)
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    _validate_range(parsed_date_from, parsed_date_to)
    max_date = await _max_historical_date(db, user)
    if max_date is None:
        return _build_category_summary([], category, normalized_view_type, stock_share_metrics=[])
    max_month = _month_start(max_date)
    if parsed_date_to and _month_start(parsed_date_to) > max_month:
        parsed_date_to = max_month
    if parsed_date_from and _month_start(parsed_date_from) > max_month:
        parsed_date_from = max_month
    if parsed_date_from and parsed_date_to and parsed_date_from > parsed_date_to:
        parsed_date_from = parsed_date_to
    metrics = await _compute_inventory_metrics_safe_for_dashboard(
        db=db,
        user=user,
        view_type=normalized_view_type,
        branch_names=branch_names,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
    )
    if normalized_view_type == "cases":
        return _build_category_summary(
            metrics,
            category,
            normalized_view_type,
            stock_share_metrics=metrics,
        )

    # For dashboard category cards, share_of_stock must always remain cases-based
    # regardless of selected view type.
    cases_metrics = await _compute_inventory_metrics_safe_for_dashboard(
        db=db,
        user=user,
        view_type="cases",
        branch_names=branch_names,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
    )
    return _build_category_summary(
        metrics,
        category,
        normalized_view_type,
        stock_share_metrics=cases_metrics,
    )


async def _compute_inventory_metrics_safe_for_dashboard(
    db: DBSession,
    user: CurrentUser,
    view_type: str,
    branch_names: list[str] | None,
    date_from: date | None,
    date_to: date | None,
):
    try:
        return await _compute_inventory_metrics(
            db=db,
            user=user,
            view_type=view_type,
            branch_names=branch_names,
            period=None,
            date_from=date_from,
            date_to=date_to,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY and str(exc.detail) == (
            "Please select only past dates available in historical_sales_monthly"
        ):
            return []
        raise


def _safe_percent_int(actual: float, target: float) -> int:
    if abs(target) < 1e-9:
        return 0
    return int((actual / target) * 100.0)


@router.get("/sku-sales-overview/", response_model=SkuSalesOverviewResponse, include_in_schema=False)
@router.get("/sku-sales-overview", response_model=SkuSalesOverviewResponse)
async def get_sku_sales_overview(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    top_n: int = Query(10, ge=1),
) -> SkuSalesOverviewResponse:
    normalized_view_type = _normalize_dashboard_view_type(view_type)
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    _validate_range(parsed_date_from, parsed_date_to)

    max_hist_date = await _max_historical_date(db, user)
    if max_hist_date is None:
        return SkuSalesOverviewResponse(view_type=normalized_view_type)

    max_hist_month = _month_start(max_hist_date)
    planning_month = _add_months(max_hist_month, 1)
    resolved_from = _month_start(parsed_date_from or _add_months(max_hist_month, -12))
    resolved_to = _month_start(parsed_date_to or max_hist_month)

    product_pieces_by_key: dict[tuple[int, str], float] = {}
    product_name_by_key: dict[tuple[int, str], str] = {}
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}

    product_stmt = _scope_stmt(select(Product), Product, user)
    products = (await db.execute(product_stmt)).scalars().all()
    for p in products:
        key = (int(p.owner_user_id), str(p.sku_code or "").strip())
        product_name_by_key[key] = str(p.sku_name or "").strip()
        product_pieces_by_key[key] = float(p.pieces_in_master_carton or 0.0)

    if normalized_view_type == "dsp":
        price_stmt = _scope_stmt(select(PriceList), PriceList, user)
        prices = (await db.execute(price_stmt)).scalars().all()
        for p in prices:
            key = (int(p.owner_user_id), str(p.sku_code or "").strip())
            prices_by_key.setdefault(key, []).append(p)
        for key in prices_by_key:
            prices_by_key[key].sort(key=lambda x: x.date)

    def _dsp_for_key_on_or_before(key: tuple[int, str], point_date: date) -> float:
        series = prices_by_key.get(key, [])
        if not series:
            return 0.0
        selected = None
        for p in series:
            if p.date <= point_date:
                selected = p
        if selected is None:
            selected = series[-1]
        return float(selected.dsp or 0.0)

    sku_perf: dict[tuple[int, str], dict[str, float]] = {}

    hist_from = resolved_from
    hist_to = min(resolved_to, max_hist_month)
    if hist_from <= hist_to:
        hist_stmt = _scope_stmt(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.date >= hist_from,
                HistoricalSalesMonthly.date <= hist_to,
            ),
            HistoricalSalesMonthly,
            user,
        )
        hist_rows = (await db.execute(hist_stmt)).scalars().all()
        for row in hist_rows:
            sku_code_value = str(row.sku_code or "").strip()
            if not sku_code_value:
                continue
            sku_key = (int(row.owner_user_id), sku_code_value)
            bucket = sku_perf.setdefault(sku_key, {"fact": 0.0, "target": 0.0})
            if normalized_view_type == "dsp":
                pieces = product_pieces_by_key.get(sku_key, 0.0)
                dsp = _dsp_for_key_on_or_before(sku_key, row.date)
                fact_amount = float(row.fact_amount_kzt or 0.0)
                target_amount = float(row.target_amount_kzt or 0.0)
                if abs(fact_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    fact_amount = float(row.fact_quantity_in_mc or 0.0) * pieces * dsp
                if abs(target_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    target_amount = float(row.target_quantity_in_mc or 0.0) * pieces * dsp
                bucket["fact"] += fact_amount
                bucket["target"] += target_amount
            else:
                bucket["fact"] += float(row.fact_quantity_in_mc or 0.0)
                bucket["target"] += float(row.target_quantity_in_mc or 0.0)

    fc_from = max(resolved_from, planning_month)
    fc_to = resolved_to
    if fc_from <= fc_to:
        fc_stmt = _scope_stmt(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.date >= fc_from,
                ForecastSalesMonthly.date <= fc_to,
            ),
            ForecastSalesMonthly,
            user,
        )
        fc_rows = (await db.execute(fc_stmt)).scalars().all()
        for row in fc_rows:
            sku_code_value = str(row.sku_code or "").strip()
            if not sku_code_value:
                continue
            sku_key = (int(row.owner_user_id), sku_code_value)
            bucket = sku_perf.setdefault(sku_key, {"fact": 0.0, "target": 0.0})
            if normalized_view_type == "dsp":
                pieces = product_pieces_by_key.get(sku_key, 0.0)
                dsp = _dsp_for_key_on_or_before(sku_key, row.date)
                baseline_amount = float(row.baseline_forecast_amount_kzt or 0.0)
                adjusted_amount = (
                    float(row.adjusted_forecast_amount_kzt)
                    if row.adjusted_forecast_amount_kzt is not None
                    else baseline_amount
                )
                baseline_qty = float(row.baseline_forecast_quantity_in_mc or 0.0)
                adjusted_qty = (
                    float(row.adjusted_forecast_quantity_in_mc)
                    if row.adjusted_forecast_quantity_in_mc is not None
                    else baseline_qty
                )
                if abs(baseline_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    baseline_amount = baseline_qty * pieces * dsp
                if abs(adjusted_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    adjusted_amount = adjusted_qty * pieces * dsp
                # For future periods: fact=baseline forecast, target=adjusted forecast.
                bucket["fact"] += baseline_amount
                bucket["target"] += adjusted_amount
            else:
                baseline_qty = float(row.baseline_forecast_quantity_in_mc or 0.0)
                adjusted_qty = (
                    float(row.adjusted_forecast_quantity_in_mc)
                    if row.adjusted_forecast_quantity_in_mc is not None
                    else baseline_qty
                )
                bucket["fact"] += baseline_qty
                bucket["target"] += adjusted_qty

    rows: list[SkuSalesPerformanceRow] = []
    for sku_key, vals in sku_perf.items():
        owner_id, sku_code = sku_key
        name = product_name_by_key.get((owner_id, sku_code), sku_code)
        rows.append(
            SkuSalesPerformanceRow(
                sku_name=name,
                fact_value_per_sku=int(vals["fact"]),
                sales_performance_per_sku=_safe_percent_int(float(vals["fact"]), float(vals["target"])),
            )
        )

    rows = sorted(
        rows,
        key=lambda x: (x.sales_performance_per_sku, x.fact_value_per_sku, x.sku_name),
        reverse=True,
    )
    return SkuSalesOverviewResponse(
        view_type=normalized_view_type,
        top_skus=rows[:top_n],
    )


@router.get(
    "/branch-sales-overview/",
    response_model=BranchSalesOverviewResponse,
    include_in_schema=False,
)
@router.get("/branch-sales-overview", response_model=BranchSalesOverviewResponse)
async def get_branch_sales_overview(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> BranchSalesOverviewResponse:
    normalized_view_type = _normalize_dashboard_view_type(view_type)
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    _validate_range(parsed_date_from, parsed_date_to)

    max_hist_date = await _max_historical_date(db, user)
    if max_hist_date is None:
        return BranchSalesOverviewResponse(view_type=normalized_view_type)

    max_hist_month = _month_start(max_hist_date)
    planning_month = _add_months(max_hist_month, 1)
    resolved_from = _month_start(parsed_date_from or _add_months(max_hist_month, -12))
    resolved_to = _month_start(parsed_date_to or max_hist_month)

    product_pieces_by_key: dict[tuple[int, str], float] = {}
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
    if normalized_view_type == "dsp":
        product_stmt = _scope_stmt(select(Product), Product, user)
        products = (await db.execute(product_stmt)).scalars().all()
        product_pieces_by_key = {
            (int(p.owner_user_id), str(p.sku_code).strip()): float(p.pieces_in_master_carton or 0.0)
            for p in products
        }
        price_stmt = _scope_stmt(select(PriceList), PriceList, user)
        prices = (await db.execute(price_stmt)).scalars().all()
        for p in prices:
            key = (int(p.owner_user_id), str(p.sku_code or "").strip())
            prices_by_key.setdefault(key, []).append(p)
        for key in prices_by_key:
            prices_by_key[key].sort(key=lambda x: x.date)

    def _dsp_for_key_on_or_before(key: tuple[int, str], point_date: date) -> float:
        series = prices_by_key.get(key, [])
        if not series:
            return 0.0
        selected = None
        for p in series:
            if p.date <= point_date:
                selected = p
        if selected is None:
            selected = series[-1]
        return float(selected.dsp or 0.0)

    branch_perf: dict[tuple[int, str], dict[str, float]] = {}

    hist_from = resolved_from
    hist_to = min(resolved_to, max_hist_month)
    if hist_from <= hist_to:
        hist_stmt = _scope_stmt(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.date >= hist_from,
                HistoricalSalesMonthly.date <= hist_to,
            ),
            HistoricalSalesMonthly,
            user,
        )
        hist_rows = (await db.execute(hist_stmt)).scalars().all()
        for row in hist_rows:
            branch_id_value = str(row.branch_id or "").strip()
            if not branch_id_value:
                continue
            branch_key = (int(row.owner_user_id), branch_id_value)
            bucket = branch_perf.setdefault(branch_key, {"fact": 0.0, "target": 0.0})
            if normalized_view_type == "dsp":
                tuple_key = (int(row.owner_user_id), str(row.sku_code or "").strip())
                pieces = product_pieces_by_key.get(tuple_key, 0.0)
                dsp = _dsp_for_key_on_or_before(tuple_key, row.date)
                fact_amount = float(row.fact_amount_kzt or 0.0)
                target_amount = float(row.target_amount_kzt or 0.0)
                if abs(fact_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    fact_amount = float(row.fact_quantity_in_mc or 0.0) * pieces * dsp
                if abs(target_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    target_amount = float(row.target_quantity_in_mc or 0.0) * pieces * dsp
                bucket["fact"] += fact_amount
                bucket["target"] += target_amount
            else:
                bucket["fact"] += float(row.fact_quantity_in_mc or 0.0)
                bucket["target"] += float(row.target_quantity_in_mc or 0.0)

    fc_from = max(resolved_from, planning_month)
    fc_to = resolved_to
    if fc_from <= fc_to:
        fc_stmt = _scope_stmt(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.date >= fc_from,
                ForecastSalesMonthly.date <= fc_to,
            ),
            ForecastSalesMonthly,
            user,
        )
        fc_rows = (await db.execute(fc_stmt)).scalars().all()
        for row in fc_rows:
            branch_id_value = str(row.branch_id or "").strip()
            if not branch_id_value:
                continue
            branch_key = (int(row.owner_user_id), branch_id_value)
            bucket = branch_perf.setdefault(branch_key, {"fact": 0.0, "target": 0.0})
            if normalized_view_type == "dsp":
                tuple_key = (int(row.owner_user_id), str(row.sku_code or "").strip())
                pieces = product_pieces_by_key.get(tuple_key, 0.0)
                dsp = _dsp_for_key_on_or_before(tuple_key, row.date)
                baseline_amount = float(row.baseline_forecast_amount_kzt or 0.0)
                adjusted_amount = (
                    float(row.adjusted_forecast_amount_kzt)
                    if row.adjusted_forecast_amount_kzt is not None
                    else baseline_amount
                )
                baseline_qty = float(row.baseline_forecast_quantity_in_mc or 0.0)
                adjusted_qty = (
                    float(row.adjusted_forecast_quantity_in_mc)
                    if row.adjusted_forecast_quantity_in_mc is not None
                    else baseline_qty
                )
                if abs(baseline_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    baseline_amount = baseline_qty * pieces * dsp
                if abs(adjusted_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    adjusted_amount = adjusted_qty * pieces * dsp
                # For future periods: fact=baseline forecast, target=adjusted forecast.
                bucket["fact"] += baseline_amount
                bucket["target"] += adjusted_amount
            else:
                baseline_qty = float(row.baseline_forecast_quantity_in_mc or 0.0)
                adjusted_qty = (
                    float(row.adjusted_forecast_quantity_in_mc)
                    if row.adjusted_forecast_quantity_in_mc is not None
                    else baseline_qty
                )
                bucket["fact"] += baseline_qty
                bucket["target"] += adjusted_qty

    branch_name_map: dict[tuple[int, str], str] = {}
    branch_stmt = _scope_stmt(select(Branch), Branch, user)
    branch_rows = (await db.execute(branch_stmt)).scalars().all()
    for b in branch_rows:
        branch_name_map[(int(b.owner_user_id), str(b.branch_id).strip())] = str(b.branch_name).strip()

    rows: list[BranchSalesPerformanceRow] = []
    for branch_key, vals in branch_perf.items():
        owner_id, branch_id = branch_key
        pretty_name = branch_name_map.get((owner_id, str(branch_id).strip()), str(branch_id).strip())
        fact_value = int(vals["fact"])
        target_value = float(vals["target"])
        rows.append(
            BranchSalesPerformanceRow(
                branch_name=pretty_name,
                fact_value_per_branch=fact_value,
                sales_performance=_safe_percent_int(float(vals["fact"]), target_value),
            )
        )

    rows = sorted(rows, key=lambda x: (x.sales_performance, x.fact_value_per_branch, x.branch_name))
    total_branches = len(rows)
    if total_branches < 10:
        top_count = (total_branches + 1) // 2
        bottom_count = total_branches // 2
        bottom = rows[:bottom_count]
        top = list(reversed(rows[-top_count:]))
    else:
        bottom = rows[:top_n]
        top = list(reversed(rows[-top_n:]))
    return BranchSalesOverviewResponse(
        view_type=normalized_view_type,
        top_branches=top,
        bottom_branches=bottom,
    )


@router.get("/sales-overview", response_model=SalesOverviewResponse)
async def get_sales_overview(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> SalesOverviewResponse:
    normalized_view_type = view_type.strip().lower()
    if normalized_view_type not in {"dsp", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр view_type должен быть одним из: DSP или Cases",
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

    # Keep DSP cards consistent with plot-data:
    # if historical amount columns are absent/zero, backfill from quantity * pieces * DSP.
    if normalized_view_type == "dsp" and (abs(target_amount) < 1e-9 or abs(fact_amount) < 1e-9):
        hist_stmt = _scope_stmt(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.date >= resolved_from,
                HistoricalSalesMonthly.date <= resolved_to,
            ),
            HistoricalSalesMonthly,
            user,
        )
        hist_rows = (await db.execute(hist_stmt)).scalars().all()

        product_stmt = _scope_stmt(select(Product), Product, user)
        products = (await db.execute(product_stmt)).scalars().all()
        product_pieces_by_key = {
            (int(p.owner_user_id), str(p.sku_code).strip()): float(p.pieces_in_master_carton or 0.0)
            for p in products
        }

        price_stmt = _scope_stmt(select(PriceList), PriceList, user)
        prices = (await db.execute(price_stmt)).scalars().all()
        prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
        for p in prices:
            key = (int(p.owner_user_id), str(p.sku_code or "").strip())
            prices_by_key.setdefault(key, []).append(p)
        for key in prices_by_key:
            prices_by_key[key].sort(key=lambda x: x.date)

        def _dsp_for_key_on_or_before(key: tuple[int, str], point_date: date) -> float:
            series = prices_by_key.get(key, [])
            if not series:
                return 0.0
            selected = None
            for p in series:
                if p.date <= point_date:
                    selected = p
            if selected is None:
                selected = series[-1]
            return float(selected.dsp or 0.0)

        fallback_fact_amount = 0.0
        fallback_target_amount = 0.0
        for h in hist_rows:
            tuple_key = (int(h.owner_user_id), str(h.sku_code or "").strip())
            pieces = product_pieces_by_key.get(tuple_key, 0.0)
            dsp = _dsp_for_key_on_or_before(tuple_key, h.date)
            fallback_fact_amount += float(h.fact_quantity_in_mc or 0.0) * pieces * dsp
            fallback_target_amount += float(h.target_quantity_in_mc or 0.0) * pieces * dsp

        if abs(fact_amount) < 1e-9:
            fact_amount = fallback_fact_amount
        if abs(target_amount) < 1e-9:
            target_amount = fallback_target_amount
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
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
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
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
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
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
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
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    referer: str | None = Header(default=None, alias="Referer"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    merged_branch_filters = _merge_branch_filters(branch_name, branch) or _branch_filters_from_referer(
        referer
    )
    return await _inventory_category_summary(
        db,
        user,
        "A",
        view_type,
        date_from,
        date_to,
        branch_names=merged_branch_filters,
    )


@router.get("/inventory-health-overview/category_b")
async def get_dashboard_category_b(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    referer: str | None = Header(default=None, alias="Referer"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    merged_branch_filters = _merge_branch_filters(branch_name, branch) or _branch_filters_from_referer(
        referer
    )
    return await _inventory_category_summary(
        db,
        user,
        "B",
        view_type,
        date_from,
        date_to,
        branch_names=merged_branch_filters,
    )


@router.get("/inventory-health-overview/category_c")
async def get_dashboard_category_c(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    referer: str | None = Header(default=None, alias="Referer"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    merged_branch_filters = _merge_branch_filters(branch_name, branch) or _branch_filters_from_referer(
        referer
    )
    return await _inventory_category_summary(
        db,
        user,
        "C",
        view_type,
        date_from,
        date_to,
        branch_names=merged_branch_filters,
    )


@router.get("/stock-coverage", response_model=StockCoverageResponse)
async def get_stock_coverage(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> StockCoverageResponse:
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return StockCoverageResponse(items=[])
    metrics = await _compute_inventory_metrics_safe_for_dashboard(
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
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> DashboardPlotDataResponse:
    normalized_view_type = _normalize_dashboard_view_type(view_type)
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    # Kept for API compatibility; this response includes both quantity and amount series.
    resolved_from, resolved_to = await _resolve_last_year_period(db, user, date_from, date_to)
    if resolved_from is None or resolved_to is None:
        return DashboardPlotDataResponse(view_type=normalized_view_type)

    product_pieces_by_key: dict[tuple[int, str], float] = {}
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
    if normalized_view_type == "dsp":
        product_stmt = _scope_stmt(select(Product), Product, user)
        products = (await db.execute(product_stmt)).scalars().all()
        product_pieces_by_key = {
            (int(p.owner_user_id), str(p.sku_code).strip()): float(p.pieces_in_master_carton or 0.0)
            for p in products
        }

        price_stmt = _scope_stmt(select(PriceList), PriceList, user)
        prices = (await db.execute(price_stmt)).scalars().all()
        for p in prices:
            key = (int(p.owner_user_id), str(p.sku_code or "").strip())
            prices_by_key.setdefault(key, []).append(p)
        for key in prices_by_key:
            prices_by_key[key].sort(key=lambda x: x.date)

    def _dsp_for_key_on_or_before(key: tuple[int, str], point_date: date) -> float:
        series = prices_by_key.get(key, [])
        if not series:
            return 0.0
        selected = None
        for p in series:
            if p.date <= point_date:
                selected = p
        if selected is None:
            selected = series[-1]
        return float(selected.dsp or 0.0)

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
        if normalized_view_type == "dsp":
            tuple_key = (int(row.owner_user_id), str(row.sku_code or "").strip())
            pieces = product_pieces_by_key.get(tuple_key, 0.0)
            dsp = _dsp_for_key_on_or_before(tuple_key, row.date)
            b["past_stock"] += float(row.past_available_stock or 0.0) * pieces * dsp
            if abs(float(row.fact_amount_kzt or 0.0)) < 1e-9 and pieces > 0 and dsp > 0:
                b["fact_amount"] += float(row.fact_quantity_in_mc or 0.0) * pieces * dsp
            if abs(float(row.target_amount_kzt or 0.0)) < 1e-9 and pieces > 0 and dsp > 0:
                b["target_amount"] += float(row.target_quantity_in_mc or 0.0) * pieces * dsp
        else:
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
            planning_month=None,
            aggregated_historical_sales_data=historical_data,
            aggregated_forecast_sales_data=[],
        )
    planning_month = _add_months(_month_start(max_hist_date), 1)
    max_forecast_to = _add_months(planning_month, 11)
    requested_forecast_to = _month_start(parsed_date_to) if parsed_date_to else _add_months(planning_month, 5)
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
            if normalized_view_type == "dsp":
                tuple_key = (int(row.owner_user_id), str(row.sku_code or "").strip())
                pieces = product_pieces_by_key.get(tuple_key, 0.0)
                dsp = _dsp_for_key_on_or_before(tuple_key, row.date)
                b["future_stock"] += float(row.future_available_stock or 0.0) * pieces * dsp
                if abs(baseline_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    b["baseline_amount"] += baseline_qty * pieces * dsp
                if abs(adjusted_amount) < 1e-9 and pieces > 0 and dsp > 0:
                    b["adjusted_amount"] += adjusted_qty * pieces * dsp
            else:
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
        planning_month=planning_month,
        aggregated_historical_sales_data=historical_data,
        aggregated_forecast_sales_data=forecast_data,
    )


@router.get("/overview", response_model=SalesOverviewResponse)
async def get_dashboard_overview_compat(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> SalesOverviewResponse:
    # Backward-compatible alias of the new sales overview.
    return await get_sales_overview(
        db=db,
        user=user,
        view_type=view_type,
        date_from=date_from,
        date_to=date_to,
    )

