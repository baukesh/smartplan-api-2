from collections import defaultdict
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import Product
from app.models.derived import InventoryHealth

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
    db: DBSession,
    _user: CurrentUser,
) -> InventoryHealthResponse:
    rows = (await db.execute(select(InventoryHealth))).scalars().all()
    if not rows:
        return InventoryHealthResponse(
            categories=[],
            table=[],
            top_surplus=[],
            top_deficit=[],
            top_stockout=[],
        )

    latest_date = max(r.date for r in rows)
    latest_rows = [r for r in rows if r.date == latest_date]
    product_by_sku = {p.sku_id: p for p in (await db.execute(select(Product))).scalars().all()}

    cat_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"share": 0.0, "sales": 0.0, "stock_days": 0.0, "health_count": 0.0, "count": 0.0}
    )
    table_rows: list[InventoryHealthRow] = []
    for r in latest_rows:
        p = product_by_sku.get(r.sku_id)
        sku_name = p.sku_name if p else r.sku_id
        health_score = 100.0
        if r.health_index == "Normal":
            health_score = 70.0
        elif r.health_index == "Critical":
            health_score = 30.0
        table_rows.append(
            InventoryHealthRow(
                sku_id=r.sku_id,
                sku_name=sku_name,
                category=r.category,
                sales_value=r.sales_amount_kzt,
                share_percent=round(r.total_sales_share * 100, 2),
                health_index=health_score,
            )
        )
        b = cat_buckets[r.category]
        b["share"] += r.total_sales_share
        b["sales"] += r.sales_amount_kzt
        b["stock_days"] += r.available_stock_days
        b["health_count"] += health_score
        b["count"] += 1

    categories = [
        CategoryCard(
            category=cat,
            share_percent=round(vals["share"] * 100, 2),
            sku_count=int(vals["count"]),
            total_sales_value=round(vals["sales"], 2),
            stock_percent=round((vals["stock_days"] / vals["count"]) if vals["count"] else 0, 2),
            health_index=round((vals["health_count"] / vals["count"]) if vals["count"] else 0, 2),
        )
        for cat, vals in sorted(cat_buckets.items())
    ]

    surplus = sorted(latest_rows, key=lambda x: x.overstock, reverse=True)[:5]
    deficit = sorted(latest_rows, key=lambda x: x.understock, reverse=True)[:5]
    stockout = sorted(latest_rows, key=lambda x: x.stock_out, reverse=True)[:5]

    def to_issue(x: InventoryHealth, amount: float) -> TopIssue:
        p = product_by_sku.get(x.sku_id)
        return TopIssue(sku_id=x.sku_id, sku_name=p.sku_name if p else x.sku_id, amount=round(amount, 2))

    return InventoryHealthResponse(
        categories=categories,
        table=table_rows,
        top_surplus=[to_issue(x, x.overstock) for x in surplus],
        top_deficit=[to_issue(x, x.understock) for x in deficit],
        top_stockout=[to_issue(x, x.stock_out) for x in stockout],
    )

