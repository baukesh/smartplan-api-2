from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, exists, or_, select

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin, require_roles
from app.core.branch_localization import localize_branch_name
from app.models.data_uploads import Branch, HistoricalSalesMonthly, Product
from app.models.derived import ForecastSalesMonthly
from app.models.reporting import DPReport, DPReportAccess, DPReportForecastOverride
from app.models.user import User, UserRole
from app.services.reporting_service import (
    build_branch_filter_options,
    build_report_tables,
    build_hub_filter_options,
    build_reporting_context,
    build_sku_status_filter_options,
    default_period_for_planning,
    get_current_planning_month,
    normalize_override_metric,
    parse_branch_filter,
    parse_hub_filter,
    parse_product_filter,
    replace_report_overrides,
    report_card_payload,
    to_json_string,
    upsert_report_overrides,
)

router = APIRouter(prefix="/reports", tags=["reports"])


class ProductFilterPayload(BaseModel):
    sku_codes: list[str] = Field(default_factory=list)
    sku_names: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    sub_categories: list[str] = Field(default_factory=list)
    sublines: list[str] = Field(default_factory=list)
    sku_statuses: list[str] = Field(default_factory=list)


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
    hub_filter: list[str] = Field(default_factory=list)
    sku_status_filter: list[str] = Field(default_factory=list)
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
    past_hub_stock: float = 0.0

    model_config = {"extra": "allow"}


class ForecastProjectedRow(BaseModel):
    period: str
    baseline_forecast_value: float
    adjusted_forecast_value: float
    future_available_stock: float
    future_hub_stock: float = 0.0

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
    past_hub_stock: float = 0.0

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
    future_hub_stock: float = 0.0

    model_config = {"extra": "allow"}


class ReportDetailProjectedResponse(BaseModel):
    report: ReportCard
    min_date: str | None = None
    max_date: str | None = None
    historical_table: list[HistoricalProjectedRow]
    forecast_table: list[ForecastProjectedRow]


class ReportDetailDetailedResponse(BaseModel):
    report: ReportCard
    min_date: str | None = None
    max_date: str | None = None
    historical_table: list[HistoricalDetailedRow]
    forecast_table: list[ForecastDetailedRow]


class ReportOverrideRow(BaseModel):
    period: str
    baseline_forecast_value: float
    adjusted_forecast_value: float
    adjustment_reason: str | None = None


class ReportOverrideResponse(BaseModel):
    report: ReportCard
    min_date: str | None = None
    max_date: str | None = None
    forecast_table: list[ReportOverrideRow]


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
    past_hub_stock: float = 0.0

    model_config = {"extra": "allow"}


class ForecastTableCreateRow(BaseModel):
    period: str
    baseline_forecast_value: float
    adjusted_forecast_value: float
    future_available_stock: float
    future_hub_stock: float = 0.0
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
    if normalized == "invoice price":
        return "invoice_amount_kzt"
    if normalized == "gross weight":
        return "gross_weight_kg"
    if normalized == "net weight":
        return "net_weight_kg"
    return "quantity_in_mc"


