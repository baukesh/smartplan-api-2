from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser, DBSession

router = APIRouter(prefix="/inventory-health", tags=["inventory-health"])


class CategoryCard(BaseModel):
    category: str
    share_percent: float
    sku_count: int
    total_sales_value: float
    stock_percent: float
    health_index: float


class InventoryHealthRow(BaseModel):
    sku_id: str
    sku_name: str
    category: str
    sales_value: float
    share_percent: float
    health_index: float


class TopIssue(BaseModel):
    sku_id: str
    sku_name: str
    amount: float


class InventoryHealthResponse(BaseModel):
    categories: List[CategoryCard]
    table: List[InventoryHealthRow]
    top_surplus: List[TopIssue]
    top_deficit: List[TopIssue]
    top_stockout: List[TopIssue]


@router.get("/", response_model=InventoryHealthResponse)
async def get_inventory_health(
    _db: DBSession,
    _user: CurrentUser,
) -> InventoryHealthResponse:
    """
    Inventory health index endpoint.

    MVP returns empty structures; later we will compute A/B/C categories,
    deviations, and health index statuses from uploaded datasets.
    """
    return InventoryHealthResponse(
        categories=[],
        table=[],
        top_surplus=[],
        top_deficit=[],
        top_stockout=[],
    )

