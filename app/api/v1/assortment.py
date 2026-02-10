from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import Assortment, BranchStockNorm, PriceList

router = APIRouter(prefix="/assortment", tags=["assortment"])


class AssortmentItem(BaseModel):
    sku_id: str
    sku_code: str | None = None
    sku_name: str
    status: str | None = None
    brand: str | None = None
    category: str | None = None

    model_config = {"from_attributes": True}


class BranchStockNormRow(BaseModel):
    branch_id: str
    sku_id: str
    current_stock: float | None = None
    stock_norm_days: float | None = None

    model_config = {"from_attributes": True}


class PriceListRow(BaseModel):
    sku_id: str
    date: str
    invoice_price: float | None = None
    dsp: float | None = None

    model_config = {"from_attributes": True}


@router.get("/items", response_model=List[AssortmentItem])
async def list_assortment(
    db: DBSession,
    _user: CurrentUser,
    status: str | None = Query(None),
) -> list[Assortment]:
    stmt = select(Assortment)
    if status:
        stmt = stmt.where(Assortment.status == status)
    result = await db.execute(stmt.order_by(Assortment.sku_id))
    return list(result.scalars().all())


@router.get("/branch-matrix", response_model=List[BranchStockNormRow])
async def get_branch_stock_matrix(
    db: DBSession,
    _user: CurrentUser,
) -> list[BranchStockNorm]:
    result = await db.execute(select(BranchStockNorm))
    return list(result.scalars().all())


@router.get("/price-list", response_model=List[PriceListRow])
async def get_price_list(
    db: DBSession,
    _user: CurrentUser,
) -> list[PriceList]:
    result = await db.execute(select(PriceList))
    return list(result.scalars().all())

