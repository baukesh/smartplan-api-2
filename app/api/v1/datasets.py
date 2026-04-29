from datetime import date
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import (
    Branch,
    Product,
    ProductBranch,
    HistoricalSalesMonthly,
    PlacedOrder,
    PriceList,
)

router = APIRouter(prefix="/datasets", tags=["datasets"])


class AssortmentRow(BaseModel):
    sku_code: str
    mother_sku: str | None = None
    barcode: str | None = None
    sku_name: str
    sku_name_local: str | None = None
    pieces_in_master_carton: float | None = None
    master_carton_volume_cbm: float | None = None
    master_carton_gross_weight_kg: float | None = None
    master_carton_net_weight_kg: float | None = None
    lead_time: float | None = None
    source: str | None = None
    general_stock_norm_days: float | None = None
    status: str | None = None
    brand: str | None = None
    category: str | None = None
    sub_category: str | None = None
    sub_line: str | None = None

    model_config = {"from_attributes": True}


class BranchStockNormRow(BaseModel):
    branch_name: str
    sku_code: str
    current_stock: float | None = None
    stock_norm: float | None = None

    model_config = {"from_attributes": True}


class PriceListRow(BaseModel):
    sku_code: str
    date: date
    invoice_price: float | None = None
    dsp: float | None = None

    model_config = {"from_attributes": True}


class HistoricalSalesMonthlyRow(BaseModel):
    sku_code: str
    hub_name: str | None = None
    branch_name: str
    date: date
    fact_quantity_in_mc: float | None = None
    target_quantity_in_mc: float | None = None
    past_available_stock: float | None = None

    model_config = {"from_attributes": True}


class PlacedOrderRow(BaseModel):
    order_id: str
    sku_code: str
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
    user: CurrentUser,
) -> list[Product]:
    stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(Product.owner_user_id == user.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/branch-stock-norm", response_model=List[BranchStockNormRow])
async def get_branch_stock_norm_dataset(
    db: DBSession,
    user: CurrentUser,
) -> list[BranchStockNormRow]:
    stmt = select(ProductBranch)
    if not is_admin(user):
        stmt = stmt.where(ProductBranch.owner_user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    branch_stmt = select(Branch)
    if not is_admin(user):
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
    branches = (await db.execute(branch_stmt)).scalars().all()
    branch_map = {(b.owner_user_id, b.branch_id): b.branch_name for b in branches}
    return [
        BranchStockNormRow(
            branch_name=branch_map.get((r.owner_user_id, r.branch_id), r.branch_id),
            sku_code=str(r.sku_code or ""),
            current_stock=r.current_stock,
            stock_norm=r.stock_norm,
        )
        for r in rows
    ]


@router.get("/price-list", response_model=List[PriceListRow])
async def get_price_list_dataset(
    db: DBSession,
    user: CurrentUser,
) -> list[PriceList]:
    stmt = select(PriceList)
    if not is_admin(user):
        stmt = stmt.where(PriceList.owner_user_id == user.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/historical-sales-monthly", response_model=List[HistoricalSalesMonthlyRow])
async def get_historical_sales_monthly_dataset(
    db: DBSession,
    user: CurrentUser,
) -> list[HistoricalSalesMonthlyRow]:
    stmt = select(HistoricalSalesMonthly)
    if not is_admin(user):
        stmt = stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    branch_stmt = select(Branch)
    if not is_admin(user):
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
    branches = (await db.execute(branch_stmt)).scalars().all()
    branch_map = {(b.owner_user_id, b.branch_id): b.branch_name for b in branches}
    return [
        HistoricalSalesMonthlyRow(
            sku_code=str(r.sku_code or ""),
            hub_name=str(r.hub_name or ""),
            branch_name=branch_map.get((r.owner_user_id, r.branch_id), r.branch_id),
            date=r.date,
            fact_quantity_in_mc=r.fact_quantity_in_mc,
            target_quantity_in_mc=r.target_quantity_in_mc,
            past_available_stock=r.past_available_stock,
        )
        for r in rows
    ]


@router.get("/placed-orders", response_model=List[PlacedOrderRow])
async def get_placed_orders_dataset(
    db: DBSession,
    user: CurrentUser,
) -> list[PlacedOrderRow]:
    stmt = select(PlacedOrder)
    if not is_admin(user):
        stmt = stmt.where(PlacedOrder.owner_user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        PlacedOrderRow(
            order_id=r.order_id,
            sku_code=str(r.sku_code or ""),
            order_name=r.order_name,
            creation_date=r.creation_date,
            receival_date=r.receival_date,
            quantity_in_mc=r.quantity_in_mc,
            gross_weight_kg=r.gross_weight_kg,
            volume_cbm=r.volume_cbm,
            amount_kzt=r.amount_kzt,
            status=r.status,
        )
        for r in rows
    ]

