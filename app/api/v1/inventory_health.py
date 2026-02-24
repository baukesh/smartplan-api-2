from calendar import monthrange
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product

router = APIRouter(prefix="/inventory-health", tags=["inventory-health"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


class InventoryHealthTableRow(BaseModel):
    sku_code: str
    sku_name: str
    abc_category: str
    sales_value: float
    share_percent: float
    health_index: float


class InventoryHealthTableResponse(BaseModel):
    items: list[InventoryHealthTableRow]
    total_items: int
    total_pages: int


class CategorySummaryRow(BaseModel):
    abc_category: str
    view_type: str
    number_of_skus: int
    percent_of_skus: float
    sales_share_percent: float
    total_sales_value: float
    share_of_stock: float
    category_health_index: float


class TopSkuShareRow(BaseModel):
    sku_code: str
    share_of_stock: float


class TopSkuShareResponse(BaseModel):
    items: list[TopSkuShareRow]


class InventoryHealthFilterOptionsResponse(BaseModel):
    branch_names: list[str]


class _SkuMetrics(BaseModel):
    sku_id: str
    sku_code: str
    sku_name: str
    sales_qty: float
    sales_dsp: float
    stock: float
    share_business: float
    share_stock: float
    share_percent: float
    health_index: float
    abc_category: str


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be one of: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


def _normalize_view_type(view_type: str) -> str:
    v = view_type.strip().lower()
    if v not in {"dsp", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="view_type must be either 'DSP' or 'Cases'",
        )
    return v


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


def _merge_branch_filters(
    branch_name: list[str] | None, branch: list[str] | None
) -> list[str] | None:
    # Support both query styles: branch_name (legacy) and branch (frontend-friendly).
    merged: list[str] = []
    for source in (branch_name, branch):
        if not source:
            continue
        for value in source:
            normalized = str(value).strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
    return merged or None


@router.get("/filter-options", response_model=InventoryHealthFilterOptionsResponse)
async def get_inventory_health_filter_options(
    db: DBSession,
    user: CurrentUser,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> InventoryHealthFilterOptionsResponse:
    same_month = (
        date_from is not None
        and date_to is not None
        and date_from.year == date_to.year
        and date_from.month == date_to.month
    )
    if same_month:
        d_from, d_to = _month_bounds(date_from)
    else:
        d_from, d_to = date_from, date_to

    hs_stmt = select(HistoricalSalesMonthly.branch_id)
    if not is_admin(user):
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    if d_from:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.date >= d_from)
    if d_to:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.date <= d_to)
    branch_ids = {str(row[0]) for row in (await db.execute(hs_stmt)).all()}

    if not branch_ids:
        return InventoryHealthFilterOptionsResponse(branch_names=[])

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
    return InventoryHealthFilterOptionsResponse(branch_names=names)


async def _compute_inventory_metrics(
    db: DBSession,
    user: CurrentUser,
    view_type: str,
    branch_names: list[str] | None,
    date_from: date | None,
    date_to: date | None,
) -> list[_SkuMetrics]:
    metric = _normalize_view_type(view_type)

    branch_stmt = select(Branch)
    if not is_admin(user):
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
    branches = (await db.execute(branch_stmt)).scalars().all()
    branch_name_to_ids: dict[str, set[str]] = {}
    known_branch_ids: set[str] = set()
    for b in branches:
        branch_name_to_ids.setdefault(b.branch_name, set()).add(b.branch_id)
        known_branch_ids.add(b.branch_id)

    selected_branch_ids: set[str] | None = None
    if branch_names:
        requested = [n.strip() for n in branch_names if n and n.strip()]
        if requested:
            selected_branch_ids = set()
            for name in requested:
                if name in branch_name_to_ids:
                    selected_branch_ids.update(branch_name_to_ids[name])
                else:
                    selected_branch_ids.add(name)

    date_scope_stmt = select(func.max(HistoricalSalesMonthly.date))
    if not is_admin(user):
        date_scope_stmt = date_scope_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    if selected_branch_ids:
        date_scope_stmt = date_scope_stmt.where(HistoricalSalesMonthly.branch_id.in_(selected_branch_ids))
    max_existing_date = (await db.execute(date_scope_stmt)).scalar_one_or_none()
    if max_existing_date is not None:
        if (date_from and date_from > max_existing_date) or (date_to and date_to > max_existing_date):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Please select only past dates available in historical_sales_monthly",
            )

    same_month = (
        date_from is not None
        and date_to is not None
        and date_from.year == date_to.year
        and date_from.month == date_to.month
    )
    if same_month:
        d_from, d_to = _month_bounds(date_from)
    else:
        d_from, d_to = date_from, date_to

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

    product_stmt = select(Product)
    if not is_admin(user):
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(product_stmt)).scalars().all()
    product_map = {(p.owner_user_id, p.sku_id): p for p in products}

    price_stmt = select(PriceList)
    if not is_admin(user):
        price_stmt = price_stmt.where(PriceList.owner_user_id == user.id)
    prices = (await db.execute(price_stmt)).scalars().all()
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
    for p in prices:
        prices_by_key.setdefault((p.owner_user_id, p.sku_id), []).append(p)
    for k in prices_by_key:
        prices_by_key[k].sort(key=lambda x: x.date)

    agg: dict[tuple[int, str], dict[str, float | str]] = {}
    for r in hs_rows:
        key = (r.owner_user_id, r.sku_id)
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

        sales_qty = float(r.fact_quantity_in_mc or 0.0)
        sales_dsp = sales_qty * float(p.pieces_in_master_carton or 0.0) * dsp
        stock = float(r.past_available_stock or 0.0)

        bucket = agg.setdefault(
            key,
            {
                "sku_id": r.sku_id,
                "sku_code": p.sku_code,
                "sku_name": p.sku_name,
                "sales_qty": 0.0,
                "sales_dsp": 0.0,
                "stock": 0.0,
            },
        )
        bucket["sales_qty"] = float(bucket["sales_qty"]) + sales_qty
        bucket["sales_dsp"] = float(bucket["sales_dsp"]) + sales_dsp
        bucket["stock"] = float(bucket["stock"]) + stock

    if not agg:
        return []

    total_sales_qty = sum(float(v["sales_qty"]) for v in agg.values())
    total_sales_dsp = sum(float(v["sales_dsp"]) for v in agg.values())
    total_sales_business = total_sales_dsp if metric == "dsp" else total_sales_qty
    total_stock = sum(float(v["stock"]) for v in agg.values())

    interim: list[dict] = []
    for v in agg.values():
        sales_value = float(v["sales_dsp"]) if metric == "dsp" else float(v["sales_qty"])
        share_business = sales_value / total_sales_business if total_sales_business > 0 else 0.0
        share_stock = float(v["stock"]) / total_stock if total_stock > 0 else 0.0
        health = (share_stock / share_business) * 100.0 if share_business > 0 else 0.0
        interim.append(
            {
                "sku_id": str(v["sku_id"]),
                "sku_code": str(v["sku_code"]),
                "sku_name": str(v["sku_name"]),
                "sales_qty": float(v["sales_qty"]),
                "sales_dsp": float(v["sales_dsp"]),
                "stock": float(v["stock"]),
                "share_business": share_business,
                "share_stock": share_stock,
                "share_percent": share_business * 100.0,
                "health_index": health,
            }
        )

    interim.sort(key=lambda x: x["share_business"], reverse=True)
    cumulative = 0.0
    for x in interim:
        cumulative += float(x["share_business"])
        if cumulative <= 0.80:
            x["abc_category"] = "A"
        elif cumulative <= 0.95:
            x["abc_category"] = "B"
        else:
            x["abc_category"] = "C"

    return [_SkuMetrics(**x) for x in interim]


