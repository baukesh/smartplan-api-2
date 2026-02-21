from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, exists, or_, select

from app.api.deps import CurrentUser, DBSession, is_admin, require_roles
from app.models.reporting import DPReport, DPReportAccess
from app.models.user import User, UserRole
from app.services.reporting_service import (
    build_report_tables,
    build_reporting_context,
    default_period_for_planning,
    get_current_planning_month,
    parse_branch_filter,
    parse_product_filter,
    replace_report_overrides,
    report_card_payload,
    to_json_string,
)

router = APIRouter(prefix="/reports", tags=["reports"])


class ProductFilterPayload(BaseModel):
    sku_codes: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    sub_categories: list[str] = Field(default_factory=list)
    sublines: list[str] = Field(default_factory=list)


class ForecastAdjustmentPayload(BaseModel):
    period: date
    metric_type: str
    value: float
    branch_name: str | None = None
    brand: str | None = None
    category: str | None = None
    sub_category: str | None = None
    subline: str | None = None
    sku_name: str | None = None


class ReportCard(BaseModel):
    report_id: int
    report_name: str
    product_filter: ProductFilterPayload
    branch_filter: list[str]
    view_type: str
    date_from: date
    date_to: date
    is_draft: bool
    planning_month: date


class ReportDetailResponse(BaseModel):
    report: ReportCard
    historical_table: list[dict]
    forecast_table: list[dict]


class ReportUpsertPayload(BaseModel):
    report_name: str | None = None
    product_filter: ProductFilterPayload | None = None
    branch_filter: list[str] | None = None
    view_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_draft: bool = True
    forecast_adjustments: list[ForecastAdjustmentPayload] | None = None


class ReportAccessGrant(BaseModel):
    user_id: int


class ReportAccessOut(BaseModel):
    user_id: int
    report_id: int
    granted_by_id: int | None = None


def _visible_reports_stmt(user: User) -> Select:
    stmt = select(DPReport).where(DPReport.is_deleted.is_(False))
    if is_admin(user):
        return stmt
    shared_access = exists(
        select(DPReportAccess.id).where(
            DPReportAccess.report_id == DPReport.id,
            DPReportAccess.user_id == user.id,
        )
    )
    return stmt.where(or_(DPReport.created_by_id == user.id, shared_access))


async def _get_accessible_report(db: DBSession, user: User, report_id: int) -> DPReport | None:
    result = await db.execute(_visible_reports_stmt(user).where(DPReport.id == report_id))
    return result.scalar_one_or_none()


async def _build_report_detail(db: DBSession, report: DPReport) -> ReportDetailResponse:
    owner_user_id = int(report.created_by_id or 0)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=owner_user_id,
        view_type=report.view_type,
        product_filter=report.product_filter_json or report.product_filter,
        branch_filter=report.branch_filter_json or report.branch_filter,
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


@router.get("/", response_model=List[ReportCard])
@router.get("/list", response_model=List[ReportCard])
async def list_reports(
    db: DBSession,
    user: CurrentUser,
) -> List[ReportCard]:
    rows = (
        await db.execute(_visible_reports_stmt(user).order_by(DPReport.created_at.desc()))  # type: ignore[attr-defined]
    ).scalars().all()
    cards: list[ReportCard] = []
    for report in rows:
        card = report_card_payload(report)
        cards.append(
            ReportCard(
                report_id=card["report_id"],
                report_name=card["report_name"],
                product_filter=ProductFilterPayload(**card["product_filter"]),
                branch_filter=card["branch_filter"],
                view_type=card["view_type"],
                date_from=card["date_from"],
                date_to=card["date_to"],
                is_draft=card["is_draft"],
                planning_month=card["planning_month"],
            )
        )
    return cards


@router.get("/new", response_model=ReportDetailResponse)
async def get_new_report_template(
    db: DBSession,
    user: CurrentUser,
) -> ReportDetailResponse:
    planning_month = await get_current_planning_month(db, user.id)
    date_from, date_to = default_period_for_planning(planning_month)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=user.id,
        view_type="cases",
        product_filter={},
        branch_filter=[],
        planning_month=planning_month,
        date_from=date_from,
        date_to=date_to,
    )
    historical_table, forecast_table = await build_report_tables(
        db=db,
        owner_user_id=user.id,
        ctx=ctx,
        report_id=None,
    )
    return ReportDetailResponse(
        report=ReportCard(
            report_id=0,
            report_name="New Demand Planning Report",
            product_filter=ProductFilterPayload(),
            branch_filter=[],
            view_type="cases",
            date_from=ctx.date_from,
            date_to=ctx.date_to,
            is_draft=True,
            planning_month=planning_month,
        ),
        historical_table=historical_table,
        forecast_table=forecast_table,
    )


