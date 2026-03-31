from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, exists, or_, select

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin, require_roles
from app.core.branch_localization import localize_branch_name
from app.models.reporting import DPReport, DPReportAccess
from app.models.user import User, UserRole
from app.services.reporting_service import (
    build_report_tables,
    build_reporting_context,
    default_period_for_planning,
    get_current_planning_month,
    normalize_override_metric,
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
    adjustment_reason: str | None = None
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


class HistoricalProjectedRow(BaseModel):
    period: str
    fact_value: float
    target_value: float
    past_available_stock: float

    model_config = {"extra": "allow"}


class ForecastProjectedRow(BaseModel):
    period: str
    baseline_forecast_value: float
    adjusted_forecast_value: float
    future_available_stock: float

    model_config = {"extra": "allow"}


class HistoricalDetailedRow(BaseModel):
    period: str
    fact_quantity_in_mc: float
    fact_gross_weight_kg: float
    fact_volume_cbm: float
    fact_amount_kzt: float
    target_quantity_in_mc: float
    target_gross_weight_kg: float
    target_volume_cbm: float
    target_amount_kzt: float
    past_available_stock: float

    model_config = {"extra": "allow"}


class ForecastDetailedRow(BaseModel):
    period: str
    baseline_forecast_quantity_in_mc: float
    baseline_forecast_gross_weight_kg: float
    baseline_forecast_volume_cbm: float
    baseline_forecast_amount_kzt: float
    adjusted_forecast_quantity_in_mc: float
    adjusted_forecast_gross_weight_kg: float
    adjusted_forecast_volume_cbm: float
    adjusted_forecast_amount_kzt: float
    future_available_stock: float

    model_config = {"extra": "allow"}


class ReportDetailProjectedResponse(BaseModel):
    report: ReportCard
    historical_table: list[HistoricalProjectedRow]
    forecast_table: list[ForecastProjectedRow]


class ReportDetailDetailedResponse(BaseModel):
    report: ReportCard
    historical_table: list[HistoricalDetailedRow]
    forecast_table: list[ForecastDetailedRow]


class ReportUpsertPayload(BaseModel):
    report_name: str | None = None
    product_filter: ProductFilterPayload | None = None
    branch_filter: list[str] | None = None
    view_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_draft: bool = True
    forecast_adjustments: list[ForecastAdjustmentPayload] | None = None


class HistoricalTableCreateRow(BaseModel):
    period: str
    fact_value: float
    target_value: float
    past_available_stock: float

    model_config = {"extra": "allow"}


class ForecastTableCreateRow(BaseModel):
    period: str
    baseline_forecast_value: float
    adjusted_forecast_value: float
    future_available_stock: float
    adjustment_reason: str | None = None

    model_config = {"extra": "allow"}


class ReportCreatePayload(BaseModel):
    report_name: str | None = None
    is_draft: bool = True
    historical_table: list[HistoricalTableCreateRow] = Field(default_factory=list)
    forecast_table: list[ForecastTableCreateRow] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class ForecastAdjustmentPatchPayload(BaseModel):
    period: date
    value: float
    adjustment_reason: str | None = None

    model_config = {"extra": "forbid"}


class ReportPatchPayload(BaseModel):
    report_name: str | None = None
    is_draft: bool | None = None
    forecast_adjustments: list[ForecastAdjustmentPatchPayload] | None = None

    model_config = {"extra": "forbid"}


class ReportAccessGrant(BaseModel):
    user_id: int


class ReportAccessOut(BaseModel):
    user_id: int
    report_id: int
    granted_by_id: int | None = None


def _view_metric_suffix(view_type: str) -> str:
    normalized = (view_type or "").strip().lower()
    if normalized == "dsp":
        return "amount_kzt"
    if normalized == "gross weight":
        return "gross_weight_kg"
    return "quantity_in_mc"


def _project_tables_for_view_type(
    *,
    historical_table: list[dict],
    forecast_table: list[dict],
    view_type: str,
) -> tuple[list[dict], list[dict]]:
    suffix = _view_metric_suffix(view_type)
    fact_key = f"fact_{suffix}"
    target_key = f"target_{suffix}"
    baseline_key = f"baseline_forecast_{suffix}"
    adjusted_key = f"adjusted_forecast_{suffix}"

    projected_historical: list[dict] = []
    for row in historical_table:
        projected_historical.append(
            {
                "period": row.get("period"),
                "fact_value": round(float(row.get(fact_key, 0.0) or 0.0), 2),
                "target_value": round(float(row.get(target_key, 0.0) or 0.0), 2),
                "past_available_stock": round(float(row.get("past_available_stock", 0.0) or 0.0), 2),
            }
        )

    projected_forecast: list[dict] = []
    for row in forecast_table:
        projected_forecast.append(
            {
                "period": row.get("period"),
                "baseline_forecast_value": round(float(row.get(baseline_key, 0.0) or 0.0), 2),
                "adjusted_forecast_value": round(
                    float(row.get(adjusted_key, row.get(baseline_key, 0.0)) or 0.0), 2
                ),
                "future_available_stock": round(float(row.get("future_available_stock", 0.0) or 0.0), 2),
            }
        )

    return projected_historical, projected_forecast


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


def _clean_list(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def _parse_period_text(period: str) -> date:
    raw = str(period or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="forecast_table.period must be a non-empty string",
        )
    for fmt_value in ("%Y-%m-%d", "%Y-%m"):
        try:
            parsed = date.fromisoformat(raw) if fmt_value == "%Y-%m-%d" else None
            if parsed is None:
                y, m = raw.split("-")
                parsed = date(int(y), int(m), 1)
            return parsed.replace(day=1)
        except Exception:
            continue
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="forecast_table.period must be in YYYY-MM or YYYY-MM-DD format",
    )


def _forecast_table_to_overrides(
    *,
    forecast_table: list[ForecastTableCreateRow],
    view_type: str,
) -> list[dict]:
    overrides: list[dict] = []
    for row in forecast_table:
        baseline = float(row.baseline_forecast_value or 0.0)
        adjusted = float(row.adjusted_forecast_value or 0.0)
        if round(adjusted, 6) == round(baseline, 6):
            continue
        overrides.append(
            {
                "period": _parse_period_text(row.period),
                "metric_type": view_type,
                "value": adjusted,
                "adjustment_reason": row.adjustment_reason,
            }
        )
    return overrides


def _resolve_report_planning_month(report: DPReport) -> date | None:
    if report.planning_month is not None:
        return report.planning_month
    if report.date_to is not None:
        return report.date_to.replace(day=1)
    if report.date_from is not None:
        return report.date_from.replace(day=1)
    return None


def _effective_filters_from_overrides(
    *,
    saved_product_filter: dict,
    saved_branch_filter: list[str],
    sku_code: list[str] | None,
    brand: list[str] | None,
    category: list[str] | None,
    sub_category: list[str] | None,
    subline: list[str] | None,
    branch_name: list[str] | None,
) -> tuple[dict, list[str]]:
    product_filter = {
        "sku_codes": list(saved_product_filter.get("sku_codes", []) or []),
        "brands": list(saved_product_filter.get("brands", []) or []),
        "categories": list(saved_product_filter.get("categories", []) or []),
        "sub_categories": list(saved_product_filter.get("sub_categories", []) or []),
        "sublines": list(saved_product_filter.get("sublines", []) or []),
    }
    if sku_code is not None:
        product_filter["sku_codes"] = _clean_list(sku_code)
    if brand is not None:
        product_filter["brands"] = _clean_list(brand)
    if category is not None:
        product_filter["categories"] = _clean_list(category)
    if sub_category is not None:
        product_filter["sub_categories"] = _clean_list(sub_category)
    if subline is not None:
        product_filter["sublines"] = _clean_list(subline)

    if branch_name is None:
        effective_branch_filter = [
            str(localize_branch_name(v) or v) for v in list(saved_branch_filter)
        ]
    else:
        effective_branch_filter = [
            str(localize_branch_name(v) or v) for v in _clean_list(branch_name)
        ]
    return product_filter, effective_branch_filter


async def _build_report_detail(
    db: DBSession,
    report: DPReport,
    *,
    view_type_override: str | None = None,
    date_from_override: date | None = None,
    date_to_override: date | None = None,
    sku_code: list[str] | None = None,
    brand: list[str] | None = None,
    category: list[str] | None = None,
    sub_category: list[str] | None = None,
    subline: list[str] | None = None,
    branch_name: list[str] | None = None,
    project_by_view_type: bool = False,
) -> dict:
    owner_user_id = int(report.created_by_id or 0)
    saved_product_filter = parse_product_filter(report.product_filter_json or report.product_filter)
    saved_branch_filter = parse_branch_filter(report.branch_filter_json or report.branch_filter)
    effective_product_filter, effective_branch_filter = _effective_filters_from_overrides(
        saved_product_filter=saved_product_filter,
        saved_branch_filter=saved_branch_filter,
        sku_code=sku_code,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        branch_name=branch_name,
    )
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=owner_user_id,
        view_type=view_type_override or report.view_type,
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        planning_month=_resolve_report_planning_month(report),
        date_from=date_from_override or report.date_from,
        date_to=date_to_override or report.date_to,
    )
    historical_table, forecast_table = await build_report_tables(
        db=db,
        owner_user_id=owner_user_id,
        ctx=ctx,
        report_id=report.id,
    )
    if project_by_view_type:
        historical_table, forecast_table = _project_tables_for_view_type(
            historical_table=historical_table,
            forecast_table=forecast_table,
            view_type=ctx.view_type,
        )
    card = report_card_payload(report)
    return {
        "report": {
            "report_id": card["report_id"],
            "report_name": card["report_name"],
            "product_filter": ProductFilterPayload(**ctx.product_filter),
            "branch_filter": ctx.branch_filter,
            "view_type": ctx.view_type,
            "date_from": ctx.date_from,
            "date_to": ctx.date_to,
            "is_draft": card["is_draft"],
            "planning_month": ctx.planning_month,
        },
        "historical_table": historical_table,
        "forecast_table": forecast_table,
    }


