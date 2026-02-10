from datetime import date
from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentUser, DBSession

router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])


class SupplyChainRow(BaseModel):
    sku_id: str
    sku_name: str | None = None
    avg_sales_last_3m: float | None = None
    avg_sales_future_3m: float | None = None
    recommended_quantity: float | None = None
    final_quantity: float | None = None


class SupplyChainResponse(BaseModel):
    period: str
    rows: List[SupplyChainRow]


@router.get("/", response_model=SupplyChainResponse)
async def get_supply_chain_view(
    _db: DBSession,
    _user: CurrentUser,
    period: str = Query(..., description="Planning period, e.g. 2025-12"),
    category: str | None = Query(None),
    source: str | None = Query(None),
) -> SupplyChainResponse:
    """
    Supply chain / orders planning endpoint.

    MVP implementation returns an empty dataset with the correct shape.
    Later, plug in the glossary formulas to compute averages and recommendations
    from historical sales, stock norms, and placed orders.
    """
    return SupplyChainResponse(period=period, rows=[])

