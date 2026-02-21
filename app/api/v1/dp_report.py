from datetime import date
from typing import List

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import exists, or_, select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import Branch
from app.models.derived import DPReportMart
from app.models.reporting import DPReport, DPReportAccess

router = APIRouter(prefix="/dp-report", tags=["dp-report"])


class DPReportMartRow(BaseModel):
    sku_id: str
    date: date
    branch_name: str
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
    user: CurrentUser,
    report_id: int = Query(...),
    sku_id: str | None = Query(None),
    branch_name: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> list[DPReportMartRow]:
    report_stmt = select(DPReport).where(
        DPReport.id == report_id, DPReport.is_deleted.is_(False)
    )
    if not is_admin(user):
        shared_access = exists(
            select(DPReportAccess.id).where(
                DPReportAccess.report_id == DPReport.id,
                DPReportAccess.user_id == user.id,
            )
        )
        report_stmt = report_stmt.where(
            or_(DPReport.created_by_id == user.id, shared_access)
        )
    report = (await db.execute(report_stmt)).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    effective_sku = sku_id if sku_id is not None else report.product_filter
    effective_branch_name = branch_name if branch_name is not None else report.branch_filter
    effective_date_from = date_from if date_from is not None else report.date_from
    effective_date_to = date_to if date_to is not None else report.date_to

    branch_rows = (
        await db.execute(
            select(Branch).where(Branch.owner_user_id == report.created_by_id)
        )
    ).scalars().all()
    branch_id_by_name = {b.branch_name: b.branch_id for b in branch_rows}
    branch_name_by_id = {b.branch_id: b.branch_name for b in branch_rows}

    effective_branch_id = (
        branch_id_by_name.get(effective_branch_name)
        if effective_branch_name
        else None
    )

    stmt = select(DPReportMart).where(
        DPReportMart.owner_user_id == report.created_by_id
    )
    if effective_sku:
        stmt = stmt.where(DPReportMart.sku_id == effective_sku)
    if effective_branch_name:
        if effective_branch_id is None:
            return []
        stmt = stmt.where(DPReportMart.branch_id == effective_branch_id)
    if effective_date_from:
        stmt = stmt.where(DPReportMart.date >= effective_date_from)
    if effective_date_to:
        stmt = stmt.where(DPReportMart.date <= effective_date_to)
    stmt = stmt.order_by(DPReportMart.sku_id, DPReportMart.branch_id, DPReportMart.date)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        DPReportMartRow(
            sku_id=r.sku_id,
            date=r.date,
            branch_name=branch_name_by_id.get(r.branch_id, r.branch_id),
            fact_quantity_in_mc=r.fact_quantity_in_mc,
            fact_gross_weight_kg=r.fact_gross_weight_kg,
            fact_volume_cbm=r.fact_volume_cbm,
            fact_amount_kzt=r.fact_amount_kzt,
            target_quantity_in_mc=r.target_quantity_in_mc,
            target_gross_weight_kg=r.target_gross_weight_kg,
            target_volume_cbm=r.target_volume_cbm,
            target_amount_kzt=r.target_amount_kzt,
            past_available_stock=r.past_available_stock,
            baseline_forecast_quantity_in_mc=r.baseline_forecast_quantity_in_mc,
            baseline_forecast_gross_weight_kg=r.baseline_forecast_gross_weight_kg,
            baseline_forecast_volume_cbm=r.baseline_forecast_volume_cbm,
            baseline_forecast_amount_kzt=r.baseline_forecast_amount_kzt,
            adjusted_forecast_quantity_in_mc=r.adjusted_forecast_quantity_in_mc,
            adjusted_forecast_gross_weight_kg=r.adjusted_forecast_gross_weight_kg,
            adjusted_forecast_volume_cbm=r.adjusted_forecast_volume_cbm,
            adjusted_forecast_amount_kzt=r.adjusted_forecast_amount_kzt,
            future_available_stock=r.future_available_stock,
        )
        for r in rows
    ]

