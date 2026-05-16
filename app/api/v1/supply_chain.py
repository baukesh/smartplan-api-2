from collections import Counter, defaultdict
from datetime import date
from io import BytesIO
import math
import re

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin
from app.core.source_normalization import normalize_source_value, source_matches
from app.models.data_uploads import HistoricalSalesMonthly, PriceList, Product, ProductBranch
from app.models.derived import ForecastOrders, ForecastSalesMonthly

router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}
SUPPLY_CHAIN_BASE_HEADERS = {
    "sku_code": "Код СКЮ",
    "sku_name": "Наименование товара",
    "recommended_quantity_in_mc": "Рекомендуемое кол-во",
    "adjusted_quantity_in_mc": "Заказать в кол-ве",
}


class SupplyChainRow(BaseModel):
    sku_code: str
    sku_name: str
    month_prior_available_stock: int
    average_l3m_quantity_in_mc: int
    average_f3m_quantity_in_mc: int
    recommended_quantity_in_mc: int
    adjusted_quantity_in_mc: int | None = None


class SupplyChainListResponse(BaseModel):
    period: str
    current_month: str
    source: str
    lead_time_days: int
    lead_time_months: int
    headers: dict[str, str]
    items: list[SupplyChainRow]
    total_sum: float
    total_quantity_in_mc: int
    total_gross_weight: float
    total_volume: float
    total_items: int
    total_pages: int
    filter_options: "SupplyChainFilterOptions"


class SupplyChainFilterOptions(BaseModel):
    sku_code: list[str] = Field(default_factory=list)
    sku_name: list[str] = Field(default_factory=list)


class SupplyChainAdjustRow(BaseModel):
    sku_code: str
    adjusted_quantity_in_mc: int | None = None


class SupplyChainAdjustRequest(BaseModel):
    updates: list[SupplyChainAdjustRow]


class SupplyChainSummary(BaseModel):
    period: str
    current_month: str
    source: str
    lead_time_days: int
    lead_time_months: int
    view_type: str
    total_sum: float
    total_quantity: int
    total_gross_weight: float
    total_volume: float


class SupplyChainFilterOptionsResponse(BaseModel):
    categories: list[str]
    sources: list[str]
    default_source: str | None = None


class SupplyChainInformationIconsResponse(BaseModel):
    month_prior_available_stock: str
    average_l3m_quantity_in_mc: str
    average_f3m_quantity_in_mc: str


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _qty_int(value: float | None) -> int:
    return int(round(float(value or 0.0)))


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _prev_month(d: date) -> date:
    return _add_months(d, -1)


def _ru_month(value: date) -> str:
    month_names = {
        1: "Янв",
        2: "Фев",
        3: "Мар",
        4: "Апр",
        5: "Май",
        6: "Июн",
        7: "Июл",
        8: "Авг",
        9: "Сен",
        10: "Окт",
        11: "Ноя",
        12: "Дек",
    }
    return f"{month_names[value.month]} '{value.strftime('%y')}"


def _lead_time_months(lead_time_days: float | None) -> int:
    return max(int(math.floor((float(lead_time_days or 0.0) / 30.0) + 0.5)), 1)


def _normalize_view_type(view_type: str) -> str:
    normalized = str(view_type or "DSP").strip().lower()
    if normalized not in {"dsp", "invoice price"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр view_type должен быть одним из: DSP, Invoice price",
        )
    return normalized


def _price_for_period(prices: list[PriceList], period_date: date, view_type: str = "dsp") -> float:
    if not prices:
        return 0.0
    sorted_prices = sorted(prices, key=lambda x: x.date)
    selected = None
    for p in sorted_prices:
        if p.date <= period_date:
            selected = p
    if selected is None:
        selected = sorted_prices[0]
    if _normalize_view_type(view_type) == "invoice price":
        return float(selected.invoice_price or 0.0)
    return float(selected.dsp or 0.0)