def _build_category_summary(
    metrics: list[_SkuMetrics], category: str, view_type: str
) -> CategorySummaryRow:
    metric = _normalize_view_type(view_type)
    total_skus = len(metrics)
    total_sales_value_all = (
        sum(m.sales_dsp for m in metrics) if metric == "dsp" else sum(m.sales_qty for m in metrics)
    )
    total_stock = sum(m.stock for m in metrics)
    filtered = [m for m in metrics if m.abc_category == category]

    number_of_skus = len(filtered)
    category_sales_value = (
        sum(m.sales_dsp for m in filtered)
        if metric == "dsp"
        else sum(m.sales_qty for m in filtered)
    )
    category_stock = sum(m.stock for m in filtered)

    percent_of_skus = (number_of_skus / total_skus * 100.0) if total_skus > 0 else 0.0
    sales_share_percent = (
        (category_sales_value / total_sales_value_all * 100.0) if total_sales_value_all > 0 else 0.0
    )
    share_of_stock = (category_stock / total_stock) if total_stock > 0 else 0.0
    category_health_index = sum(m.health_index * m.share_business for m in filtered)

    return CategorySummaryRow(
        abc_category=category,
        view_type=metric,
        number_of_skus=number_of_skus,
        percent_of_skus=round(percent_of_skus, 1),
        sales_share_percent=round(sales_share_percent, 1),
        total_sales_value=round(category_sales_value, 2),
        share_of_stock=round(share_of_stock, 4),
        category_health_index=round(category_health_index, 2),
    )