@router.get("", response_model=List[ReportCard], include_in_schema=False)
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


@router.get("/new", response_model=ReportDetailProjectedResponse)
async def get_new_report_template(
    db: DBSession,
    user: CurrentUser,
) -> ReportDetailProjectedResponse:
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
    projected_historical, projected_forecast = _project_tables_for_view_type(
        historical_table=historical_table,
        forecast_table=forecast_table,
        view_type=ctx.view_type,
    )
    return ReportDetailProjectedResponse(
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
        historical_table=projected_historical,
        forecast_table=projected_forecast,
    )


@router.post("", response_model=ReportDetailProjectedResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
@router.post("/", response_model=ReportDetailProjectedResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    db: DBSession,
    user: CurrentUser,
    payload: ReportCreatePayload,
    view_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    effective_product_filter, effective_branch_filter = _effective_filters_from_overrides(
        saved_product_filter={},
        saved_branch_filter=[],
        sku_code=sku_code,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        branch_name=branch_name,
    )

    planning_month = await get_current_planning_month(db, user.id)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=user.id,
        view_type=view_type or "cases",
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        planning_month=planning_month,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
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
        overrides=_forecast_table_to_overrides(
            forecast_table=payload.forecast_table,
            view_type=ctx.view_type,
        ),
    )
    await db.commit()
    await db.refresh(report)
    payload_out = await _build_report_detail(db, report, project_by_view_type=True)
    return ReportDetailProjectedResponse(**payload_out)


@router.post("/preview", response_model=ReportDetailProjectedResponse)
async def preview_report(
    db: DBSession,
    user: CurrentUser,
    payload: ReportUpsertPayload,
    report_id: int | None = None,
) -> ReportDetailProjectedResponse:
    """
    Preview report tables for ad-hoc filter changes without persisting updates.
    If report_id is provided, preview uses that report's planning_month and saved defaults.
    """
    base_report: DPReport | None = None
    owner_user_id = user.id
    planning_month: date | None = None
    report_name = payload.report_name or "Preview Demand Planning Report"
    base_view_type = payload.view_type or "cases"
    base_product_filter: object | None = (
        payload.product_filter.model_dump() if payload.product_filter is not None else {}
    )
    base_branch_filter: object | None = payload.branch_filter or []
    base_date_from = payload.date_from
    base_date_to = payload.date_to
    preview_report_id: int | None = None

    if report_id is not None:
        base_report = await _get_accessible_report(db, user, report_id)
        if not base_report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )
        owner_user_id = int(base_report.created_by_id or user.id)
        planning_month = base_report.planning_month
        preview_report_id = base_report.id
        report_name = payload.report_name or base_report.name
        base_view_type = payload.view_type or base_report.view_type
        if payload.product_filter is None:
            base_product_filter = (
                base_report.product_filter_json or base_report.product_filter
            )
        if payload.branch_filter is None:
            base_branch_filter = parse_branch_filter(
                base_report.branch_filter_json or base_report.branch_filter
            )
        base_date_from = payload.date_from or base_report.date_from
        base_date_to = payload.date_to or base_report.date_to

    ctx = await build_reporting_context(
        db=db,
        owner_user_id=owner_user_id,
        view_type=base_view_type,
        product_filter=base_product_filter,
        branch_filter=base_branch_filter,
        planning_month=planning_month,
        date_from=base_date_from,
        date_to=base_date_to,
    )
    historical_table, forecast_table = await build_report_tables(
        db=db,
        owner_user_id=owner_user_id,
        ctx=ctx,
        report_id=preview_report_id,
    )
    projected_historical, projected_forecast = _project_tables_for_view_type(
        historical_table=historical_table,
        forecast_table=forecast_table,
        view_type=ctx.view_type,
    )
    return ReportDetailProjectedResponse(
        report=ReportCard(
            report_id=preview_report_id or 0,
            report_name=report_name,
            product_filter=ProductFilterPayload(**ctx.product_filter),
            branch_filter=ctx.branch_filter,
            view_type=ctx.view_type,
            date_from=ctx.date_from,
            date_to=ctx.date_to,
            is_draft=(base_report.is_draft if base_report else payload.is_draft),
            planning_month=ctx.planning_month,
        ),
        historical_table=projected_historical,
        forecast_table=projected_forecast,
    )


