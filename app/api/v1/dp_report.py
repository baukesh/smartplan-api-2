from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import exists, or_, select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.api.v1.reports import ReportDetailResponse
from app.models.reporting import DPReport, DPReportAccess
from app.services.reporting_service import build_report_tables, build_reporting_context, parse_branch_filter, parse_product_filter, report_card_payload

router = APIRouter(prefix="/dp-report", tags=["dp-report"])


@router.get("/", response_model=ReportDetailResponse)
async def get_dp_report_datamart(
    db: DBSession,
    user: CurrentUser,
    report_id: int = Query(...),
) -> ReportDetailResponse:
    stmt = select(DPReport).where(DPReport.id == report_id, DPReport.is_deleted.is_(False))
    if not is_admin(user):
        shared = exists(
            select(DPReportAccess.id).where(
                DPReportAccess.report_id == DPReport.id,
                DPReportAccess.user_id == user.id,
            )
        )
        stmt = stmt.where(or_(DPReport.created_by_id == user.id, shared))
    report = (await db.execute(stmt)).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    owner_user_id = int(report.created_by_id or 0)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=owner_user_id,
        view_type=report.view_type,
        product_filter=parse_product_filter(report.product_filter_json or report.product_filter),
        branch_filter=parse_branch_filter(report.branch_filter_json or report.branch_filter),
        planning_month=report.planning_month,
        date_from=report.date_from,
        date_to=report.date_to,
    )
    historical_table, forecast_table = await build_report_tables(
        db=db,
        owner_user_id=owner_user_id,
        ctx=ctx,
        report_id=report.id,
    )
    card = report_card_payload(report)
    from app.api.v1.reports import ProductFilterPayload, ReportCard

    return ReportDetailResponse(
        report=ReportCard(
            report_id=card["report_id"],
            report_name=card["report_name"],
            product_filter=ProductFilterPayload(**card["product_filter"]),
            branch_filter=card["branch_filter"],
            view_type=card["view_type"],
            date_from=card["date_from"],
            date_to=card["date_to"],
            is_draft=card["is_draft"],
            planning_month=card["planning_month"],
        ),
        historical_table=historical_table,
        forecast_table=forecast_table,
    )

