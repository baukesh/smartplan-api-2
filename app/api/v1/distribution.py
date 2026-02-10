from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser, DBSession

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
    _db: DBSession,
    _user: CurrentUser,
) -> DistributionSummary:
    """
    Distribution by branches endpoint.

    For MVP it returns an empty plan with numeric headers; later we can
    implement the health index logic and recommended volumes.
    """
    return DistributionSummary(
        starting_stock_amount=0,
        distribution_volume_amount=0,
        rows=[],
    )

