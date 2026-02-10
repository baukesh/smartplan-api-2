from datetime import date
from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import PlacedOrder

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderRow(BaseModel):
    order_id: str
    sku_id: str
    order_name: str | None = None
    creation_date: date
    receival_date: date | None = None
    quantity_in_mc: float | None = None
    amount_kzt: float | None = None
    status: str | None = None

    model_config = {"from_attributes": True}


@router.get("/", response_model=List[OrderRow])
async def list_orders(
    db: DBSession,
    _user: CurrentUser,
    status: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> list[PlacedOrder]:
    stmt = select(PlacedOrder)
    if status:
        stmt = stmt.where(PlacedOrder.status == status)
    if date_from:
        stmt = stmt.where(PlacedOrder.creation_date >= date_from)
    if date_to:
        stmt = stmt.where(PlacedOrder.creation_date <= date_to)
    result = await db.execute(stmt.order_by(PlacedOrder.creation_date.desc()))
    return list(result.scalars().all())

