from datetime import date
from typing import List

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.models.reporting import DPReport

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


def _visible_reports_stmt(current_user_id: int) -> Select:
    # MVP: all team-shared, soft-deleted filtered out
    return select(DPReport).where(DPReport.is_deleted.is_(False))


@router.get("/", response_model=List[DPReportOut])
async def list_reports(
    db: DBSession,
    _user: CurrentUser,
) -> List[DPReport]:
    stmt = _visible_reports_stmt(_user.id).order_by(DPReport.created_at.desc())  # type: ignore[attr-defined]
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
    _user: CurrentUser,
    report_id: int,
) -> DPReportDetail:
    report = await db.get(DPReport, report_id)
    if not report or report.is_deleted:
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
    report = await db.get(DPReport, report_id)
    if not report or report.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(report, field, value)
    report.updated_by_id = user.id
    await db.commit()
    await db.refresh(report)
    return report


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    db: DBSession,
    _user: CurrentUser,
    report_id: int,
) -> None:
    report = await db.get(DPReport, report_id)
    if not report or report.is_deleted:
        return
    report.is_deleted = True
    await db.commit()