@router.get("/{report_id}", response_model=ReportDetailProjectedResponse)
async def get_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    view_type: str | None = Query(
        default=None,
        description="Transient projection filter for this GET only. Values: DSP, Cases, Gross weight.",
    ),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    payload_out = await _build_report_detail(
        db,
        report,
        view_type_override=view_type,
        date_from_override=parsed_date_from,
        date_to_override=parsed_date_to,
        sku_code=sku_code,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        branch_name=branch_name,
        project_by_view_type=True,
    )
    return ReportDetailProjectedResponse(**payload_out)


@router.patch("/{report_id}", response_model=ReportDetailProjectedResponse)
async def update_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    payload: ReportPatchPayload,
    view_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if not is_admin(user) and report.created_by_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
    saved_product_filter = parse_product_filter(report.product_filter_json or report.product_filter)
    saved_branch_filter = parse_branch_filter(report.branch_filter_json or report.branch_filter)
    effective_product_filter, effective_branch_filter = _effective_filters_from_overrides(
        saved_product_filter=saved_product_filter,
        saved_branch_filter=saved_branch_filter,
        sku_code=sku_code,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        branch_name=branch_name,
    )

    ctx = await build_reporting_context(
        db=db,
        owner_user_id=int(report.created_by_id or user.id),
        view_type=view_type or report.view_type,
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        planning_month=_resolve_report_planning_month(report),
        date_from=parsed_date_from or report.date_from,
        date_to=parsed_date_to or report.date_to,
    )

    report.name = payload.report_name or report.name
    report.product_filter_json = to_json_string(ctx.product_filter)
    report.branch_filter_json = to_json_string(ctx.branch_filter)
    report.view_type = ctx.view_type
    report.date_from = ctx.date_from
    report.date_to = ctx.date_to
    if payload.is_draft is not None:
        report.is_draft = payload.is_draft
    report.updated_by_id = user.id

    if payload.forecast_adjustments is not None:
        effective_metric_type = view_type or report.view_type
        normalized_metric = normalize_override_metric(effective_metric_type or "")
        if normalized_metric is None or str(effective_metric_type or "").strip().lower() not in {
            "dsp",
            "cases",
            "gross weight",
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="view_type must be provided as query param (DSP, Cases, Gross Weight) or be valid on report",
            )
        overrides = [
            {
                "period": adj.period,
                "metric_type": normalized_metric,
                "value": adj.value,
                "adjustment_reason": adj.adjustment_reason,
            }
            for adj in payload.forecast_adjustments
        ]
        await replace_report_overrides(
            db=db,
            report_id=report.id,
            owner_user_id=int(report.created_by_id or user.id),
            overrides=overrides,
        )
    await db.commit()
    await db.refresh(report)
    payload_out = await _build_report_detail(db, report, project_by_view_type=True)
    return ReportDetailProjectedResponse(**payload_out)


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