def _supply_chain_headers(
    *,
    stock_month: date,
    l3_months: list[date],
    f3_months: list[date],
) -> dict[str, str]:
    return {
        "sku_code": SUPPLY_CHAIN_BASE_HEADERS["sku_code"],
        "sku_name": SUPPLY_CHAIN_BASE_HEADERS["sku_name"],
        "month_prior_available_stock": f"Планируемое наличие товара в {_ru_month(stock_month)}",
        "average_l3m_quantity_in_mc": (
            f"Средние продажи за {_ru_month(l3_months[0])} - {_ru_month(l3_months[-1])}"
        ),
        "average_f3m_quantity_in_mc": (
            f"Средние продажи за {_ru_month(f3_months[0])} - {_ru_month(f3_months[-1])}"
        ),
        "recommended_quantity_in_mc": SUPPLY_CHAIN_BASE_HEADERS["recommended_quantity_in_mc"],
        "adjusted_quantity_in_mc": SUPPLY_CHAIN_BASE_HEADERS["adjusted_quantity_in_mc"],
    }


def _period_to_date(period: str) -> date:
    try:
        year, month = period.split("-")
        return date(int(year), int(month), 1)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр period должен быть в формате YYYY-MM",
        ) from exc


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр page_size должен быть одним из: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


def _paginate(items: list, page: int, page_size: str) -> tuple[list, int, int]:
    size = _parse_page_size(page_size)
    total_items = len(items)
    if size is None:
        return items, total_items, 1
    total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    offset = (page - 1) * size
    return items[offset : offset + size], total_items, total_pages


async def _resolve_period(db: DBSession, user: CurrentUser, period: str | None) -> date:
    if period:
        return _period_to_date(period)

    stmt = select(func.max(HistoricalSalesMonthly.date))
    if not is_admin(user):
        stmt = stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    max_hist_date = (await db.execute(stmt)).scalar_one_or_none()
    if max_hist_date is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Невозможно определить период по умолчанию без данных historical_sales_monthly",
        )
    if max_hist_date.month == 12:
        return date(max_hist_date.year + 1, 1, 1)
    return date(max_hist_date.year, max_hist_date.month + 1, 1)


async def _resolve_period_from_args(
    db: DBSession,
    user: CurrentUser,
    period: str | None,
    date_from: str | date | None,
    date_to: str | date | None,
) -> date:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    if period:
        return _period_to_date(period)
    # Backward-compatible query style: allow date_from/date_to and
    # derive the planning period month from either value.
    if parsed_date_to is not None:
        return _month_start(parsed_date_to)
    if parsed_date_from is not None:
        return _month_start(parsed_date_from)
    return await _resolve_period(db, user, None)


def _closest_dsp_for_period(prices: list[PriceList], period_date: date) -> float:
    if not prices:
        return 0.0
    sorted_prices = sorted(prices, key=lambda x: x.date)
    selected = None
    for p in sorted_prices:
        if p.date <= period_date:
            selected = p
    if selected is None:
        selected = sorted_prices[-1]
    return float(selected.dsp)


def _normalize_category_filter(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = re.sub(r"^[^0-9A-Za-zА-Яа-я]+", "", raw)
    normalized = re.sub(r"[^0-9A-Za-zА-Яа-я]+$", "", normalized)
    return normalized.strip().lower()


async def _load_products(db: DBSession, user: CurrentUser) -> list[Product]:
    stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(Product.owner_user_id == user.id)
    return list((await db.execute(stmt)).scalars().all())


def _apply_category_filter(products: list[Product], category: str | None) -> list[Product]:
    if not category:
        return products
    wanted_category = _normalize_category_filter(category)
    return [
        p
        for p in products
        if _normalize_category_filter(p.category) == wanted_category
    ]


def _default_source(products: list[Product]) -> str | None:
    counts = Counter(
        normalize_source_value(p.source)
        for p in products
        if p.source is not None and normalize_source_value(p.source)
    )
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))[0][0]


def _single_source(source: list[str] | None) -> str | None:
    values = [str(v).strip() for v in (source or []) if str(v).strip()]
    normalized_values = {normalize_source_value(v).lower() for v in values}
    if len(normalized_values) > 1 or len(values) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр source должен содержать только один источник",
        )
    return values[0] if values else None