def _project_tables_for_view_type(
    *,
    historical_table: list[dict],
    forecast_table: list[dict],
    view_type: str,
) -> tuple[list[dict], list[dict]]:
    normalized_view = (view_type or "").strip().lower()
    suffix = _view_metric_suffix(view_type)
    fact_key = f"fact_{suffix}"
    target_key = f"target_{suffix}"
    baseline_key = f"baseline_forecast_{suffix}"
    adjusted_key = f"adjusted_forecast_{suffix}"
    historical_stock_key = (
        "past_available_stock_amount_kzt"
        if normalized_view == "dsp"
        else "past_available_stock_invoice_amount_kzt"
        if normalized_view == "invoice price"
        else "past_available_stock_gross_weight_kg"
        if normalized_view == "gross weight"
        else "past_available_stock_net_weight_kg"
        if normalized_view == "net weight"
        else "past_available_stock"
    )
    historical_hub_stock_key = (
        "past_hub_stock_amount_kzt"
        if normalized_view == "dsp"
        else "past_hub_stock_invoice_amount_kzt"
        if normalized_view == "invoice price"
        else "past_hub_stock_gross_weight_kg"
        if normalized_view == "gross weight"
        else "past_hub_stock_net_weight_kg"
        if normalized_view == "net weight"
        else "past_hub_stock"
    )
    forecast_stock_key = (
        "future_available_stock_amount_kzt"
        if normalized_view == "dsp"
        else "future_available_stock_invoice_amount_kzt"
        if normalized_view == "invoice price"
        else "future_available_stock_gross_weight_kg"
        if normalized_view == "gross weight"
        else "future_available_stock_net_weight_kg"
        if normalized_view == "net weight"
        else "future_available_stock"
    )
    forecast_hub_stock_key = (
        "future_hub_stock_amount_kzt"
        if normalized_view == "dsp"
        else "future_hub_stock_invoice_amount_kzt"
        if normalized_view == "invoice price"
        else "future_hub_stock_gross_weight_kg"
        if normalized_view == "gross weight"
        else "future_hub_stock_net_weight_kg"
        if normalized_view == "net weight"
        else "future_hub_stock"
    )

    projected_historical: list[dict] = []
    for row in historical_table:
        projected_historical.append(
            {
                "period": row.get("period"),
                "fact_value": round(float(row.get(fact_key, 0.0) or 0.0), 2),
                "target_value": round(float(row.get(target_key, 0.0) or 0.0), 2),
                "past_available_stock": round(float(row.get(historical_stock_key, 0.0) or 0.0), 2),
                "past_hub_stock": round(float(row.get(historical_hub_stock_key, 0.0) or 0.0), 2),
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
                "future_available_stock": round(float(row.get(forecast_stock_key, 0.0) or 0.0), 2),
                "future_hub_stock": round(float(row.get(forecast_hub_stock_key, 0.0) or 0.0), 2),
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


def _single_filter_value(values: list[str] | None) -> str | None:
    cleaned = list(dict.fromkeys(_clean_list(values)))
    if len(cleaned) == 1:
        return cleaned[0]
    return None


async def _resolve_override_sku_name(
    db: DBSession,
    owner_user_id: int,
    sku_code: list[str] | None,
    sku_name: list[str] | None,
) -> str | None:
    explicit_sku_name = _single_filter_value(sku_name)
    if explicit_sku_name:
        return explicit_sku_name

    single_sku_code = _single_filter_value(sku_code)
    if not single_sku_code:
        return None

    product = (
        await db.execute(
            select(Product).where(
                Product.owner_user_id == owner_user_id,
                Product.sku_code == single_sku_code,
            )
        )
    ).scalar_one_or_none()
    return str(product.sku_name).strip() if product is not None else None


async def _resolve_sku_names_from_codes(
    db: DBSession,
    owner_user_id: int,
    sku_codes: list[str],
) -> set[str]:
    cleaned_codes = _clean_list(sku_codes)
    if not cleaned_codes:
        return set()

    products = (
        await db.execute(
            select(Product).where(
                Product.owner_user_id == owner_user_id,
                Product.sku_code.in_(cleaned_codes),
            )
        )
    ).scalars().all()
    return {str(product.sku_name).strip() for product in products if str(product.sku_name).strip()}


def _parse_period_text(period: str) -> date:
    raw = str(period or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Поле forecast_table.period должно быть непустой строкой",
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
        detail="Поле forecast_table.period должно быть в формате YYYY-MM или YYYY-MM-DD",
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


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _month_iso(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _iter_months(start: date, end: date):
    current = _month_start(start)
    last = _month_start(end)
    while current <= last:
        yield current
        current = _add_months(current, 1)


def _matches_applied_filters(product: Product, branch_name_value: str, product_filter: dict, branch_filter: list[str]) -> bool:
    if branch_filter:
        normalized_filter = {str(localize_branch_name(v) or v).strip().lower() for v in branch_filter if str(v).strip()}
        if str(localize_branch_name(branch_name_value) or branch_name_value).strip().lower() not in normalized_filter:
            return False
    sku_codes = {str(v).strip() for v in (product_filter.get("sku_codes") or []) if str(v).strip()}
    sku_names = {str(v).strip() for v in (product_filter.get("sku_names") or []) if str(v).strip()}
    brands = {str(v).strip() for v in (product_filter.get("brands") or []) if str(v).strip()}
    categories = {str(v).strip() for v in (product_filter.get("categories") or []) if str(v).strip()}
    sub_categories = {str(v).strip() for v in (product_filter.get("sub_categories") or []) if str(v).strip()}
    sublines = {str(v).strip() for v in (product_filter.get("sublines") or []) if str(v).strip()}
    sku_statuses = {str(v).strip() for v in (product_filter.get("sku_statuses") or []) if str(v).strip()}
    if sku_codes and str(product.sku_code or "").strip() not in sku_codes:
        return False
    if sku_names and str(product.sku_name or "").strip() not in sku_names:
        return False
    if brands and str(product.brand or "").strip() not in brands:
        return False
    if categories and str(product.category or "").strip() not in categories:
        return False
    if sub_categories and str(product.sub_category or "").strip() not in sub_categories:
        return False
    if sublines and str(product.sub_line or "").strip() not in sublines:
        return False
    if sku_statuses and str(product.status or "").strip() not in sku_statuses:
        return False
    return True


def _matches_hub_filter(hub_name_value: str | None, hub_filter: list[str]) -> bool:
    if not hub_filter:
        return True
    normalized = {str(v).strip() for v in hub_filter if str(v).strip()}
    return str(hub_name_value or "").strip() in normalized


def _fill_projected_historical_months(rows: list[dict], start_month: date, end_month: date) -> list[dict]:
    by_period = {str(r.get("period")): r for r in rows}
    out: list[dict] = []
    for m in _iter_months(start_month, end_month):
        key = m.isoformat()
        row = by_period.get(key)
        if row is None:
            row = {
                "period": key,
                "fact_value": 0.0,
                "target_value": 0.0,
                "past_available_stock": 0.0,
                "past_hub_stock": 0.0,
            }
        out.append(row)
    return out


def _fill_projected_forecast_months(rows: list[dict], start_month: date, end_month: date) -> list[dict]:
    by_period = {str(r.get("period")): r for r in rows}
    out: list[dict] = []
    for m in _iter_months(start_month, end_month):
        key = m.isoformat()
        row = by_period.get(key)
        if row is None:
            row = {
                "period": key,
                "baseline_forecast_value": 0.0,
                "adjusted_forecast_value": 0.0,
                "future_available_stock": 0.0,
                "future_hub_stock": 0.0,
            }
        out.append(row)
    return out


async def _compute_min_max_dates(
    db: DBSession,
    owner_user_id: int,
    product_filter: dict,
    branch_filter: list[str],
    hub_filter: list[str],
) -> tuple[date | None, date | None, date | None]:
    products = (
        await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
    ).scalars().all()
    product_by_code = {str(p.sku_code or "").strip(): p for p in products}
    branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    branch_by_id = {str(b.branch_id).strip(): str(b.branch_name) for b in branches}
    latest_branch_hub_by_sku_branch: dict[tuple[str, str], tuple[date, str]] = {}
    latest_branch_hub_by_branch: dict[str, tuple[date, str]] = {}

    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(HistoricalSalesMonthly.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    forecast_rows = (
        await db.execute(
            select(ForecastSalesMonthly).where(ForecastSalesMonthly.owner_user_id == owner_user_id)
        )
    ).scalars().all()

    min_hist: date | None = None
    max_hist: date | None = None
    max_forecast: date | None = None

    for r in hist_rows:
        branch_id = str(r.branch_id or "").strip()
        if not branch_id:
            continue
        hub_name = str(r.hub_name or "").strip() or "KZ-HUB"
        sku_code = str(r.sku_code or r.sku_id or "").strip()
        sku_branch_key = (sku_code, branch_id)
        if sku_code and (
            sku_branch_key not in latest_branch_hub_by_sku_branch
            or latest_branch_hub_by_sku_branch[sku_branch_key][0] <= r.date
        ):
            latest_branch_hub_by_sku_branch[sku_branch_key] = (r.date, hub_name)
        if branch_id not in latest_branch_hub_by_branch or latest_branch_hub_by_branch[branch_id][0] <= r.date:
            latest_branch_hub_by_branch[branch_id] = (r.date, hub_name)

    for r in hist_rows:
        if not str(r.branch_id or "").strip():
            continue
        sku_code = str(r.sku_code or "").strip()
        product = product_by_code.get(sku_code)
        if product is None:
            continue
        branch_name_value = branch_by_id.get(str(r.branch_id).strip(), str(r.branch_id).strip())
        if not _matches_applied_filters(product, branch_name_value, product_filter, branch_filter):
            continue
        if not _matches_hub_filter(str(r.hub_name or "").strip() or "KZ-HUB", hub_filter):
            continue
        m = _month_start(r.date)
        min_hist = m if min_hist is None else min(min_hist, m)
        max_hist = m if max_hist is None else max(max_hist, m)

    for r in forecast_rows:
        sku_code = str(r.sku_code or "").strip()
        product = product_by_code.get(sku_code)
        if product is None:
            continue
        branch_id = str(r.branch_id).strip()
        branch_name_value = branch_by_id.get(branch_id, branch_id)
        if not _matches_applied_filters(product, branch_name_value, product_filter, branch_filter):
            continue
        hub_name = (
            latest_branch_hub_by_sku_branch.get((sku_code, branch_id), (r.date, ""))[1]
            or latest_branch_hub_by_branch.get(branch_id, (r.date, ""))[1]
            or "KZ-HUB"
        )
        if not _matches_hub_filter(hub_name, hub_filter):
            continue
        m = _month_start(r.date)
        max_forecast = m if max_forecast is None else max(max_forecast, m)

    return min_hist, max_hist, (max_forecast or max_hist)


def _effective_filters_from_overrides(
    *,
    saved_product_filter: dict,
    saved_branch_filter: list[str],
    sku_code: list[str] | None,
    sku_name: list[str] | None,
    brand: list[str] | None,
    category: list[str] | None,
    sub_category: list[str] | None,
    subline: list[str] | None,
    sku_status: list[str] | None,
    branch_name: list[str] | None,
    hub_name: list[str] | None = None,
) -> tuple[dict, list[str], list[str]]:
    saved_branch_fallback = list(saved_branch_filter or saved_product_filter.get("branch_filter", []) or [])
    product_filter = {
        "sku_codes": list(saved_product_filter.get("sku_codes", []) or []),
        "sku_names": list(saved_product_filter.get("sku_names", []) or []),
        "brands": list(saved_product_filter.get("brands", []) or []),
        "categories": list(saved_product_filter.get("categories", []) or []),
        "sub_categories": list(saved_product_filter.get("sub_categories", []) or []),
        "sublines": list(saved_product_filter.get("sublines", []) or []),
        "sku_statuses": list(saved_product_filter.get("sku_statuses", []) or []),
    }
    if sku_code is not None:
        product_filter["sku_codes"] = _clean_list(sku_code)
    if sku_name is not None:
        product_filter["sku_names"] = _clean_list(sku_name)
    if brand is not None:
        product_filter["brands"] = _clean_list(brand)
    if category is not None:
        product_filter["categories"] = _clean_list(category)
    if sub_category is not None:
        product_filter["sub_categories"] = _clean_list(sub_category)
    if subline is not None:
        product_filter["sublines"] = _clean_list(subline)
    if sku_status is not None:
        product_filter["sku_statuses"] = _clean_list(sku_status)

    if branch_name is None:
        effective_branch_filter = [
            str(localize_branch_name(v) or v) for v in saved_branch_fallback
        ]
    else:
        effective_branch_filter = [
            str(localize_branch_name(v) or v) for v in _clean_list(branch_name)
        ]
    effective_hub_filter = parse_hub_filter(hub_name)
    return product_filter, effective_branch_filter, effective_hub_filter


async def _build_report_detail(
    db: DBSession,
    report: DPReport,
    *,
    view_type_override: str | None = None,
    date_from_override: date | None = None,
    date_to_override: date | None = None,
    sku_code: list[str] | None = None,
    sku_name: list[str] | None = None,
    brand: list[str] | None = None,
    category: list[str] | None = None,
    sub_category: list[str] | None = None,
    subline: list[str] | None = None,
    sku_status: list[str] | None = None,
    branch_name: list[str] | None = None,
    hub_name: list[str] | None = None,
    project_by_view_type: bool = False,
    ignore_saved_product_filter: bool = False,
    ignore_saved_branch_filter: bool = False,
) -> dict:
    owner_user_id = int(report.created_by_id or 0)
    saved_product_filter = (
        {}
        if ignore_saved_product_filter
        else parse_product_filter(report.product_filter_json or report.product_filter)
    )
    saved_branch_filter = [] if ignore_saved_branch_filter else parse_branch_filter(report.branch_filter_json or report.branch_filter)
    effective_product_filter, effective_branch_filter, effective_hub_filter = _effective_filters_from_overrides(
        saved_product_filter=saved_product_filter,
        saved_branch_filter=saved_branch_filter,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        sku_status=sku_status,
        branch_name=branch_name,
        hub_name=hub_name,
    )
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=owner_user_id,
        view_type=view_type_override or report.view_type,
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        hub_filter=effective_hub_filter,
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

    min_hist_month, max_hist_month_available, max_available_month = await _compute_min_max_dates(
        db=db,
        owner_user_id=owner_user_id,
        product_filter=ctx.product_filter,
        branch_filter=ctx.branch_filter,
        hub_filter=ctx.hub_filter,
    )

    if max_available_month is not None:
        requested_from = _month_start(ctx.date_from)
        requested_to = _month_start(ctx.date_to)
        max_hist_month = max_hist_month_available or _add_months(_month_start(ctx.planning_month), -1)

        include_historical = requested_from <= max_hist_month
        include_forecast = requested_to > max_hist_month

        if include_historical:
            hist_end = min(requested_to, max_hist_month)
            historical_table = _fill_projected_historical_months(historical_table, requested_from, hist_end)
        else:
            historical_table = []

        if include_forecast:
            forecast_start = max(requested_from, _add_months(max_hist_month, 1))
            forecast_table = _fill_projected_forecast_months(forecast_table, forecast_start, requested_to)
        else:
            forecast_table = []

    card = report_card_payload(report)
    branch_options = await build_branch_filter_options(
        db=db,
        owner_user_id=owner_user_id,
        product_filter=ctx.product_filter,
        hub_filter=ctx.hub_filter,
    )
    hub_options = await build_hub_filter_options(
        db=db,
        owner_user_id=owner_user_id,
        product_filter=ctx.product_filter,
    )
    sku_status_options = await build_sku_status_filter_options(
        db=db,
        owner_user_id=owner_user_id,
        product_filter=ctx.product_filter,
        branch_filter=ctx.branch_filter,
        hub_filter=ctx.hub_filter,
    )
    return {
        "report": {
            "report_id": card["report_id"],
            "report_name": card["report_name"],
            "product_filter": ProductFilterPayload(**ctx.product_filter),
            "branch_filter": branch_options,
            "hub_filter": hub_options,
            "sku_status_filter": sku_status_options,
            "view_type": ctx.view_type,
            "date_from": ctx.date_from,
            "date_to": ctx.date_to,
            "is_draft": card["is_draft"],
            "planning_month": ctx.planning_month,
        },
        "min_date": _month_iso(min_hist_month) if min_hist_month else None,
        "max_date": _month_iso(max_available_month) if max_available_month else None,
        "historical_table": historical_table,
        "forecast_table": forecast_table,
    }


def _matches_override_scope(
    override: DPReportForecastOverride,
    *,
    branch_names: set[str],
    brands: set[str],
    categories: set[str],
    sub_categories: set[str],
    sublines: set[str],
    sku_names: set[str],
) -> bool:
    if branch_names and override.branch_name is not None and override.branch_name not in branch_names:
        return False
    if brands and override.brand is not None and override.brand not in brands:
        return False
    if categories and override.category is not None and override.category not in categories:
        return False
    if sub_categories and override.sub_category is not None and override.sub_category not in sub_categories:
        return False
    if sublines and override.subline is not None and override.subline not in sublines:
        return False
    if sku_names and override.sku_name is not None and override.sku_name not in sku_names:
        return False
    return True


def _aggregate_adjustment_reasons(rows: list[DPReportForecastOverride]) -> dict[str, str]:
    by_period: dict[str, list[str]] = {}
    for row in rows:
        reason = str(row.adjustment_reason or "").strip()
        if not reason:
            continue
        period_key = _month_start(row.period).isoformat()
        bucket = by_period.setdefault(period_key, [])
        if reason not in bucket:
            bucket.append(reason)
    return {period: ", ".join(reasons) for period, reasons in by_period.items() if reasons}


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
    view_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sku_status: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
    hub_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    planning_month = await get_current_planning_month(db, user.id)
    default_from, default_to = default_period_for_planning(planning_month)
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    effective_product_filter, effective_branch_filter, effective_hub_filter = _effective_filters_from_overrides(
        saved_product_filter={},
        saved_branch_filter=[],
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        sku_status=sku_status,
        branch_name=branch_name,
        hub_name=hub_name,
    )
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=user.id,
        view_type=view_type or "cases",
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        hub_filter=effective_hub_filter,
        planning_month=planning_month,
        date_from=parsed_date_from or default_from,
        date_to=parsed_date_to or default_to,
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
            product_filter=ProductFilterPayload(**ctx.product_filter),
            branch_filter=await build_branch_filter_options(
                db=db,
                owner_user_id=user.id,
                product_filter=ctx.product_filter,
                hub_filter=ctx.hub_filter,
            ),
            hub_filter=await build_hub_filter_options(
                db=db,
                owner_user_id=user.id,
                product_filter=ctx.product_filter,
            ),
            sku_status_filter=await build_sku_status_filter_options(
                db=db,
                owner_user_id=user.id,
                product_filter=ctx.product_filter,
                branch_filter=ctx.branch_filter,
                hub_filter=ctx.hub_filter,
            ),
            view_type=ctx.view_type,
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
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sku_status: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
    hub_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    effective_product_filter, effective_branch_filter, effective_hub_filter = _effective_filters_from_overrides(
        saved_product_filter={},
        saved_branch_filter=[],
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        sku_status=sku_status,
        branch_name=branch_name,
        hub_name=hub_name,
    )

    planning_month = await get_current_planning_month(db, user.id)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=user.id,
        view_type=view_type or "cases",
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        hub_filter=effective_hub_filter,
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
                status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден"
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
            branch_filter=await build_branch_filter_options(
                db=db,
                owner_user_id=owner_user_id,
                product_filter=ctx.product_filter,
                hub_filter=ctx.hub_filter,
            ),
            hub_filter=await build_hub_filter_options(
                db=db,
                owner_user_id=owner_user_id,
                product_filter=ctx.product_filter,
            ),
            sku_status_filter=await build_sku_status_filter_options(
                db=db,
                owner_user_id=owner_user_id,
                product_filter=ctx.product_filter,
                branch_filter=ctx.branch_filter,
                hub_filter=ctx.hub_filter,
            ),
            view_type=ctx.view_type,
            date_from=ctx.date_from,
            date_to=ctx.date_to,
            is_draft=(base_report.is_draft if base_report else payload.is_draft),
            planning_month=ctx.planning_month,
        ),
        historical_table=projected_historical,
        forecast_table=projected_forecast,
    )


@router.get("/{report_id:int}", response_model=ReportDetailProjectedResponse)
async def get_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    view_type: str | None = Query(
        default=None,
        description="Transient projection filter for this GET only. Values: DSP, Invoice price, Cases, Gross weight, Net weight.",
    ),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sku_status: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
    hub_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден")
    payload_out = await _build_report_detail(
        db,
        report,
        view_type_override=view_type,
        date_from_override=parsed_date_from,
        date_to_override=parsed_date_to,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        sku_status=sku_status,
        branch_name=branch_name,
        hub_name=hub_name,
        project_by_view_type=True,
        ignore_saved_product_filter=True,
        ignore_saved_branch_filter=True,
    )
    return ReportDetailProjectedResponse(**payload_out)


@router.get("/override/", response_model=ReportOverrideResponse, include_in_schema=False)
@router.get("/override", response_model=ReportOverrideResponse)
async def get_report_override(
    db: DBSession,
    user: CurrentUser,
    report_id: int = Query(...),
    view_type: str | None = Query(
        default=None,
        description="Transient projection filter for this GET only. Values: DSP, Invoice price, Cases, Gross weight, Net weight.",
    ),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sublines: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
) -> ReportOverrideResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден")

    merged_subline = (subline or []) + (sublines or [])
    payload_out = await _build_report_detail(
        db,
        report,
        view_type_override=view_type,
        date_from_override=parsed_date_from,
        date_to_override=parsed_date_to,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=merged_subline or None,
        branch_name=branch_name,
        project_by_view_type=True,
        ignore_saved_product_filter=True,
        ignore_saved_branch_filter=True,
    )

    _, effective_branch_filter, _ = _effective_filters_from_overrides(
        saved_product_filter={},
        saved_branch_filter=[],
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=merged_subline or None,
        sku_status=None,
        branch_name=branch_name,
        hub_name=None,
    )

    metric_type = normalize_override_metric(payload_out["report"]["view_type"]) or ""
    effective_filter_obj = payload_out["report"]["product_filter"]
    effective_filter = (
        effective_filter_obj.model_dump()
        if hasattr(effective_filter_obj, "model_dump")
        else dict(effective_filter_obj or {})
    )
    branch_names_scope = {str(v).strip() for v in effective_branch_filter if str(v).strip()}
    effective_sku_names = {
        str(v).strip() for v in (effective_filter.get("sku_names") or []) if str(v).strip()
    }
    effective_sku_names.update(
        await _resolve_sku_names_from_codes(
            db=db,
            owner_user_id=int(report.created_by_id or user.id),
            sku_codes=list(effective_filter.get("sku_codes") or []),
        )
    )
    override_rows = (
        await db.execute(
            select(DPReportForecastOverride).where(
                DPReportForecastOverride.report_id == report.id,
                DPReportForecastOverride.owner_user_id == int(report.created_by_id or user.id),
                DPReportForecastOverride.metric_type == metric_type,
            )
        )
    ).scalars().all()
    scoped_override_rows = [
        row
        for row in override_rows
        if _matches_override_scope(
            row,
            branch_names=branch_names_scope,
            brands={str(v).strip() for v in (effective_filter.get("brands") or []) if str(v).strip()},
            categories={str(v).strip() for v in (effective_filter.get("categories") or []) if str(v).strip()},
            sub_categories={str(v).strip() for v in (effective_filter.get("sub_categories") or []) if str(v).strip()},
            sublines={str(v).strip() for v in (effective_filter.get("sublines") or []) if str(v).strip()},
            sku_names=effective_sku_names,
        )
    ]
    reasons_by_period = _aggregate_adjustment_reasons(scoped_override_rows)
    rows_out: list[ReportOverrideRow] = []
    for row in payload_out["forecast_table"]:
        period = str(row.get("period") or "")
        baseline_value = round(float(row.get("baseline_forecast_value", 0.0) or 0.0), 2)
        adjusted_value = round(float(row.get("adjusted_forecast_value", 0.0) or 0.0), 2)
        rows_out.append(
            ReportOverrideRow(
                period=period,
                baseline_forecast_value=baseline_value,
                adjusted_forecast_value=adjusted_value,
                adjustment_reason=reasons_by_period.get(period),
            )
        )

    return ReportOverrideResponse(
        report=ReportCard(**payload_out["report"]),
        min_date=payload_out.get("min_date"),
        max_date=payload_out.get("max_date"),
        forecast_table=rows_out,
    )


@router.patch("/{report_id:int}", response_model=ReportDetailProjectedResponse)
async def update_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    payload: ReportPatchPayload,
    view_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sku_status: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
) -> ReportDetailProjectedResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден")
    if not is_admin(user) and report.created_by_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    saved_product_filter = parse_product_filter(report.product_filter_json or report.product_filter)
    saved_branch_filter = parse_branch_filter(report.branch_filter_json or report.branch_filter)
    effective_product_filter, effective_branch_filter, effective_hub_filter = _effective_filters_from_overrides(
        saved_product_filter=saved_product_filter,
        saved_branch_filter=saved_branch_filter,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        sku_status=sku_status,
        branch_name=branch_name,
    )

    ctx = await build_reporting_context(
        db=db,
        owner_user_id=int(report.created_by_id or user.id),
        view_type=view_type or report.view_type,
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        hub_filter=effective_hub_filter,
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
            "invoice price",
            "cases",
            "gross weight",
            "net weight",
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Параметр view_type должен быть передан в query (DSP, Invoice Price, Cases, Gross Weight, Net Weight) или быть валидным в отчете",
            )
        override_sku_name = await _resolve_override_sku_name(
            db=db,
            owner_user_id=int(report.created_by_id or user.id),
            sku_code=sku_code,
            sku_name=sku_name,
        )
        overrides = [
            {
                "period": adj.period,
                "metric_type": normalized_metric,
                "value": adj.value,
                "adjustment_reason": adj.adjustment_reason,
                "branch_name": _single_filter_value(branch_name),
                "brand": _single_filter_value(brand),
                "category": _single_filter_value(category),
                "sub_category": _single_filter_value(sub_category),
                "subline": _single_filter_value(subline),
                "sku_name": override_sku_name,
            }
            for adj in payload.forecast_adjustments
        ]
        await upsert_report_overrides(
            db=db,
            report_id=report.id,
            owner_user_id=int(report.created_by_id or user.id),
            overrides=overrides,
        )
    await db.commit()
    await db.refresh(report)
    payload_out = await _build_report_detail(db, report, project_by_view_type=True)
    return ReportDetailProjectedResponse(**payload_out)


@router.delete("/{report_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
) -> None:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        return
    if not is_admin(user) and report.created_by_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    report.is_deleted = True
    await db.commit()


@router.get("/{report_id:int}/access", response_model=List[ReportAccessOut])
async def list_report_access(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
) -> List[ReportAccessOut]:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден")
    rows = (
        await db.execute(select(DPReportAccess).where(DPReportAccess.report_id == report_id))
    ).scalars().all()
    return [
        ReportAccessOut(user_id=r.user_id, report_id=r.report_id, granted_by_id=r.granted_by_id)
        for r in rows
    ]


@router.post(
    "/{report_id:int}/access",
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден")
    target_user = await db.get(User, payload.user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")
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
    "/{report_id:int}/access/{user_id:int}",
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

