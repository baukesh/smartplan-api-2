from datetime import date
from io import BytesIO
import re

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin
from app.core.source_normalization import normalize_source_value, source_matches
from app.models.data_uploads import HistoricalSalesMonthly, PriceList, Product
from app.models.derived import ForecastOrders

router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


class SupplyChainRow(BaseModel):
    sku_code: str
    sku_name: str
    month_prior_available_stock: float
    average_l3m_quantity_in_mc: int
    average_f3m_quantity_in_mc: int
    recommended_quantity_in_mc: int
    adjusted_quantity_in_mc: int | None = None


class SupplyChainListResponse(BaseModel):
    period: str
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
    total_sum: float
    total_quantity: int
    total_gross_weight: float
    total_volume: float


class SupplyChainFilterOptionsResponse(BaseModel):
    categories: list[str]
    sources: list[str]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _qty_int(value: float | None) -> int:
    return int(round(float(value or 0.0)))


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
            month_prior_available_stock=float(r.month_prior_available_stock),
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


@router.get("/filter-options", response_model=SupplyChainFilterOptionsResponse)
async def get_supply_chain_filter_options(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> SupplyChainFilterOptionsResponse:
    has_period_context = bool(period or date_from or date_to)
    period_date: date | None = None
    if has_period_context:
        period_date = await _resolve_period_from_args(db, user, period, date_from, date_to)

    p_stmt = select(Product)
    if not is_admin(user):
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(p_stmt)).scalars().all()

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

    # Optional context-aware narrowing by selected planning period:
    # keep only options present in forecast rows for that month.
    if period_date is not None:
        fo_stmt = select(ForecastOrders).where(ForecastOrders.date == period_date)
        if not is_admin(user):
            fo_stmt = fo_stmt.where(ForecastOrders.owner_user_id == user.id)
        forecast_rows = (await db.execute(fo_stmt)).scalars().all()
        if forecast_rows:
            sku_codes = {str(r.sku_code or "").strip() for r in forecast_rows}
            if sku_codes:
                period_products = [p for p in products if str(p.sku_code).strip() in sku_codes]
                categories = sorted(
                    {
                        str(p.category).strip()
                        for p in period_products
                        if p.category is not None and str(p.category).strip()
                    }
                )
                sources = sorted(
                    {
                        normalize_source_value(p.source)
                        for p in period_products
                        if p.source is not None and normalize_source_value(p.source)
                    }
                )

    return SupplyChainFilterOptionsResponse(categories=categories, sources=sources)


@router.get("", response_model=SupplyChainListResponse, include_in_schema=False)
@router.get("/", response_model=SupplyChainListResponse)
async def get_supply_chain_view(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    category: str | None = Query(None),
    source: str | None = Query(None),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> SupplyChainListResponse:
    period_date = await _resolve_period_from_args(db, user, period, date_from, date_to)
    rows, product_by_sku, fo_by_sku = await _load_supply_rows(db, user, period_date, category, source)
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

    if sku_code or sku_name:
        allowed_sku_codes = {
            sku_code_key
            for sku_code_key, product in product_by_sku.items()
            if any(
                str(r.sku_code).strip() == str(product.sku_code).strip()
                and str(r.sku_name).strip() == str(product.sku_name).strip()
                for r in filtered_rows
            )
        }
        product_by_sku_for_totals = {
            sku_code_key: product
            for sku_code_key, product in product_by_sku.items()
            if sku_code_key in allowed_sku_codes
        }
        fo_by_sku_for_totals = {
            sku_code_key: row
            for sku_code_key, row in fo_by_sku.items()
            if sku_code_key in allowed_sku_codes
        }
    else:
        product_by_sku_for_totals = product_by_sku
        fo_by_sku_for_totals = fo_by_sku

    total_sum, total_quantity, total_gross_weight, total_volume = await _compute_supply_totals(
        db=db,
        user=user,
        period_date=period_date,
        product_by_sku=product_by_sku_for_totals,
        fo_by_sku=fo_by_sku_for_totals,
    )
    items, total_items, total_pages = _paginate(filtered_rows, page=page, page_size=page_size)
    return SupplyChainListResponse(
        period=period_date.strftime("%Y-%m"),
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
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Список updates не может быть пустым",
        )
    if not period and not date_from and not date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Для PATCH /supply-chain требуется period (или date_from/date_to), "
                "чтобы не обновить другой месяц планирования"
            ),
        )
    period_date = await _resolve_period_from_args(db, user, period, date_from, date_to)

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
    source: str | None = Query(None),
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
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="supply_chain")
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
    source: str | None = Query(None),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
) -> SupplyChainSummary:
    period_date = await _resolve_period_from_args(db, user, period, date_from, date_to)
    rows, product_by_sku, fo_by_sku = await _load_supply_rows(db, user, period_date, category, source)
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

    if sku_code or sku_name:
        allowed_sku_codes = {str(r.sku_code).strip() for r in filtered_rows}
        product_by_sku = {
            sku_code_key: product
            for sku_code_key, product in product_by_sku.items()
            if sku_code_key in allowed_sku_codes
        }
        fo_by_sku = {
            sku_code_key: row
            for sku_code_key, row in fo_by_sku.items()
            if sku_code_key in allowed_sku_codes
        }

    total_sum, total_quantity, total_gross_weight, total_volume = await _compute_supply_totals(
        db=db,
        user=user,
        period_date=period_date,
        product_by_sku=product_by_sku,
        fo_by_sku=fo_by_sku,
    )

    return SupplyChainSummary(
        period=period_date.strftime("%Y-%m"),
        total_sum=total_sum,
        total_quantity=total_quantity,
        total_gross_weight=total_gross_weight,
        total_volume=total_volume,
    )

