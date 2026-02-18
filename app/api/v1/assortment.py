from datetime import date
from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import Product, ProductBranch, PriceList

router = APIRouter(prefix="/assortment", tags=["assortment"])


class AssortmentItem(BaseModel):
    sku_id: str
    sku_code: str
    sku_name: str
    status: str
    brand: str
    category: str

    model_config = {"from_attributes": True}


class BranchStockNormRow(BaseModel):
    branch_id: str
    sku_id: str
    current_stock: float
    stock_norm: float

    model_config = {"from_attributes": True}


class PriceListRow(BaseModel):
    sku_id: str
    date: date
    invoice_price: float
    dsp: float

    model_config = {"from_attributes": True}


@router.get("/items", response_model=List[AssortmentItem])
async def list_assortment(
    db: DBSession,
    _user: CurrentUser,
    status: str | None = Query(None),
) -> list[Product]:
    stmt = select(Product)
    if status:
        stmt = stmt.where(Product.status == status)
    result = await db.execute(stmt.order_by(Product.sku_id))
    return list(result.scalars().all())


@router.get("/branch-matrix", response_model=List[BranchStockNormRow])
async def get_branch_stock_matrix(
    db: DBSession,
    _user: CurrentUser,
) -> list[ProductBranch]:
    result = await db.execute(select(ProductBranch))
    return list(result.scalars().all())


@router.get("/price-list", response_model=List[PriceListRow])
async def get_price_list(
    db: DBSession,
    _user: CurrentUser,
) -> list[PriceList]:
    result = await db.execute(select(PriceList))
    return list(result.scalars().all())
