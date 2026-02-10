from datetime import date
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import (
    Assortment,
    BranchStockNorm,
    HistoricalSalesMonthly,
    PlacedOrder,
    PriceList,
)

router = APIRouter(prefix="/datasets", tags=["datasets"])


class AssortmentRow(BaseModel):
    sku_id: str
    sku_code: str | None = None
    mother_sku: str | None = None
    barcode: str | None = None
    sku_name: str
    sku_name_local: str | None = None
    pieces_in_master_carton: float | None = None
    master_carton_volume_cbm: float | None = None
    master_carton_gross_weight_kg: float | None = None
    master_carton_net_weight_kg: float | None = None
    lead_time_days: float | None = None
    source: str | None = None
    general_stock_norm_days: float | None = None
    status: str | None = None
    brand: str | None = None
    category: str | None = None
    sub_category: str | None = None
    sub_line: str | None = None
    line: str | None = None

    model_config = {"from_attributes": True}


class BranchStockNormRow(BaseModel):
    branch_id: str
    sku_id: str
    current_stock: float | None = None
    stock_norm_days: float | None = None

    model_config = {"from_attributes": True}


class PriceListRow(BaseModel):
    sku_id: str
    date: date
    invoice_price: float | None = None
    dsp: float | None = None

    model_config = {"from_attributes": True}


class HistoricalSalesMonthlyRow(BaseModel):
    sku_id: str
    branch_id: str
    date: date
    fact_quantity_in_mc: float | None = None
    target_quantity_in_mc: float | None = None
    past_available_stock: float | None = None

    model_config = {"from_attributes": True}


class PlacedOrderRow(BaseModel):
    order_id: str
    sku_id: str
    order_name: str | None = None
    creation_date: date
    receival_date: date | None = None
    quantity_in_mc: float | None = None
    gross_weight_kg: float | None = None
    volume_cbm: float | None = None
    amount_kzt: float | None = None
    status: str | None = None

    model_config = {"from_attributes": True}


@router.get("/assortment", response_model=List[AssortmentRow])
async def get_assortment_dataset(
    db: DBSession,
    _user: CurrentUser,
) -> list[Assortment]:
    result = await db.execute(select(Assortment))
    return list(result.scalars().all())


@router.get("/branch-stock-norm", response_model=List[BranchStockNormRow])
async def get_branch_stock_norm_dataset(
    db: DBSession,
    _user: CurrentUser,
) -> list[BranchStockNorm]:
    result = await db.execute(select(BranchStockNorm))
    return list(result.scalars().all())


@router.get("/price-list", response_model=List[PriceListRow])
async def get_price_list_dataset(
    db: DBSession,
    _user: CurrentUser,
) -> list[PriceList]:
    result = await db.execute(select(PriceList))
    return list(result.scalars().all())


@router.get("/historical-sales-monthly", response_model=List[HistoricalSalesMonthlyRow])
async def get_historical_sales_monthly_dataset(
    db: DBSession,
    _user: CurrentUser,
) -> list[HistoricalSalesMonthly]:
    result = await db.execute(select(HistoricalSalesMonthly))
    return list(result.scalars().all())


@router.get("/placed-orders", response_model=List[PlacedOrderRow])
async def get_placed_orders_dataset(
    db: DBSession,
    _user: CurrentUser,
) -> list[PlacedOrder]:
    result = await db.execute(select(PlacedOrder))
    return list(result.scalars().all())

