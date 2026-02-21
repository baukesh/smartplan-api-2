from datetime import date
from io import BytesIO

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import HistoricalSalesMonthly, PriceList, Product
from app.models.derived import ForecastOrders

router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


class SupplyChainRow(BaseModel):
    sku_code: str
    sku_name: str
    month_prior_available_stock: float
    average_l3m_quantity_in_mc: float
    average_f3m_quantity_in_mc: float
    recommended_quantity_in_mc: float
    adjusted_quantity_in_mc: float | None = None


class SupplyChainListResponse(BaseModel):
    period: str
    items: list[SupplyChainRow]
    total_items: int
    total_pages: int


class SupplyChainAdjustRow(BaseModel):
    sku_code: str
    adjusted_quantity_in_mc: float | None = None


class SupplyChainAdjustRequest(BaseModel):
    updates: list[SupplyChainAdjustRow]


class SupplyChainSummary(BaseModel):
    period: str
    total_sum: float
    total_gross_weight: float
    total_volume: float


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _period_to_date(period: str) -> date:
    try:
        year, month = period.split("-")
        return date(int(year), int(month), 1)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="period must be in YYYY-MM format",
        ) from exc


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be one of: 10, 50, 100, all",
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
            detail="Cannot derive default period without historical_sales_monthly data",
        )
    if max_hist_date.month == 12:
        return date(max_hist_date.year + 1, 1, 1)
    return date(max_hist_date.year, max_hist_date.month + 1, 1)


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
    if category:
        p_stmt = p_stmt.where(Product.category == category)
    if source:
        p_stmt = p_stmt.where(Product.source == source)

    products = (await db.execute(p_stmt)).scalars().all()
    product_by_sku = {p.sku_id: p for p in products}
    fo_rows = (await db.execute(fo_stmt.order_by(ForecastOrders.sku_id))).scalars().all()
    fo_by_sku = {r.sku_id: r for r in fo_rows if r.sku_id in product_by_sku}

    rows = [
        SupplyChainRow(
            sku_code=product_by_sku[r.sku_id].sku_code,
            sku_name=product_by_sku[r.sku_id].sku_name,
            month_prior_available_stock=float(r.month_prior_available_stock),
            average_l3m_quantity_in_mc=float(r.average_l3m_quantity_in_mc),
            average_f3m_quantity_in_mc=float(r.average_f3m_quantity_in_mc),
            recommended_quantity_in_mc=float(r.recommended_quantity_in_mc),
            adjusted_quantity_in_mc=float(r.adjusted_quantity_in_mc) if r.adjusted_quantity_in_mc is not None else None,
        )
        for r in fo_rows
        if r.sku_id in product_by_sku
    ]
    rows.sort(key=lambda x: x.sku_code)
    return rows, product_by_sku, fo_by_sku


@router.get("/", response_model=SupplyChainListResponse)
async def get_supply_chain_view(
    db: DBSession,
    user: CurrentUser,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
    category: str | None = Query(None),
    source: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> SupplyChainListResponse:
    period_date = await _resolve_period(db, user, period)
    rows, _, _ = await _load_supply_rows(db, user, period_date, category, source)
    items, total_items, total_pages = _paginate(rows, page=page, page_size=page_size)
    return SupplyChainListResponse(
        period=period_date.strftime("%Y-%m"),
        items=items,
        total_items=total_items,
        total_pages=total_pages,
    )


@router.patch("/")
async def update_adjusted_quantities(
    db: DBSession,
    user: CurrentUser,
    payload: SupplyChainAdjustRequest,
    period: str | None = Query(None, description="Planning period in YYYY-MM"),
) -> dict:
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="updates cannot be empty",
        )
    period_date = await _resolve_period(db, user, period)

    p_stmt = select(Product)
    if not is_admin(user):
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(p_stmt)).scalars().all()
    sku_id_by_code: dict[tuple[int, str], str] = {(p.owner_user_id, p.sku_code): p.sku_id for p in products}
    owners = sorted({k[0] for k in sku_id_by_code.keys()})

    updated = 0
    for row in payload.updates:
        for owner_id in owners:
            sku_id = sku_id_by_code.get((owner_id, row.sku_code))
            if not sku_id:
                continue
            stmt = (
                update(ForecastOrders)
                .where(
                    ForecastOrders.owner_user_id == owner_id,
                    ForecastOrders.sku_id == sku_id,
                    ForecastOrders.date == period_date,
                )
                .values(adjusted_quantity_in_mc=row.adjusted_quantity_in_mc)
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
    category: str | None = Query(None),
    source: str | None = Query(None),
):
    period_date = await _resolve_period(db, user, period)
    rows, _, _ = await _load_supply_rows(db, user, period_date, category, source)
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
    category: str | None = Query(None),
    source: str | None = Query(None),
) -> SupplyChainSummary:
    period_date = await _resolve_period(db, user, period)
    _, product_by_sku, fo_by_sku = await _load_supply_rows(db, user, period_date, category, source)

    if not fo_by_sku:
        return SupplyChainSummary(
            period=period_date.strftime("%Y-%m"),
            total_sum=0.0,
            total_gross_weight=0.0,
            total_volume=0.0,
        )

    price_stmt = select(PriceList)
    if not is_admin(user):
        price_stmt = price_stmt.where(PriceList.owner_user_id == user.id)
    prices = (await db.execute(price_stmt)).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for p in prices:
        prices_by_sku.setdefault(p.sku_id, []).append(p)

    total_sum = 0.0
    total_gross_weight = 0.0
    total_volume = 0.0
    for sku_id, fo in fo_by_sku.items():
        product = product_by_sku.get(sku_id)
        if not product:
            continue
        quantity = (
            float(fo.adjusted_quantity_in_mc)
            if fo.adjusted_quantity_in_mc is not None
            else float(fo.recommended_quantity_in_mc)
        )
        dsp = _closest_dsp_for_period(prices_by_sku.get(sku_id, []), period_date)
        total_sum += quantity * float(product.pieces_in_master_carton) * dsp
        total_gross_weight += quantity * float(product.master_carton_gross_weight_kg)
        total_volume += quantity * float(product.master_carton_volume_cbm)

    return SupplyChainSummary(
        period=period_date.strftime("%Y-%m"),
        total_sum=round(total_sum, 2),
        total_gross_weight=round(total_gross_weight, 2),
        total_volume=round(total_volume, 2),
    )

