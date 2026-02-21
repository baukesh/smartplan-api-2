from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Select, exists, or_, select

from app.api.deps import CurrentUser, DBSession, is_admin, require_roles
from app.models.reporting import DPReport, DPReportAccess
from app.models.user import User, UserRole

router = APIRouter(prefix="/reports", tags=["reports"])


class DPReportBase(BaseModel):
    name: str
    product_filter: str | None = None
    branch_filter: str | None = None
    view_type: str = "cases"
    date_from: date | None = None
    date_to: date | None = None
    is_draft: bool = True


class DPReportCreate(DPReportBase):
    pass


class DPReportUpdate(BaseModel):
    name: str | None = None
    product_filter: str | None = None
    branch_filter: str | None = None
    view_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_draft: bool | None = None


class DPReportOut(DPReportBase):
    id: int

    model_config = {"from_attributes": True}


class ReportTimeseriesPoint(BaseModel):
    period: str
    fact: float = 0
    target: float = 0
    forecast_baseline: float = 0
    forecast_adjusted: float = 0
    available_stock: float = 0


class DPReportDetail(BaseModel):
    report: DPReportOut
    series: List[ReportTimeseriesPoint]


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
    stmt = _visible_reports_stmt(user).where(DPReport.id == report_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@router.get("/", response_model=List[DPReportOut])
async def list_reports(
    db: DBSession,
    user: CurrentUser,
) -> List[DPReport]:
    stmt = _visible_reports_stmt(user).order_by(DPReport.created_at.desc())  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/", response_model=DPReportOut, status_code=status.HTTP_201_CREATED)
async def create_report(
    db: DBSession,
    user: CurrentUser,
    payload: DPReportCreate,
) -> DPReport:
    report = DPReport(
        **payload.model_dump(),
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


@router.get("/{report_id}", response_model=DPReportDetail)
async def get_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
) -> DPReportDetail:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    # MVP detail: return structure compatible with chart/table, but with zeroed metrics.
    series: list[ReportTimeseriesPoint] = []
    return DPReportDetail(report=DPReportOut.model_validate(report), series=series)


@router.patch("/{report_id}", response_model=DPReportOut)
async def update_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    payload: DPReportUpdate,
) -> DPReport:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if not is_admin(user) and report.created_by_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(report, field, value)
    report.updated_by_id = user.id
    await db.commit()
    await db.refresh(report)
    return report


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
    result = await db.execute(
        select(DPReportAccess).where(DPReportAccess.report_id == report_id)
    )
    rows = result.scalars().all()
    return [
        ReportAccessOut(
            user_id=r.user_id,
            report_id=r.report_id,
            granted_by_id=r.granted_by_id,
        )
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
    existing = await db.execute(
        select(DPReportAccess).where(
            DPReportAccess.report_id == report_id, DPReportAccess.user_id == payload.user_id
        )
    )
    access = existing.scalar_one_or_none()
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
    result = await db.execute(
        select(DPReportAccess).where(
            DPReportAccess.report_id == report_id, DPReportAccess.user_id == user_id
        )
    )
    access = result.scalar_one_or_none()
    if not access:
        return
    await db.delete(access)
    await db.commit()