@router.post("/", response_model=ReportDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    db: DBSession,
    user: CurrentUser,
    payload: ReportUpsertPayload,
) -> ReportDetailResponse:
    planning_month = await get_current_planning_month(db, user.id)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=user.id,
        view_type=payload.view_type or "cases",
        product_filter=(payload.product_filter.model_dump() if payload.product_filter else {}),
        branch_filter=(payload.branch_filter or []),
        planning_month=planning_month,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )
    report = DPReport(
        name=payload.report_name or "New Demand Planning Report",
        product_filter=None,
        branch_filter=None,
        product_filter_json=to_json_string(ctx.product_filter),
        branch_filter_json=to_json_string(ctx.branch_filter),
        view_type=ctx.view_type,
        date_from=ctx.date_from,
        date_to=ctx.date_to,
        planning_month=ctx.planning_month,
        is_draft=payload.is_draft,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(report)
    await db.flush()
    await replace_report_overrides(
        db=db,
        report_id=report.id,
        owner_user_id=user.id,
        overrides=[x.model_dump() for x in (payload.forecast_adjustments or [])],
    )
    await db.commit()
    await db.refresh(report)
    return await _build_report_detail(db, report)


@router.get("/{report_id}", response_model=ReportDetailResponse)
async def get_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
) -> ReportDetailResponse:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return await _build_report_detail(db, report)


@router.patch("/{report_id}", response_model=ReportDetailResponse)
async def update_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    payload: ReportUpsertPayload,
) -> ReportDetailResponse:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if not is_admin(user) and report.created_by_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")

    ctx = await build_reporting_context(
        db=db,
        owner_user_id=int(report.created_by_id or user.id),
        view_type=payload.view_type or report.view_type,
        product_filter=(
            payload.product_filter.model_dump()
            if payload.product_filter is not None
            else (report.product_filter_json or report.product_filter)
        ),
        branch_filter=(
            payload.branch_filter
            if payload.branch_filter is not None
            else parse_branch_filter(report.branch_filter_json or report.branch_filter)
        ),
        planning_month=report.planning_month,
        date_from=payload.date_from or report.date_from,
        date_to=payload.date_to or report.date_to,
    )

    report.name = payload.report_name or report.name
    report.product_filter_json = to_json_string(ctx.product_filter)
    report.branch_filter_json = to_json_string(ctx.branch_filter)
    report.view_type = ctx.view_type
    report.date_from = ctx.date_from
    report.date_to = ctx.date_to
    report.is_draft = payload.is_draft
    report.updated_by_id = user.id

    if payload.forecast_adjustments is not None:
        await replace_report_overrides(
            db=db,
            report_id=report.id,
            owner_user_id=int(report.created_by_id or user.id),
            overrides=[x.model_dump() for x in (payload.forecast_adjustments or [])],
        )
    await db.commit()
    await db.refresh(report)
    return await _build_report_detail(db, report)


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
) -> None:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        return
    if not is_admin(user) and report.created_by_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
    report.is_deleted = True
    await db.commit()


@router.get("/{report_id}/access", response_model=List[ReportAccessOut])
async def list_report_access(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
) -> List[ReportAccessOut]:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    rows = (
        await db.execute(select(DPReportAccess).where(DPReportAccess.report_id == report_id))
    ).scalars().all()
    return [
        ReportAccessOut(user_id=r.user_id, report_id=r.report_id, granted_by_id=r.granted_by_id)
        for r in rows
    ]


@router.post(
    "/{report_id}/access",
    response_model=ReportAccessOut,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def grant_report_access(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    payload: ReportAccessGrant,
) -> ReportAccessOut:
    report = await db.get(DPReport, report_id)
    if not report or report.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    target_user = await db.get(User, payload.user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    access = (
        await db.execute(
            select(DPReportAccess).where(
                DPReportAccess.report_id == report_id,
                DPReportAccess.user_id == payload.user_id,
            )
        )
    ).scalar_one_or_none()
    if not access:
        access = DPReportAccess(
            report_id=report_id,
            user_id=payload.user_id,
            granted_by_id=user.id,
        )
        db.add(access)
        await db.commit()
        await db.refresh(access)
    return ReportAccessOut(
        user_id=access.user_id,
        report_id=access.report_id,
        granted_by_id=access.granted_by_id,
    )


@router.delete(
    "/{report_id}/access/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def revoke_report_access(
    db: DBSession,
    report_id: int,
    user_id: int,
) -> None:
    access = (
        await db.execute(
            select(DPReportAccess).where(
                DPReportAccess.report_id == report_id,
                DPReportAccess.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if access:
        await db.delete(access)
        await db.commit()