def _select_source_context(
    products: list[Product],
    source: list[str] | None,
) -> tuple[str, list[Product], int, int]:
    requested_source = _single_source(source)
    selected_source = normalize_source_value(requested_source) if requested_source else _default_source(products)
    if not selected_source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Невозможно определить source без данных ассортимента",
        )
    source_products = [p for p in products if source_matches(selected_source, p.source)]
    if not source_products:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для выбранного source нет СКЮ",
        )
    lead_time_counts = Counter(_qty_int(p.lead_time) for p in source_products)
    lead_time_days = sorted(lead_time_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return selected_source, source_products, lead_time_days, _lead_time_months(lead_time_days)


async def _resolve_current_month(db: DBSession, user: CurrentUser) -> date:
    return await _resolve_period(db, user, None)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _effective_order_quantity(row: SupplyChainRow) -> int:
    return _qty_int(
        row.adjusted_quantity_in_mc
        if row.adjusted_quantity_in_mc is not None
        else row.recommended_quantity_in_mc
    )


async def _load_current_month_supply_rows(
    db: DBSession,
    user: CurrentUser,
    *,
    category: str | None,
    source: list[str] | None,
) -> tuple[
    date,
    str,
    int,
    int,
    dict[str, str],
    list[SupplyChainRow],
    dict[str, Product],
]:
    current_month = await _resolve_current_month(db, user)
    all_products = await _load_products(db, user)
    category_products = _apply_category_filter(all_products, category)
    selected_source, source_products, lead_time_days, lead_time_months = _select_source_context(
        category_products,
        source,
    )
    product_by_sku = {str(p.sku_code).strip(): p for p in source_products}
    selected_skus = set(product_by_sku.keys())
    stock_month = _add_months(current_month, lead_time_months - 1)
    l3_months = [_add_months(current_month, -3), _add_months(current_month, -2), _add_months(current_month, -1)]
    f3_months = [_add_months(current_month, lead_time_months + offset) for offset in range(3)]
    headers = _supply_chain_headers(
        stock_month=stock_month,
        l3_months=l3_months,
        f3_months=f3_months,
    )

    hs_stmt = select(HistoricalSalesMonthly)
    fs_stmt = select(ForecastSalesMonthly)
    pb_stmt = select(ProductBranch)
    fo_stmt = select(ForecastOrders).where(ForecastOrders.date == current_month)
    if not is_admin(user):
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
        fs_stmt = fs_stmt.where(ForecastSalesMonthly.owner_user_id == user.id)
        pb_stmt = pb_stmt.where(ProductBranch.owner_user_id == user.id)
        fo_stmt = fo_stmt.where(ForecastOrders.owner_user_id == user.id)
    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    forecast_rows = (await db.execute(fs_stmt)).scalars().all()
    product_branch_rows = (await db.execute(pb_stmt)).scalars().all()
    order_adjustment_rows = (await db.execute(fo_stmt)).scalars().all()

    hist_qty_by_sku_month: dict[tuple[str, date], float] = defaultdict(float)
    for row in hist_rows:
        sku_code = str(row.sku_code or "").strip()
        if sku_code in selected_skus:
            hist_qty_by_sku_month[(sku_code, _month_start(row.date))] += float(row.fact_quantity_in_mc or 0.0)

    forecast_qty_by_sku_branch_month: dict[tuple[str, str, date], float] = defaultdict(float)
    forecast_stock_by_sku_branch_month: dict[tuple[str, str, date], float] = defaultdict(float)
    for row in forecast_rows:
        sku_code = str(row.sku_code or "").strip()
        if sku_code not in selected_skus:
            continue
        branch_id = str(row.branch_id or "").strip()
        month = _month_start(row.date)
        qty = (
            float(row.adjusted_forecast_quantity_in_mc)
            if row.adjusted_forecast_quantity_in_mc is not None
            else float(row.baseline_forecast_quantity_in_mc or 0.0)
        )
        forecast_qty_by_sku_branch_month[(sku_code, branch_id, month)] += qty
        forecast_stock_by_sku_branch_month[(sku_code, branch_id, month)] += float(row.future_available_stock or 0.0)

    stock_norm_by_sku_branch = {
        (str(row.sku_code or "").strip(), str(row.branch_id or "").strip()): float(row.stock_norm or 0.0)
        for row in product_branch_rows
        if str(row.sku_code or "").strip() in selected_skus
    }
    branch_ids_by_sku: dict[str, set[str]] = defaultdict(set)
    for sku_code, branch_id in stock_norm_by_sku_branch:
        if branch_id:
            branch_ids_by_sku[sku_code].add(branch_id)
    for sku_code, branch_id, _month in forecast_qty_by_sku_branch_month:
        if branch_id:
            branch_ids_by_sku[sku_code].add(branch_id)

    adjustments_by_sku = {
        str(row.sku_code or "").strip(): row
        for row in order_adjustment_rows
        if str(row.sku_code or "").strip() in selected_skus
    }

    rows: list[SupplyChainRow] = []
    for sku_code, product in product_by_sku.items():
        month_prior_available_stock = sum(
            forecast_stock_by_sku_branch_month.get((sku_code, branch_id, stock_month), 0.0)
            for branch_id in branch_ids_by_sku.get(sku_code, set())
        )
        l3_values = [hist_qty_by_sku_month.get((sku_code, month), 0.0) for month in l3_months]
        f3_month_totals = [
            sum(
                forecast_qty_by_sku_branch_month.get((sku_code, branch_id, month), 0.0)
                for branch_id in branch_ids_by_sku.get(sku_code, set())
            )
            for month in f3_months
        ]
        recommended_quantity = 0.0
        for branch_id in branch_ids_by_sku.get(sku_code, set()):
            branch_f3_values = [
                forecast_qty_by_sku_branch_month.get((sku_code, branch_id, month), 0.0)
                for month in f3_months
            ]
            branch_avg_f3 = _avg(branch_f3_values)
            branch_stock = forecast_stock_by_sku_branch_month.get((sku_code, branch_id, stock_month), 0.0)
            stock_norm = stock_norm_by_sku_branch.get(
                (sku_code, branch_id),
                float(product.general_stock_norm_days or 0.0),
            )
            needed = stock_norm * (branch_avg_f3 / 30.0)
            recommended_quantity += max(needed - branch_stock, 0.0)
        recommended_int = _qty_int(recommended_quantity)
        adjustment_row = adjustments_by_sku.get(sku_code)
        adjusted_quantity = (
            _qty_int(adjustment_row.adjusted_quantity_in_mc)
            if adjustment_row is not None and adjustment_row.adjusted_quantity_in_mc is not None
            else recommended_int
        )
        rows.append(
            SupplyChainRow(
                sku_code=product.sku_code,
                sku_name=product.sku_name,
                month_prior_available_stock=_qty_int(month_prior_available_stock),
                average_l3m_quantity_in_mc=_qty_int(_avg(l3_values)),
                average_f3m_quantity_in_mc=_qty_int(_avg(f3_month_totals)),
                recommended_quantity_in_mc=recommended_int,
                adjusted_quantity_in_mc=adjusted_quantity,
            )
        )

    rows.sort(key=lambda row: (-row.recommended_quantity_in_mc, row.sku_code))
    return current_month, selected_source, lead_time_days, lead_time_months, headers, rows, product_by_sku


async def _load_supply_rows(
    db: DBSession,
    user: CurrentUser,
    period_date: date,
    category: str | None,
    source: str | None,
) -> tuple[list[SupplyChainRow], dict[str, Product], dict[str, ForecastOrders]]:
    p_stmt = select(Product)
    fo_stmt = select(ForecastOrders).where(ForecastOrders.date == period_date)
    if not is_admin(user):
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
        fo_stmt = fo_stmt.where(ForecastOrders.owner_user_id == user.id)
    products = (await db.execute(p_stmt)).scalars().all()
    if category:
        wanted_category = _normalize_category_filter(category)
        products = [
            p
            for p in products
            if _normalize_category_filter(p.category) == wanted_category
        ]
    if source:
        products = [p for p in products if source_matches(source, p.source)]
    product_by_sku = {str(p.sku_code).strip(): p for p in products}
    fo_rows = (await db.execute(fo_stmt.order_by(ForecastOrders.sku_code))).scalars().all()
    fo_by_sku = {
        str(r.sku_code or "").strip(): r
        for r in fo_rows
        if str(r.sku_code or "").strip() in product_by_sku
    }

    rows = [
        SupplyChainRow(
            sku_code=product_by_sku[str(r.sku_code or "").strip()].sku_code,
            sku_name=product_by_sku[str(r.sku_code or "").strip()].sku_name,
            month_prior_available_stock=_qty_int(r.month_prior_available_stock),
            average_l3m_quantity_in_mc=_qty_int(r.average_l3m_quantity_in_mc),
            average_f3m_quantity_in_mc=_qty_int(r.average_f3m_quantity_in_mc),
            recommended_quantity_in_mc=_qty_int(r.recommended_quantity_in_mc),
            adjusted_quantity_in_mc=(
                _qty_int(r.adjusted_quantity_in_mc)
                if r.adjusted_quantity_in_mc is not None
                else None
            ),
        )
        for r in fo_rows
        if str(r.sku_code or "").strip() in product_by_sku
    ]
    rows.sort(key=lambda x: x.sku_code)
    return rows, product_by_sku, fo_by_sku


async def _compute_supply_totals(
    db: DBSession,
    user: CurrentUser,
    period_date: date,
    product_by_sku: dict[str, Product],
    fo_by_sku: dict[str, ForecastOrders],
) -> tuple[float, int, float, float]:
    if not fo_by_sku:
        return 0.0, 0.0, 0.0, 0.0

    price_stmt = select(PriceList)
    if not is_admin(user):
        price_stmt = price_stmt.where(PriceList.owner_user_id == user.id)
    prices = (await db.execute(price_stmt)).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for p in prices:
        prices_by_sku.setdefault(str(p.sku_code or "").strip(), []).append(p)

    total_sum = 0.0
    total_quantity = 0
    total_gross_weight = 0.0
    total_volume = 0.0
    for sku_code, fo in fo_by_sku.items():
        product = product_by_sku.get(sku_code)
        if not product:
            continue
        quantity = (
            _qty_int(fo.adjusted_quantity_in_mc)
            if fo.adjusted_quantity_in_mc is not None
            else _qty_int(fo.recommended_quantity_in_mc)
        )
        dsp = _closest_dsp_for_period(prices_by_sku.get(sku_code, []), period_date)
        total_quantity += quantity
        total_sum += quantity * float(product.pieces_in_master_carton) * dsp
        total_gross_weight += quantity * float(product.master_carton_gross_weight_kg)
        total_volume += quantity * float(product.master_carton_volume_cbm)

    return (
        round(total_sum, 2),
        int(total_quantity),
        round(total_gross_weight, 2),
        round(total_volume, 2),
    )


async def _compute_supply_totals_from_rows(
    db: DBSession,
    user: CurrentUser,
    *,
    period_date: date,
    rows: list[SupplyChainRow],
    product_by_sku: dict[str, Product],
    view_type: str = "DSP",
) -> tuple[float, int, float, float]:
    if not rows:
        return 0.0, 0, 0.0, 0.0

    metric = _normalize_view_type(view_type)
    price_stmt = select(PriceList)
    if not is_admin(user):
        price_stmt = price_stmt.where(PriceList.owner_user_id == user.id)
    prices = (await db.execute(price_stmt)).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for price in prices:
        prices_by_sku[str(price.sku_code or "").strip()].append(price)

    total_sum = 0.0
    total_quantity = 0
    total_gross_weight = 0.0
    total_volume = 0.0
    for row in rows:
        sku_code = str(row.sku_code).strip()
        product = product_by_sku.get(sku_code)
        if product is None:
            continue
        quantity = _effective_order_quantity(row)
        price = _price_for_period(prices_by_sku.get(sku_code, []), period_date, metric)
        total_quantity += quantity
        total_sum += quantity * float(product.pieces_in_master_carton or 0.0) * price
        total_gross_weight += quantity * float(product.master_carton_gross_weight_kg or 0.0)
        total_volume += quantity * float(product.master_carton_volume_cbm or 0.0)

    return (
        round(total_sum, 2),
        int(total_quantity),
        round(total_gross_weight, 2),
        round(total_volume, 2),
    )


@router.get("/filter-options", response_model=SupplyChainFilterOptionsResponse)
async def get_supply_chain_filter_options(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> SupplyChainFilterOptionsResponse:
    _ = period, date_from, date_to
    products = await _load_products(db, user)

    # Global options for user-owned products.
    categories = sorted(
        {
            str(p.category).strip()
            for p in products
            if p.category is not None and str(p.category).strip()
        }
    )
    sources = sorted(
        {
            normalize_source_value(p.source)
            for p in products
            if p.source is not None and normalize_source_value(p.source)
        }
    )

    return SupplyChainFilterOptionsResponse(
        categories=categories,
        sources=sources,
        default_source=_default_source(products),
    )


@router.get("/information-icons/", response_model=SupplyChainInformationIconsResponse, include_in_schema=False)
@router.get("/information-icons", response_model=SupplyChainInformationIconsResponse)
async def get_supply_chain_information_icons(
    user: CurrentUser,
) -> SupplyChainInformationIconsResponse:
    _ = user
    return SupplyChainInformationIconsResponse(
        month_prior_available_stock="Наличие товара -1 от планируемого месяца",
        average_l3m_quantity_in_mc="Средние продажи за последние 3 мес",
        average_f3m_quantity_in_mc="Средние продажи за будущие 3 мес",
    )


@router.get("", response_model=SupplyChainListResponse, include_in_schema=False)
@router.get("/", response_model=SupplyChainListResponse)
async def get_supply_chain_view(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    category: str | None = Query(None),
    source: list[str] | None = Query(None),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> SupplyChainListResponse:
    _ = period, date_from, date_to
    (
        current_month,
        selected_source,
        lead_time_days,
        lead_time_months,
        headers,
        rows,
        product_by_sku,
    ) = await _load_current_month_supply_rows(db, user, category=category, source=source)
    filter_options = SupplyChainFilterOptions(
        sku_code=sorted({str(r.sku_code).strip() for r in rows if str(r.sku_code).strip()}),
        sku_name=sorted({str(r.sku_name).strip() for r in rows if str(r.sku_name).strip()}),
    )
    filtered_rows = rows
    if sku_code:
        sku_code_values = {str(v).strip() for v in sku_code if str(v).strip()}
        filtered_rows = [
            r for r in filtered_rows if str(r.sku_code).strip() in sku_code_values
        ]
    if sku_name:
        sku_name_values = {str(v).strip() for v in sku_name if str(v).strip()}
        filtered_rows = [
            r for r in filtered_rows if str(r.sku_name).strip() in sku_name_values
        ]

    product_by_sku_for_totals = {
        str(row.sku_code).strip(): product_by_sku[str(row.sku_code).strip()]
        for row in filtered_rows
        if str(row.sku_code).strip() in product_by_sku
    }
    total_sum, total_quantity, total_gross_weight, total_volume = await _compute_supply_totals_from_rows(
        db=db,
        user=user,
        period_date=current_month,
        rows=filtered_rows,
        product_by_sku=product_by_sku_for_totals,
        view_type="DSP",
    )
    items, total_items, total_pages = _paginate(filtered_rows, page=page, page_size=page_size)
    return SupplyChainListResponse(
        period=current_month.strftime("%Y-%m"),
        current_month=current_month.strftime("%Y-%m"),
        source=selected_source,
        lead_time_days=lead_time_days,
        lead_time_months=lead_time_months,
        headers=headers,
        items=items,
        total_sum=total_sum,
        total_quantity_in_mc=total_quantity,
        total_gross_weight=total_gross_weight,
        total_volume=total_volume,
        total_items=total_items,
        total_pages=total_pages,
        filter_options=filter_options,
    )


@router.patch("", include_in_schema=False)
@router.patch("/")
async def update_adjusted_quantities(
    db: DBSession,
    user: CurrentUser,
    payload: SupplyChainAdjustRequest,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> dict:
    _ = period, date_from, date_to
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Список updates не может быть пустым",
        )
    period_date = await _resolve_current_month(db, user)

    p_stmt = select(Product)
    if not is_admin(user):
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(p_stmt)).scalars().all()
    sku_code_by_owner: dict[tuple[int, str], str] = {
        (p.owner_user_id, str(p.sku_code).strip()): str(p.sku_code).strip() for p in products
    }
    owners = sorted({k[0] for k in sku_code_by_owner.keys()})

    updated = 0
    for row in payload.updates:
        for owner_id in owners:
            normalized_sku_code = sku_code_by_owner.get((owner_id, str(row.sku_code).strip()))
            if not normalized_sku_code:
                continue
            stmt = (
                update(ForecastOrders)
                .where(
                    ForecastOrders.owner_user_id == owner_id,
                    ForecastOrders.sku_code == normalized_sku_code,
                    ForecastOrders.date == period_date,
                )
                .values(
                    adjusted_quantity_in_mc=(
                        int(row.adjusted_quantity_in_mc)
                        if row.adjusted_quantity_in_mc is not None
                        else None
                    ),
                    sku_code=row.sku_code,
                )
            )
            result = await db.execute(stmt)
            updated += int(result.rowcount or 0)
    await db.commit()
    return {"rows_updated": updated}


@router.get("/download")
async def download_supply_chain(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    category: str | None = Query(None),
    source: list[str] | None = Query(None),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
):
    response = await get_supply_chain_view(
        db=db,
        user=user,
        period=period,
        date_from=date_from,
        date_to=date_to,
        category=category,
        source=source,
        sku_code=sku_code,
        sku_name=sku_name,
        page=1,
        page_size="all",
    )
    rows = response.items
    export_rows = [
        {
            "sku_code": r.sku_code,
            "sku_name": r.sku_name,
            "month_prior_available_stock": r.month_prior_available_stock,
            "average_l3m_quantity_in_mc": r.average_l3m_quantity_in_mc,
            "average_f3m_quantity_in_mc": r.average_f3m_quantity_in_mc,
            "recommended_quantity_in_mc": r.recommended_quantity_in_mc,
            "adjusted_quantity_in_mc": r.adjusted_quantity_in_mc,
        }
        for r in rows
    ]
    output = BytesIO()
    pd.DataFrame(export_rows).rename(columns=response.headers).to_excel(
        output, index=False, sheet_name="supply_chain"
    )
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="supply_chain.xlsx"'},
    )


@router.get("/summary", response_model=SupplyChainSummary)
async def get_supply_chain_summary(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    category: str | None = Query(None),
    source: list[str] | None = Query(None),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
    view_type: str = Query("DSP", description="DSP or Invoice price"),
) -> SupplyChainSummary:
    _ = period, date_from, date_to
    metric = _normalize_view_type(view_type)
    (
        current_month,
        selected_source,
        lead_time_days,
        lead_time_months,
        _headers,
        rows,
        product_by_sku,
    ) = await _load_current_month_supply_rows(db, user, category=category, source=source)
    filtered_rows = rows
    if sku_code:
        sku_code_values = {str(v).strip() for v in sku_code if str(v).strip()}
        filtered_rows = [
            r for r in filtered_rows if str(r.sku_code).strip() in sku_code_values
        ]
    if sku_name:
        sku_name_values = {str(v).strip() for v in sku_name if str(v).strip()}
        filtered_rows = [
            r for r in filtered_rows if str(r.sku_name).strip() in sku_name_values
        ]

    allowed_sku_codes = {str(r.sku_code).strip() for r in filtered_rows}
    product_by_sku = {
        sku_code_key: product
        for sku_code_key, product in product_by_sku.items()
        if sku_code_key in allowed_sku_codes
    }

    total_sum, total_quantity, total_gross_weight, total_volume = await _compute_supply_totals_from_rows(
        db=db,
        user=user,
        period_date=current_month,
        rows=filtered_rows,
        product_by_sku=product_by_sku,
        view_type=metric,
    )

    return SupplyChainSummary(
        period=current_month.strftime("%Y-%m"),
        current_month=current_month.strftime("%Y-%m"),
        source=selected_source,
        lead_time_days=lead_time_days,
        lead_time_months=lead_time_months,
        view_type="Invoice price" if metric == "invoice price" else "DSP",
        total_sum=total_sum,
        total_quantity=total_quantity,
        total_gross_weight=total_gross_weight,
        total_volume=total_volume,
    )