@router.get("/", response_model=InventoryHealthTableResponse)
async def get_inventory_health_table(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> InventoryHealthTableResponse:
    metrics = await _compute_inventory_metrics(
        db=db,
        user=user,
        view_type=view_type,
        branch_names=_merge_branch_filters(branch_name, branch),
        date_from=date_from,
        date_to=date_to,
    )
    table = [
        InventoryHealthTableRow(
            sku_code=m.sku_code,
            sku_name=m.sku_name,
            abc_category=m.abc_category,
            sales_value=round(m.sales_dsp if _normalize_view_type(view_type) == "dsp" else m.sales_qty, 2),
            share_percent=round(m.share_percent, 2),
            health_index=round(m.health_index, 2),
        )
        for m in metrics
    ]
    items, total_items, total_pages = _paginate(table, page=page, page_size=page_size)
    return InventoryHealthTableResponse(items=items, total_items=total_items, total_pages=total_pages)


@router.get("/category-a", response_model=CategorySummaryRow)
async def get_category_a(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> CategorySummaryRow:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, _merge_branch_filters(branch_name, branch), date_from, date_to
    )
    return _build_category_summary(metrics, "A", view_type)


@router.get("/category-b", response_model=CategorySummaryRow)
async def get_category_b(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> CategorySummaryRow:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, _merge_branch_filters(branch_name, branch), date_from, date_to
    )
    return _build_category_summary(metrics, "B", view_type)


@router.get("/category-c", response_model=CategorySummaryRow)
async def get_category_c(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> CategorySummaryRow:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, _merge_branch_filters(branch_name, branch), date_from, date_to
    )
    return _build_category_summary(metrics, "C", view_type)


def _top_issue_rows(metrics: list[_SkuMetrics], issue_type: str, top_n: int) -> list[TopSkuShareRow]:
    total_stock = sum(m.stock for m in metrics)
    if issue_type == "overstock":
        chosen = sorted(
            [m for m in metrics if m.health_index >= 100.0],
            key=lambda m: m.health_index,
            reverse=True,
        )
    elif issue_type == "understock":
        chosen = sorted(
            [m for m in metrics if 0.0 < m.health_index < 100.0],
            key=lambda m: m.health_index,
        )
    else:
        chosen = [m for m in metrics if abs(m.health_index) < 1e-9]
        chosen = sorted(chosen, key=lambda m: m.share_stock, reverse=True)

    sliced = chosen[: max(top_n, 0)]
    return [
        TopSkuShareRow(
            sku_code=m.sku_code,
            share_of_stock=round((m.stock / total_stock * 100.0) if total_stock > 0 else 0.0, 1),
        )
        for m in sliced
    ]


@router.get("/overstock", response_model=TopSkuShareResponse)
async def get_overstock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> TopSkuShareResponse:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, _merge_branch_filters(branch_name, branch), date_from, date_to
    )
    return TopSkuShareResponse(items=_top_issue_rows(metrics, "overstock", top_n))


@router.get("/understock", response_model=TopSkuShareResponse)
async def get_understock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> TopSkuShareResponse:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, _merge_branch_filters(branch_name, branch), date_from, date_to
    )
    return TopSkuShareResponse(items=_top_issue_rows(metrics, "understock", top_n))


@router.get("/out-of-stock", response_model=TopSkuShareResponse)
async def get_out_of_stock(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP or Cases"),
    branch_name: list[str] | None = Query(None),
    branch: list[str] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    top_n: int = Query(5, ge=1),
) -> TopSkuShareResponse:
    metrics = await _compute_inventory_metrics(
        db, user, view_type, _merge_branch_filters(branch_name, branch), date_from, date_to
    )
    return TopSkuShareResponse(items=_top_issue_rows(metrics, "out-of-stock", top_n))

