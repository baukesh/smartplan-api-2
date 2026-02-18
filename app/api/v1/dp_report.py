from datetime import date
from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.models.derived import DPReportMart

router = APIRouter(prefix="/dp-report", tags=["dp-report"])


class DPReportMartRow(BaseModel):
    sku_id: str
    date: date
    branch_id: str
    fact_quantity_in_mc: float | None = None
    fact_gross_weight_kg: float | None = None
    fact_volume_cbm: float | None = None
    fact_amount_kzt: float | None = None
    target_quantity_in_mc: float | None = None
    target_gross_weight_kg: float | None = None
    target_volume_cbm: float | None = None
    target_amount_kzt: float | None = None
    past_available_stock: float | None = None
    baseline_forecast_quantity_in_mc: float | None = None
    baseline_forecast_gross_weight_kg: float | None = None
    baseline_forecast_volume_cbm: float | None = None
    baseline_forecast_amount_kzt: float | None = None
    adjusted_forecast_quantity_in_mc: float | None = None
    adjusted_forecast_gross_weight_kg: float | None = None
    adjusted_forecast_volume_cbm: float | None = None
    adjusted_forecast_amount_kzt: float | None = None
    future_available_stock: float | None = None

    model_config = {"from_attributes": True}


@router.get("/", response_model=List[DPReportMartRow])
async def get_dp_report_datamart(
    db: DBSession,
    _user: CurrentUser,
    sku_id: str | None = Query(None),
    branch_id: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> list[DPReportMart]:
    stmt = select(DPReportMart)
    if sku_id:
        stmt = stmt.where(DPReportMart.sku_id == sku_id)
    if branch_id:
        stmt = stmt.where(DPReportMart.branch_id == branch_id)
    if date_from:
        stmt = stmt.where(DPReportMart.date >= date_from)
    if date_to:
        stmt = stmt.where(DPReportMart.date <= date_to)
    stmt = stmt.order_by(DPReportMart.sku_id, DPReportMart.branch_id, DPReportMart.date)
    result = await db.execute(stmt)
    return list(result.scalars().all())

