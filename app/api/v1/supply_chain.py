from datetime import date
from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import Product
from app.models.derived import ForecastOrders

router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])


class SupplyChainRow(BaseModel):
    sku_id: str
    date: date
    sku_name: str | None = None
    month_prior_available_stock: float
    average_l3m_quantity_in_mc: float
    average_f3m_quantity_in_mc: float
    recommended_quantity_in_mc: float
    adjusted_quantity_in_mc: float | None = None


class SupplyChainResponse(BaseModel):
    period: str
    rows: List[SupplyChainRow]


@router.get("/", response_model=SupplyChainResponse)
async def get_supply_chain_view(
    db: DBSession,
    _user: CurrentUser,
    period: str = Query(..., description="Planning period, e.g. 2025-12"),
    category: str | None = Query(None),
    source: str | None = Query(None),
) -> SupplyChainResponse:
    year, month = period.split("-")
    target_date = date(int(year), int(month), 1)

    product_rows = {p.sku_id: p for p in (await db.execute(select(Product))).scalars().all()}
    fo_rows = (
        await db.execute(
            select(ForecastOrders).where(ForecastOrders.date == target_date).order_by(
                ForecastOrders.sku_id
            )
        )
    ).scalars().all()

    rows = [
        SupplyChainRow(
            sku_id=r.sku_id,
            date=r.date,
            sku_name=product_rows.get(r.sku_id).sku_name if r.sku_id in product_rows else None,
            month_prior_available_stock=r.month_prior_available_stock,
            average_l3m_quantity_in_mc=r.average_l3m_quantity_in_mc,
            average_f3m_quantity_in_mc=r.average_f3m_quantity_in_mc,
            recommended_quantity_in_mc=r.recommended_quantity_in_mc,
            adjusted_quantity_in_mc=r.adjusted_quantity_in_mc,
        )
        for r in fo_rows
    ]
    return SupplyChainResponse(period=period, rows=rows)

