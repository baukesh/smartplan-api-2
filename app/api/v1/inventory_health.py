from calendar import monthrange
from datetime import date
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.date_params import parse_query_date
from app.core.branch_localization import localize_branch_name, normalize_branch_lookup
from app.core.ttl_cache import AsyncTTLCache
from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product, ProductBranch
from app.models.derived import ForecastSalesMonthly
from app.services.report_override_utils import apply_latest_case_overrides_to_forecast_rows

router = APIRouter(prefix="/inventory-health", tags=["inventory-health"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}
_inventory_metrics_cache: AsyncTTLCache[list["_SkuMetrics"]] = AsyncTTLCache(ttl_seconds=45.0, maxsize=128)


class InventoryHealthTableRow(BaseModel):
    sku_code: str
    sku_name: str
    abc_category: str
    sales_value: float
    share_percent: float
    health_index: int


class InventoryHealthTableResponse(BaseModel):
    items: list[InventoryHealthTableRow]
    total_items: int
    total_pages: int
    filter_options: "InventoryHealthFilterOptions"


class InventoryHealthFilterOptions(BaseModel):
    sku_code: list[str] = Field(default_factory=list)
    sku_name: list[str] = Field(default_factory=list)
    abc_category: list[str] = Field(default_factory=list)


class CategorySummaryRow(BaseModel):
    abc_category: str
    view_type: str
    number_of_skus: int
    percent_of_skus: float
    sales_share_percent: float
    total_sales_value: float
    share_of_stock: float
    category_health_index: int


class TopSkuShareRow(BaseModel):
    sku_name: str
    share_of_stock: float
    health_index_deviation: int


class TopSkuShareResponse(BaseModel):
    items: list[TopSkuShareRow]


class OutOfStockRow(BaseModel):
    sku_name: str


class OutOfStockResponse(BaseModel):
    items: list[OutOfStockRow]


class InventoryHealthFilterOptionsResponse(BaseModel):
    branch_names: list[str]
    min_date: str | None = None
    max_date: str | None = None


class HealthIndexInformationResponse(BaseModel):
    healthy: str
    normal: str
    critical_understock: str
    critical_overstock: str


class HealthIndexDeviationInformationResponse(BaseModel):
    overstock_logic: str
    understock_logic: str
    out_of_stock_logic: str
    notes: str


class _SkuMetrics(BaseModel):
    sku_code: str
    sku_name: str
    sales_qty: float
    sales_dsp: float
    sales_invoice_price: float
    stock: float
    stock_dsp: float
    stock_invoice_price: float
    required_stock: float
    required_stock_dsp: float
    required_stock_invoice_price: float
    stock_diff: float
    stock_diff_dsp: float
    stock_diff_invoice_price: float
    share_business: float
    share_stock: float
    share_percent: float
    health_index: float
    abc_category: str
    average_historical_sales: float
    status: str | None = None


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр page_size должен быть одним из: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


def _normalize_view_type(view_type: str) -> str:
    v = view_type.strip().lower()
    if v not in {"dsp", "invoice price", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр view_type должен быть одним из: DSP, Invoice price или Cases",
        )
    return v


def _metric_sales_value(metric: str, m: _SkuMetrics) -> float:
    if metric == "dsp":
        return m.sales_dsp
    if metric == "invoice price":
        return m.sales_invoice_price
    return m.sales_qty


def _metric_stock_value(metric: str, m: _SkuMetrics) -> float:
    if metric == "dsp":
        return m.stock_dsp
    if metric == "invoice price":
        return m.stock_invoice_price
    return m.stock


def _metric_required_stock_value(metric: str, m: _SkuMetrics) -> float:
    if metric == "dsp":
        return m.required_stock_dsp
    if metric == "invoice price":
        return m.required_stock_invoice_price
    return m.required_stock


def _metric_stock_diff_value(metric: str, m: _SkuMetrics) -> float:
    if metric == "dsp":
        return m.stock_diff_dsp
    if metric == "invoice price":
        return m.stock_diff_invoice_price
    return m.stock_diff


def _forecast_required_for_stock_norm(
    forecast_qty_by_key: dict[tuple[int, str, str, date], float],
    owner_user_id: int,
    branch_id: str,
    sku_code: str,
    basis_month: date,
    stock_norm_days: float,
) -> float:
    if stock_norm_days <= 0:
        return 0.0
    target_qty = 0.0
    remaining_days = float(stock_norm_days)
    month_offset = 1
    while remaining_days > 0:
        covered_days = min(30.0, remaining_days)
        month = _add_months(basis_month, month_offset)
        monthly_qty = forecast_qty_by_key.get((owner_user_id, branch_id, sku_code, month), 0.0)
        target_qty += monthly_qty * (covered_days / 30.0)
        remaining_days -= covered_days
        month_offset += 1
    return target_qty


def _paginate(items: list, page: int, page_size: str) -> tuple[list, int, int]:
    size = _parse_page_size(page_size)
    total_items = len(items)
    if size is None:
        return items, total_items, 1
    total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    offset = (page - 1) * size
    return items[offset : offset + size], total_items, total_pages


def _month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    last_day = monthrange(d.year, d.month)[1]
    last = d.replace(day=last_day)
    return first, last


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def _resolve_date_window(
    *,
    period: str | date | None,
    date_from: str | date | None,
    date_to: str | date | None,
) -> tuple[date | None, date | None]:
    # New frontend flow: period (single month) takes precedence when provided.
    parsed_period = parse_query_date(period, field_name="period")
    if parsed_period is not None:
        return _month_bounds(parsed_period)

    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    same_month = (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from.year == parsed_date_to.year
        and parsed_date_from.month == parsed_date_to.month
    )
    if same_month:
        return _month_bounds(parsed_date_from)
    return parsed_date_from, parsed_date_to


def _merge_branch_filters(
    branch_name: list[str] | None, branch: list[str] | None
) -> list[str] | None:
    # Support both query styles: branch_name (legacy) and branch (frontend-friendly).
    merged: list[str] = []
    for source in (branch_name, branch):
        if not source:
            continue
        for value in source:
            raw = str(value).strip()
            if not raw:
                continue
            # Support comma-separated style ("Алматы,Астана").
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            for normalized in parts:
                localized = str(localize_branch_name(normalized) or normalized)
                if localized not in merged:
                    merged.append(localized)
    return merged or None


def _branch_filters_from_referer(referer: str | None) -> list[str] | None:
    if not referer:
        return None
    try:
        parsed = urlparse(referer)
        qs = parse_qs(parsed.query)
    except Exception:
        return None
    collected: list[str] = []
    for key in ("branch", "branch_name"):
        for raw in qs.get(key) or []:
            decoded = unquote(str(raw or "")).strip()
            if decoded:
                collected.append(decoded)
    if not collected:
        return None
    return _merge_branch_filters(None, collected)


@router.get("/filter-options", response_model=InventoryHealthFilterOptionsResponse)
async def get_inventory_health_filter_options(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> InventoryHealthFilterOptionsResponse:
    d_from, d_to = _resolve_date_window(period=period, date_from=date_from, date_to=date_to)

    date_range_stmt = select(
        func.min(HistoricalSalesMonthly.date),
        func.max(HistoricalSalesMonthly.date),
    )
    if not is_admin(user):
        date_range_stmt = date_range_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    min_hist_date, max_hist_date = (await db.execute(date_range_stmt)).one()
    min_date = min_hist_date.strftime("%Y-%m") if min_hist_date else None
    max_date = max_hist_date.strftime("%Y-%m") if max_hist_date else None

    hs_stmt = select(HistoricalSalesMonthly.branch_id)
    if not is_admin(user):
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    if d_from:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.date >= d_from)
    if d_to:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.date <= d_to)
    branch_ids = {str(row[0]) for row in (await db.execute(hs_stmt)).all()}

    if not branch_ids:
        return InventoryHealthFilterOptionsResponse(
            branch_names=[],
            min_date=min_date,
            max_date=max_date,
        )

    b_stmt = select(Branch)
    if not is_admin(user):
        b_stmt = b_stmt.where(Branch.owner_user_id == user.id)
    branches = (await db.execute(b_stmt)).scalars().all()
    names = sorted(
        {
            str(b.branch_name).strip()
            for b in branches
            if b.branch_id in branch_ids and str(b.branch_name).strip()
        }
    )
    return InventoryHealthFilterOptionsResponse(
        branch_names=names,
        min_date=min_date,
        max_date=max_date,
    )


