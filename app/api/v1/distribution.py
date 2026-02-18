from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.models.derived import BranchDistribution

router = APIRouter(prefix="/distribution", tags=["distribution"])


class BranchDistributionRow(BaseModel):
    branch_id: str
    branch_name: str
    volume_cbm: float | None = None
    amount_kzt: float | None = None
    recommended_volume: float | None = None
    distribute_quantity: float | None = None
    health_index_status: str | None = None


class DistributionSummary(BaseModel):
    starting_stock_amount: float | None = None
    distribution_volume_amount: float | None = None
    rows: List[BranchDistributionRow]


@router.get("/", response_model=DistributionSummary)
async def get_distribution_plan(
    db: DBSession,
    _user: CurrentUser,
) -> DistributionSummary:
    rows = (await db.execute(select(BranchDistribution).order_by(BranchDistribution.branch_id))).scalars().all()
    formatted = [
        BranchDistributionRow(
            branch_id=r.branch_id,
            branch_name=r.branch_id,
            volume_cbm=r.available_volume_cbm,
            amount_kzt=r.available_amount_kzt,
            recommended_volume=r.recommended_volume_cbm,
            distribute_quantity=r.recommended_quantity_in_mc,
            health_index_status=r.branch_health_index,
        )
        for r in rows
    ]
    return DistributionSummary(
        starting_stock_amount=round(sum(r.available_amount_kzt for r in rows), 2) if rows else 0,
        distribution_volume_amount=round(sum(r.recommended_amount_kzt for r in rows), 2) if rows else 0,
        rows=formatted,
    )