def _user_cache_scope(user: CurrentUser) -> tuple[str, int]:
    return ("admin" if is_admin(user) else "user", int(user.id))


def _normalize_cache_date(value: str | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip() or None


async def clear_inventory_health_cache() -> None:
    await _inventory_metrics_cache.clear()


async def _compute_inventory_metrics(
    db: DBSession,
    user: CurrentUser,
    view_type: str,
    branch_names: list[str] | None,
    period: str | date | None,
    date_from: str | date | None,
    date_to: str | date | None,
) -> list[_SkuMetrics]:
    metric = _normalize_view_type(view_type)
    branch_key = tuple(sorted(normalize_branch_lookup(n) for n in (branch_names or []) if str(n or "").strip()))
    key = (
        *_user_cache_scope(user),
        metric,
        branch_key,
        _normalize_cache_date(period),
        _normalize_cache_date(date_from),
        _normalize_cache_date(date_to),
    )
    return await _inventory_metrics_cache.get_or_set(
        key,
        lambda: _compute_inventory_metrics_uncached(db, user, metric, branch_names, period, date_from, date_to),
    )


async def _compute_inventory_metrics_uncached(
    db: DBSession,
    user: CurrentUser,
    view_type: str,
    branch_names: list[str] | None,
    period: str | date | None,
    date_from: str | date | None,
    date_to: str | date | None,
) -> list[_SkuMetrics]:
    metric = _normalize_view_type(view_type)
    d_from, d_to = _resolve_date_window(period=period, date_from=date_from, date_to=date_to)
    requested_from_month = d_from.replace(day=1) if d_from else None
    requested_to_month = d_to.replace(day=1) if d_to else None

    branch_stmt = select(Branch)
    if not is_admin(user):
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
    branches = (await db.execute(branch_stmt)).scalars().all()
    branch_name_to_ids: dict[str, set[str]] = {}
    known_branch_ids: set[str] = set()
    for b in branches:
        branch_name_to_ids.setdefault(normalize_branch_lookup(b.branch_name), set()).add(b.branch_id)
        known_branch_ids.add(b.branch_id)

    selected_branch_ids: set[str] | None = None
    if branch_names:
        requested = [n.strip() for n in branch_names if n and n.strip()]
        if requested:
            selected_branch_ids = set()
            for name in requested:
                normalized_name = normalize_branch_lookup(name)
                if normalized_name in branch_name_to_ids:
                    selected_branch_ids.update(branch_name_to_ids[normalized_name])
                else:
                    selected_branch_ids.add(name)

    date_scope_stmt = select(func.max(HistoricalSalesMonthly.date))
    if not is_admin(user):
        date_scope_stmt = date_scope_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    if selected_branch_ids:
        date_scope_stmt = date_scope_stmt.where(HistoricalSalesMonthly.branch_id.in_(selected_branch_ids))
    max_existing_date = (await db.execute(date_scope_stmt)).scalar_one_or_none()
    if max_existing_date is not None:
        max_existing_month = max_existing_date.replace(day=1)
        if (requested_from_month and requested_from_month > max_existing_month) or (
            requested_to_month and requested_to_month > max_existing_month
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Выберите только прошедшие даты, доступные в historical_sales_monthly",
            )

    hs_stmt = select(HistoricalSalesMonthly)
    if not is_admin(user):
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    if selected_branch_ids:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.branch_id.in_(selected_branch_ids))
    if d_from:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.date >= d_from)
    if d_to:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.date <= d_to)
    hs_rows = (await db.execute(hs_stmt)).scalars().all()
    if not hs_rows:
        return []

    sales_window_end = requested_to_month or (max_existing_date.replace(day=1) if max_existing_date else None)
    sales_window_start = _add_months(sales_window_end, -5) if sales_window_end else None
    sales_stmt = select(HistoricalSalesMonthly)
    if not is_admin(user):
        sales_stmt = sales_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    if selected_branch_ids:
        sales_stmt = sales_stmt.where(HistoricalSalesMonthly.branch_id.in_(selected_branch_ids))
    if sales_window_start:
        sales_stmt = sales_stmt.where(HistoricalSalesMonthly.date >= sales_window_start)
    if sales_window_end:
        sales_stmt = sales_stmt.where(HistoricalSalesMonthly.date <= sales_window_end)
    sales_rows = (await db.execute(sales_stmt)).scalars().all()

    product_stmt = select(Product)
    if not is_admin(user):
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(product_stmt)).scalars().all()
    product_map = {(p.owner_user_id, str(p.sku_code).strip()): p for p in products}

    price_stmt = select(PriceList)
    if not is_admin(user):
        price_stmt = price_stmt.where(PriceList.owner_user_id == user.id)
    prices = (await db.execute(price_stmt)).scalars().all()
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
    for p in prices:
        prices_by_key.setdefault((p.owner_user_id, str(p.sku_code).strip()), []).append(p)
    for k in prices_by_key:
        prices_by_key[k].sort(key=lambda x: x.date)

    product_branch_stmt = select(ProductBranch)
    forecast_stmt = select(ForecastSalesMonthly)
    if not is_admin(user):
        product_branch_stmt = product_branch_stmt.where(ProductBranch.owner_user_id == user.id)
        forecast_stmt = forecast_stmt.where(ForecastSalesMonthly.owner_user_id == user.id)
    if selected_branch_ids:
        product_branch_stmt = product_branch_stmt.where(ProductBranch.branch_id.in_(selected_branch_ids))
        forecast_stmt = forecast_stmt.where(ForecastSalesMonthly.branch_id.in_(selected_branch_ids))
    product_branch_rows = (await db.execute(product_branch_stmt)).scalars().all()
    stock_norm_by_branch_sku = {
        (row.owner_user_id, str(row.branch_id or "").strip(), str(row.sku_code or "").strip()): float(
            row.stock_norm or 0.0
        )
        for row in product_branch_rows
    }
    forecast_rows = (await db.execute(forecast_stmt)).scalars().all()
    branch_stmt = select(Branch)
    if not is_admin(user):
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
    branch_rows = (await db.execute(branch_stmt)).scalars().all()
    branch_name_by_id = {str(row.branch_id).strip(): str(row.branch_name) for row in branch_rows}
    product_by_sku = {str(product.sku_code or "").strip(): product for product in products}
    latest_override_qty = {}
    if not is_admin(user):
        latest_override_qty = await apply_latest_case_overrides_to_forecast_rows(
            db,
            owner_user_id=int(user.id),
            forecast_rows=forecast_rows,
            product_by_sku=product_by_sku,
            branch_name_by_id=branch_name_by_id,
        )
    forecast_qty_by_key: dict[tuple[int, str, str, date], float] = {}
    for row in forecast_rows:
        month = row.date.replace(day=1)
        key = (
            row.owner_user_id,
            str(row.branch_id or "").strip(),
            str(row.sku_code or "").strip(),
            month,
        )
        qty = (
            latest_override_qty.get((str(row.sku_code or "").strip(), str(row.branch_id or "").strip(), month))
            if (str(row.sku_code or "").strip(), str(row.branch_id or "").strip(), month) in latest_override_qty
            else float(row.adjusted_forecast_quantity_in_mc)
            if row.adjusted_forecast_quantity_in_mc is not None
            else float(row.baseline_forecast_quantity_in_mc or 0.0)
        )
        forecast_qty_by_key[key] = forecast_qty_by_key.get(key, 0.0) + qty

    sales_agg: dict[tuple[int, str], dict[str, float]] = {}
    month_buckets_by_sku: dict[tuple[int, str], set[str]] = {}
    for r in sales_rows:
        if not str(r.branch_id or "").strip():
            continue
        key = (r.owner_user_id, str(r.sku_code or "").strip())
        p = product_map.get(key)
        if not p:
            continue
        price_candidates = prices_by_key.get(key, [])
        chosen_price = None
        for pr in price_candidates:
            if pr.date <= r.date:
                chosen_price = pr
        if chosen_price is None and price_candidates:
            chosen_price = price_candidates[0]
        dsp = float(chosen_price.dsp) if chosen_price is not None else 0.0
        invoice_price = float(chosen_price.invoice_price) if chosen_price is not None else 0.0

        sales_qty = float(r.fact_quantity_in_mc or 0.0)
        sales_dsp = sales_qty * float(p.pieces_in_master_carton or 0.0) * dsp
        sales_invoice_price = sales_qty * float(p.pieces_in_master_carton or 0.0) * invoice_price
        sales_bucket = sales_agg.setdefault(
            key,
            {
                "sales_qty": 0.0,
                "sales_dsp": 0.0,
                "sales_invoice_price": 0.0,
            },
        )
        sales_bucket["sales_qty"] += sales_qty
        sales_bucket["sales_dsp"] += sales_dsp
        sales_bucket["sales_invoice_price"] += sales_invoice_price
        month_buckets_by_sku.setdefault(key, set()).add(r.date.replace(day=1).isoformat())

    for key, sales_bucket in sales_agg.items():
        month_count = len(month_buckets_by_sku.get(key, set()))
        if month_count <= 0:
            continue
        sales_bucket["sales_qty"] /= month_count
        sales_bucket["sales_dsp"] /= month_count
        sales_bucket["sales_invoice_price"] /= month_count

    agg: dict[tuple[int, str], dict[str, float | str | int | None]] = {}
    for r in hs_rows:
        if not str(r.branch_id or "").strip():
            continue
        key = (r.owner_user_id, str(r.sku_code or "").strip())
        p = product_map.get(key)
        if not p:
            continue

        price_candidates = prices_by_key.get(key, [])
        chosen_price = None
        for pr in price_candidates:
            if pr.date <= r.date:
                chosen_price = pr
        if chosen_price is None and price_candidates:
            chosen_price = price_candidates[0]
        dsp = float(chosen_price.dsp) if chosen_price is not None else 0.0
        invoice_price = float(chosen_price.invoice_price) if chosen_price is not None else 0.0

        stock = float(r.past_available_stock or 0.0)
        stock_dsp = stock * float(p.pieces_in_master_carton or 0.0) * dsp
        stock_invoice_price = stock * float(p.pieces_in_master_carton or 0.0) * invoice_price
        branch_id = str(r.branch_id or "").strip()
        month = r.date.replace(day=1)
        stock_norm_days = stock_norm_by_branch_sku.get(
            (r.owner_user_id, branch_id, str(r.sku_code or "").strip()),
            float(p.general_stock_norm_days or 0.0),
        )
        required_stock = _forecast_required_for_stock_norm(
            forecast_qty_by_key=forecast_qty_by_key,
            owner_user_id=r.owner_user_id,
            branch_id=branch_id,
            sku_code=str(r.sku_code or "").strip(),
            basis_month=month,
            stock_norm_days=stock_norm_days,
        )
        required_stock_dsp = required_stock * float(p.pieces_in_master_carton or 0.0) * dsp
        required_stock_invoice_price = required_stock * float(p.pieces_in_master_carton or 0.0) * invoice_price

        bucket = agg.setdefault(
            key,
            {
                "owner_user_id": int(p.owner_user_id),
                "sku_code": p.sku_code,
                "sku_name": p.sku_name,
                "status": str(p.status or "").strip().lower(),
                "sales_qty": float(sales_agg.get(key, {}).get("sales_qty", 0.0)),
                "sales_dsp": float(sales_agg.get(key, {}).get("sales_dsp", 0.0)),
                "sales_invoice_price": float(
                    sales_agg.get(key, {}).get("sales_invoice_price", 0.0)
                ),
                "stock": 0.0,
                "stock_dsp": 0.0,
                "stock_invoice_price": 0.0,
                "required_stock": 0.0,
                "required_stock_dsp": 0.0,
                "required_stock_invoice_price": 0.0,
            },
        )
        bucket["stock"] = float(bucket["stock"]) + stock
        bucket["stock_dsp"] = float(bucket["stock_dsp"]) + stock_dsp
        bucket["stock_invoice_price"] = float(bucket["stock_invoice_price"]) + stock_invoice_price
        bucket["required_stock"] = float(bucket["required_stock"]) + required_stock
        bucket["required_stock_dsp"] = float(bucket["required_stock_dsp"]) + required_stock_dsp
        bucket["required_stock_invoice_price"] = (
            float(bucket["required_stock_invoice_price"]) + required_stock_invoice_price
        )

    if not agg:
        return []

    total_sales_qty = sum(float(v["sales_qty"]) for v in agg.values())
    total_sales_dsp = sum(float(v["sales_dsp"]) for v in agg.values())
    total_sales_invoice_price = sum(float(v["sales_invoice_price"]) for v in agg.values())
    total_sales_business = (
        total_sales_dsp
        if metric == "dsp"
        else total_sales_invoice_price
        if metric == "invoice price"
        else total_sales_qty
    )
    # A/B/C segmentation must remain stable across view_type and always be DSP-driven.
    # Fallback to quantity share only when DSP total is zero to avoid degenerate buckets.
    abc_total_sales = total_sales_dsp if total_sales_dsp > 0 else total_sales_qty
    total_stock = sum(float(v["stock"]) for v in agg.values())
    total_stock_dsp = sum(float(v["stock_dsp"]) for v in agg.values())
    total_stock_invoice_price = sum(float(v["stock_invoice_price"]) for v in agg.values())

    interim: list[dict] = []
    for v in agg.values():
        sales_value = (
            float(v["sales_dsp"])
            if metric == "dsp"
            else float(v["sales_invoice_price"])
            if metric == "invoice price"
            else float(v["sales_qty"])
        )
        share_business = sales_value / total_sales_business if total_sales_business > 0 else 0.0
        abc_share_business = (
            float(v["sales_dsp"]) / abc_total_sales
            if total_sales_dsp > 0 and abc_total_sales > 0
            else (float(v["sales_qty"]) / abc_total_sales if abc_total_sales > 0 else 0.0)
        )
        share_stock = (
            (float(v["stock_dsp"]) / total_stock_dsp)
            if metric == "dsp" and total_stock_dsp > 0
            else (float(v["stock_invoice_price"]) / total_stock_invoice_price)
            if metric == "invoice price" and total_stock_invoice_price > 0
            else ((float(v["stock"]) / total_stock) if total_stock > 0 else 0.0)
        )
        health = (share_stock / share_business) * 100.0 if share_business > 0 else 0.0
        sku_key = (int(v.get("owner_user_id", 0) or 0), str(v["sku_code"]))
        month_count = len(month_buckets_by_sku.get(sku_key, set()))
        avg_hist_sales = (float(v["sales_qty"]) / float(month_count)) if month_count > 0 else 0.0
        stock_diff = abs(float(v["stock"]) - float(v["required_stock"]))
        stock_diff_dsp = abs(float(v["stock_dsp"]) - float(v["required_stock_dsp"]))
        stock_diff_invoice_price = abs(
            float(v["stock_invoice_price"]) - float(v["required_stock_invoice_price"])
        )
        interim.append(
            {
                "sku_code": str(v["sku_code"]),
                "sku_name": str(v["sku_name"]),
                "sales_qty": float(v["sales_qty"]),
                "sales_dsp": float(v["sales_dsp"]),
                "sales_invoice_price": float(v["sales_invoice_price"]),
                "stock": float(v["stock"]),
                "stock_dsp": float(v["stock_dsp"]),
                "stock_invoice_price": float(v["stock_invoice_price"]),
                "required_stock": float(v["required_stock"]),
                "required_stock_dsp": float(v["required_stock_dsp"]),
                "required_stock_invoice_price": float(v["required_stock_invoice_price"]),
                "stock_diff": stock_diff,
                "stock_diff_dsp": stock_diff_dsp,
                "stock_diff_invoice_price": stock_diff_invoice_price,
                "share_business": share_business,
                "share_stock": share_stock,
                "share_percent": share_business * 100.0,
                "health_index": health,
                "abc_share_business": abc_share_business,
                "average_historical_sales": avg_hist_sales,
                "status": v.get("status"),
            }
        )

    interim.sort(key=lambda x: x["abc_share_business"], reverse=True)
    cumulative = 0.0
    for x in interim:
        cumulative += float(x["abc_share_business"])
        if cumulative <= 0.80:
            x["abc_category"] = "A"
        elif cumulative <= 0.95:
            x["abc_category"] = "B"
        else:
            x["abc_category"] = "C"

    return [_SkuMetrics(**x) for x in interim]


def _build_category_summary(
    metrics: list[_SkuMetrics],
    category: str,
    view_type: str,
    stock_share_metrics: list[_SkuMetrics] | None = None,
) -> CategorySummaryRow:
    metric = _normalize_view_type(view_type)
    total_skus = len(metrics)
    total_sales_value_all = sum(_metric_sales_value(metric, m) for m in metrics)
    # share_of_stock must be cases-based regardless of selected metric view.
    stock_base = stock_share_metrics if stock_share_metrics is not None else metrics
    total_stock = sum(m.stock for m in stock_base)
    filtered = [m for m in metrics if m.abc_category == category]
    filtered_stock = [m for m in stock_base if m.abc_category == category]

    number_of_skus = len(filtered)
    category_sales_value = sum(_metric_sales_value(metric, m) for m in filtered)
    category_stock = sum(m.stock for m in filtered_stock)

    percent_of_skus = (number_of_skus / total_skus * 100.0) if total_skus > 0 else 0.0
    sales_share_percent = (
        (category_sales_value / total_sales_value_all * 100.0) if total_sales_value_all > 0 else 0.0
    )
    share_of_stock = ((category_stock / total_stock) * 100.0) if total_stock > 0 else 0.0
    # Weighted average health index inside the category (A/B/C),
    # not weighted contribution to the whole portfolio.
    category_business_share = sum(m.share_business for m in filtered)
    category_health_index = (
        sum(m.health_index * (m.share_business / category_business_share) for m in filtered)
        if category_business_share > 0
        else 0.0
    )

    return CategorySummaryRow(
        abc_category=f"Категория {category}",
        view_type=metric,
        number_of_skus=number_of_skus,
        percent_of_skus=round(percent_of_skus, 1),
        sales_share_percent=round(sales_share_percent, 1),
        total_sales_value=round(category_sales_value, 2),
        share_of_stock=round(share_of_stock, 2),
        category_health_index=int(round(category_health_index)),
    )


async def _build_category_summary_cases_stock(
    db: DBSession,
    user: CurrentUser,
    category: str,
    view_type: str,
    merged_branch_filters: list[str] | None,
    period: str | None,
    date_from: str | None,
    date_to: str | None,
) -> CategorySummaryRow:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, merged_branch_filters, period, date_from, date_to
    )
    normalized_view = _normalize_view_type(view_type)
    if normalized_view == "cases":
        return _build_category_summary(metrics, category, view_type, stock_share_metrics=metrics)

    cases_metrics = await _compute_inventory_metrics(
        db, user, "cases", merged_branch_filters, period, date_from, date_to
    )
    return _build_category_summary(
        metrics,
        category,
        view_type,
        stock_share_metrics=cases_metrics,
    )


@router.get("", response_model=InventoryHealthTableResponse, include_in_schema=False)
@router.get("/", response_model=InventoryHealthTableResponse)
async def get_inventory_health_table(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
    abc_category: list[str] | None = Query(None),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> InventoryHealthTableResponse:
    metrics = await _compute_inventory_metrics(
        db=db,
        user=user,
        view_type=view_type,
        branch_names=_merge_branch_filters(branch_name, branch),
        period=period,
        date_from=date_from,
        date_to=date_to,
    )
    table = [
        InventoryHealthTableRow(
            sku_code=m.sku_code,
            sku_name=m.sku_name,
            abc_category=m.abc_category,
            sales_value=round(_metric_sales_value(_normalize_view_type(view_type), m), 2),
            share_percent=round(m.share_percent, 2),
            health_index=int(round(m.health_index)),
        )
        for m in metrics
    ]
    filtered_table = table
    if sku_code:
        sku_code_values = {str(v).strip() for v in sku_code if str(v).strip()}
        filtered_table = [
            r for r in filtered_table if str(r.sku_code).strip() in sku_code_values
        ]
    if sku_name:
        sku_name_values = {str(v).strip() for v in sku_name if str(v).strip()}
        filtered_table = [
            r for r in filtered_table if str(r.sku_name).strip() in sku_name_values
        ]
    if abc_category:
        abc_values = {
            str(v).strip().upper() for v in abc_category if str(v).strip()
        }
        filtered_table = [
            r for r in filtered_table if str(r.abc_category).strip().upper() in abc_values
        ]

    filter_options = InventoryHealthFilterOptions(
        sku_code=sorted({str(r.sku_code).strip() for r in filtered_table if str(r.sku_code).strip()}),
        sku_name=sorted({str(r.sku_name).strip() for r in filtered_table if str(r.sku_name).strip()}),
        abc_category=sorted(
            {str(r.abc_category).strip() for r in filtered_table if str(r.abc_category).strip()}
        ),
    )

    items, total_items, total_pages = _paginate(filtered_table, page=page, page_size=page_size)
    return InventoryHealthTableResponse(
        items=items,
        total_items=total_items,
        total_pages=total_pages,
        filter_options=filter_options,
    )


@router.get("/category-a", response_model=CategorySummaryRow)
async def get_category_a(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    referer: str | None = Header(default=None, alias="Referer"),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> CategorySummaryRow:
    merged_branch_filters = _merge_branch_filters(branch_name, branch) or _branch_filters_from_referer(
        referer
    )
    return await _build_category_summary_cases_stock(
        db=db,
        user=user,
        category="A",
        view_type=view_type,
        merged_branch_filters=merged_branch_filters,
        period=period,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/category-b", response_model=CategorySummaryRow)
async def get_category_b(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    referer: str | None = Header(default=None, alias="Referer"),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> CategorySummaryRow:
    merged_branch_filters = _merge_branch_filters(branch_name, branch) or _branch_filters_from_referer(
        referer
    )
    return await _build_category_summary_cases_stock(
        db=db,
        user=user,
        category="B",
        view_type=view_type,
        merged_branch_filters=merged_branch_filters,
        period=period,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/category-c", response_model=CategorySummaryRow)
async def get_category_c(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    referer: str | None = Header(default=None, alias="Referer"),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> CategorySummaryRow:
    merged_branch_filters = _merge_branch_filters(branch_name, branch) or _branch_filters_from_referer(
        referer
    )
    return await _build_category_summary_cases_stock(
        db=db,
        user=user,
        category="C",
        view_type=view_type,
        merged_branch_filters=merged_branch_filters,
        period=period,
        date_from=date_from,
        date_to=date_to,
    )


def _top_issue_rows(
    metrics: list[_SkuMetrics],
    issue_type: str,
    top_n: int,
    view_type: str,
) -> list[TopSkuShareRow]:
    metric = _normalize_view_type(view_type)
    if issue_type == "overstock":
        chosen = [
            m
            for m in metrics
            if _metric_stock_value(metric, m) > _metric_required_stock_value(metric, m)
            and _metric_stock_diff_value(metric, m) > 0
        ]
    elif issue_type == "understock":
        chosen = [
            m
            for m in metrics
            if _metric_stock_value(metric, m) < _metric_required_stock_value(metric, m)
            and _metric_stock_diff_value(metric, m) > 0
        ]
    else:
        chosen = []

    total_diff = sum(_metric_stock_diff_value(metric, m) for m in chosen)
    chosen = sorted(chosen, key=lambda m: _metric_stock_diff_value(metric, m), reverse=True)
    limit = max(top_n, 0)
    sliced = chosen[:limit]
    rows = [
        TopSkuShareRow(
            sku_name=m.sku_name,
            share_of_stock=share,
            health_index_deviation=int(round(share)),
        )
        for m in sliced
        for share in [
            round((_metric_stock_diff_value(metric, m) / total_diff * 100.0) if total_diff > 0 else 0.0, 1)
        ]
    ]
    if len(chosen) > limit:
        others_diff = sum(_metric_stock_diff_value(metric, m) for m in chosen[limit:])
        others_share = round((others_diff / total_diff * 100.0) if total_diff > 0 else 0.0, 1)
        rows.append(
            TopSkuShareRow(
                sku_name="Others",
                share_of_stock=others_share,
                health_index_deviation=int(round(others_share)),
            )
        )
    return rows


def _out_of_stock_rows(metrics: list[_SkuMetrics], top_n: int) -> list[OutOfStockRow]:
    active_zero_stock = [
        m
        for m in metrics
        if abs(float(m.stock)) < 1e-9 and str(m.status or "").strip().lower() == "активный"
    ]
    active_zero_stock = sorted(
        active_zero_stock,
        key=lambda m: (m.average_historical_sales, m.share_stock),
        reverse=True,
    )
    sliced = active_zero_stock[: max(top_n, 0)]
    return [OutOfStockRow(sku_name=m.sku_name) for m in sliced]


@router.get("/overstock", response_model=TopSkuShareResponse)
async def get_overstock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> TopSkuShareResponse:
    metrics = await _compute_inventory_metrics(
        db,
        user,
        view_type,
        _merge_branch_filters(branch_name, branch),
        period,
        date_from,
        date_to,
    )
    return TopSkuShareResponse(items=_top_issue_rows(metrics, "overstock", top_n, view_type))


@router.get("/understock", response_model=TopSkuShareResponse)
async def get_understock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> TopSkuShareResponse:
    metrics = await _compute_inventory_metrics(
        db,
        user,
        view_type,
        _merge_branch_filters(branch_name, branch),
        period,
        date_from,
        date_to,
    )
    return TopSkuShareResponse(items=_top_issue_rows(metrics, "understock", top_n, view_type))


@router.get("/out-of-stock/", response_model=OutOfStockResponse, include_in_schema=False)
@router.get("/out-of-stock", response_model=OutOfStockResponse)
async def get_out_of_stock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    period: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> OutOfStockResponse:
    metrics = await _compute_inventory_metrics(
        db,
        user,
        view_type,
        _merge_branch_filters(branch_name, branch),
        period,
        date_from,
        date_to,
    )
    return OutOfStockResponse(items=_out_of_stock_rows(metrics, top_n))


@router.get("/health-index-information-icon", response_model=HealthIndexInformationResponse)
async def get_health_index_information_icon(
    user: CurrentUser,
) -> HealthIndexInformationResponse:
    # Authenticated endpoint for frontend info icon tooltip content.
    _ = user
    return HealthIndexInformationResponse(
        healthy="90-110",
        normal="70-90 или 110-130",
        critical_understock="меньше 70: критический недостаток запаса относительно нормы",
        critical_overstock="больше 130: критический избыток запаса относительно нормы",
    )


@router.get(
    "/health-index-deviation-information-icon/",
    response_model=HealthIndexDeviationInformationResponse,
    include_in_schema=False,
)
@router.get(
    "/health-index-deviation-information-icon",
    response_model=HealthIndexDeviationInformationResponse,
)
async def get_health_index_deviation_information_icon(
    user: CurrentUser,
) -> HealthIndexDeviationInformationResponse:
    # Authenticated endpoint for frontend info icon tooltip content.
    _ = user
    return HealthIndexDeviationInformationResponse(
        overstock_logic=(
            "Избыток показывает товары, у которых запаса больше, чем обычно нужно для текущего "
            "уровня продаж. Чем выше значение, тем сильнее товар перегружает склад."
        ),
        understock_logic=(
            "Недостаток показывает товары, у которых запаса меньше, чем нужно для текущего "
            "уровня продаж. Чем выше значение, тем выше риск нехватки товара."
        ),
        out_of_stock_logic=(
            "Нет в наличии показывает активные товары, по которым сейчас нет доступного запаса. "
            "Список помогает быстро увидеть позиции, которые могут требовать пополнения в первую очередь."
        ),
        notes=(
            "Показатели помогают сравнить товары между собой и выделить самые важные проблемы "
            "с запасами для принятия решений."
        ),
    )

