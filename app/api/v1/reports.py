import asyncio
from datetime import date
from io import BytesIO
from typing import List

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, exists, func, or_, select

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin, require_roles
from app.core.branch_localization import localize_branch_name
from app.core.database import AsyncSessionLocal
from app.core.response_cache import clear_response_cache
from app.core.ttl_cache import AsyncTTLCache
from app.models.data_uploads import Branch, HistoricalSalesMonthly, Product
from app.models.derived import ForecastSalesMonthly
from app.models.reporting import DPReport, DPReportAccess, DPReportForecastOverride, PromoActivity
from app.models.user import User, UserRole
from app.services.reporting_service import (
    build_branch_filter_options,
    build_report_dimensional_tables,
    build_report_tables,
    build_hub_filter_options,
    build_reporting_context,
    build_sku_status_filter_options,
    clear_reporting_service_caches,
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
from app.services.promo_service import (
    compute_promo_values,
    format_promo_date,
    load_owner_promos,
    load_promo_dropdowns,
    normalize_promo_filters,
    parse_promo_date,
    parse_promo_list,
    serialize_promo_list,
)

router = APIRouter(prefix="/reports", tags=["reports"])
_report_detail_cache: AsyncTTLCache[dict] = AsyncTTLCache(ttl_seconds=60.0, maxsize=96)
_report_minmax_cache: AsyncTTLCache[tuple[date | None, date | None, date | None]] = AsyncTTLCache(
    ttl_seconds=120.0,
    maxsize=128,
)


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


class PromoActivityRow(BaseModel):
    promo_id: int
    promo_name: str
    promo_channel: str | None = None
    promo_branches: list[str]
    promo_sku_codes: list[str]
    promo_start_date: str
    promo_end_date: str
    fact_value: float
    baseline_forecast_value: float
    promo_effect: float
    promo_plan_value: float
    promo_is_active: bool


class PromoActivityTemplate(BaseModel):
    promo_id: int
    promo_name: str
    promo_channel: str | None = None
    promo_branches: list[str]
    promo_sku_codes: list[str]
    promo_start_date: str
    promo_end_date: str
    promo_effect: float
    promo_is_active: bool


class PromoActivityListResponse(BaseModel):
    rows: list[PromoActivityRow]
    available_promos: list[PromoActivityTemplate] = Field(default_factory=list)
    branch_options: list[str] = Field(default_factory=list)
    sku_code_options: list[str] = Field(default_factory=list)


class PromoActivityCreate(BaseModel):
    promo_name: str
    promo_channel: str | None = None
    promo_branches: list[str]
    promo_sku_codes: list[str]
    promo_start_date: str
    promo_end_date: str
    promo_effect: float
    promo_is_active: bool = False
    fact_value: float | None = None
    baseline_forecast_value: float | None = None
    promo_plan_value: float | None = None

    model_config = {"extra": "forbid"}


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

    def _project_forecast_value(value: float) -> float:
        numeric = float(value or 0.0)
        if normalized_view == "cases":
            if numeric >= 1:
                return float(int(round(numeric)))
            if 0 < numeric < 1:
                return round(numeric, 2)
        return round(numeric, 2)

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
                "baseline_forecast_value": _project_forecast_value(
                    float(row.get(baseline_key, 0.0) or 0.0)
                ),
                "adjusted_forecast_value": _project_forecast_value(
                    float(row.get(adjusted_key, row.get(baseline_key, 0.0)) or 0.0)
                ),
                "future_available_stock": round(float(row.get(forecast_stock_key, 0.0) or 0.0), 2),
                "future_hub_stock": round(float(row.get(forecast_hub_stock_key, 0.0) or 0.0), 2),
            }
        )

    return projected_historical, projected_forecast


_RU_MONTH_NAMES = {
    1: "Янв",
    2: "Фев",
    3: "Мар",
    4: "Апр",
    5: "Май",
    6: "Июн",
    7: "Июл",
    8: "Авг",
    9: "Сен",
    10: "Окт",
    11: "Ноя",
    12: "Дек",
}


def _project_export_value(value: float, view_type: str) -> float:
    numeric = float(value or 0.0)
    if (view_type or "").strip().lower() == "cases":
        if numeric >= 1:
            return float(int(round(numeric)))
        if 0 < numeric < 1:
            return round(numeric, 2)
    return round(numeric, 2)


def _report_export_month_label(prefix: str, month: date) -> str:
    return f"{prefix} {_RU_MONTH_NAMES[month.month]} {month.year}"


def _report_export_metric_keys(view_type: str) -> tuple[str, str]:
    suffix = _view_metric_suffix(view_type)
    return f"fact_{suffix}", f"adjusted_forecast_{suffix}"


def _build_report_export_rows(
    *,
    historical_table: list[dict],
    forecast_table: list[dict],
    view_type: str,
    planning_month: date,
) -> tuple[list[dict], list[str]]:
    hist_months = [_add_months(planning_month, offset) for offset in range(-12, 0)]
    forecast_months = [_add_months(planning_month, offset) for offset in range(0, 12)]
    fact_key, forecast_key = _report_export_metric_keys(view_type)
    static_columns = [
        "Склад",
        "Бренд",
        "Категория",
        "Подкатегория",
        "Сублинейка",
        "Код СКЮ",
        "Наименование",
    ]
    hist_columns = [_report_export_month_label("Факт", month) for month in hist_months]
    forecast_columns = [_report_export_month_label("Прогноз", month) for month in forecast_months]
    columns = static_columns + hist_columns + forecast_columns
    buckets: dict[tuple[str, str, str, str, str, str, str], dict] = {}

    def _bucket(row: dict) -> dict:
        key = (
            str(row.get("branch_name") or ""),
            str(row.get("brand") or ""),
            str(row.get("category") or ""),
            str(row.get("sub_category") or ""),
            str(row.get("subline") or ""),
            str(row.get("sku_code") or ""),
            str(row.get("sku_name") or ""),
        )
        if key not in buckets:
            buckets[key] = {
                "Склад": key[0],
                "Бренд": key[1],
                "Категория": key[2],
                "Подкатегория": key[3],
                "Сублинейка": key[4],
                "Код СКЮ": key[5],
                "Наименование": key[6],
                **{column: 0.0 for column in hist_columns + forecast_columns},
            }
        return buckets[key]

    hist_month_set = {month.isoformat(): month for month in hist_months}
    forecast_month_set = {month.isoformat(): month for month in forecast_months}
    for row in historical_table:
        period = str(row.get("period") or "")
        month = hist_month_set.get(period)
        if month is None:
            continue
        export_row = _bucket(row)
        column = _report_export_month_label("Факт", month)
        export_row[column] = _project_export_value(float(export_row[column]) + float(row.get(fact_key, 0.0) or 0.0), view_type)

    for row in forecast_table:
        period = str(row.get("period") or "")
        month = forecast_month_set.get(period)
        if month is None:
            continue
        export_row = _bucket(row)
        column = _report_export_month_label("Прогноз", month)
        export_row[column] = _project_export_value(
            float(export_row[column]) + float(row.get(forecast_key, 0.0) or 0.0),
            view_type,
        )

    rows = sorted(
        buckets.values(),
        key=lambda row: (
            str(row.get("Склад") or ""),
            str(row.get("Код СКЮ") or ""),
            str(row.get("Наименование") or ""),
        ),
    )
    return rows, columns


async def _build_report_download_workbook(
    db: DBSession,
    report: DPReport,
    *,
    view_type_override: str | None = None,
    sku_code: list[str] | None = None,
    sku_name: list[str] | None = None,
    brand: list[str] | None = None,
    category: list[str] | None = None,
    sub_category: list[str] | None = None,
    subline: list[str] | None = None,
    sku_status: list[str] | None = None,
    branch_name: list[str] | None = None,
    hub_name: list[str] | None = None,
) -> BytesIO:
    report_product_filter = parse_product_filter(report.product_filter)
    effective_product_filter, effective_branch_filter, effective_hub_filter = _effective_filters_from_overrides(
        saved_product_filter=report_product_filter,
        saved_branch_filter=parse_branch_filter(report.branch_filter),
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
    planning_month = _resolve_report_planning_month(report) or await get_current_planning_month(db, int(report.created_by_id))
    export_from = _add_months(planning_month, -12)
    export_to = _add_months(planning_month, 11)
    ctx = await build_reporting_context(
        db=db,
        owner_user_id=int(report.created_by_id),
        view_type=view_type_override or report.view_type,
        product_filter=effective_product_filter,
        branch_filter=effective_branch_filter,
        hub_filter=effective_hub_filter,
        planning_month=planning_month,
        date_from=export_from,
        date_to=export_to,
    )
    historical_table, forecast_table = await build_report_dimensional_tables(
        db=db,
        owner_user_id=int(report.created_by_id),
        ctx=ctx,
        report_id=int(report.id),
    )
    rows, columns = _build_report_export_rows(
        historical_table=historical_table,
        forecast_table=forecast_table,
        view_type=ctx.view_type,
        planning_month=planning_month,
    )
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(rows, columns=columns).to_excel(writer, index=False, sheet_name="report")
    output.seek(0)
    return output


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


def _user_cache_scope(user: CurrentUser) -> tuple[str, int]:
    return ("admin" if is_admin(user) else "user", int(user.id))


def _cache_list(values: list[str] | None) -> tuple[str, ...]:
    return tuple(sorted(str(v).strip() for v in (values or []) if str(v).strip()))


def _cache_product_filter(product_filter: dict) -> tuple:
    return tuple(
        (key, _cache_list(product_filter.get(key) or []))
        for key in (
            "sku_codes",
            "sku_names",
            "brands",
            "categories",
            "sub_categories",
            "sublines",
            "sku_statuses",
        )
    )


def _cache_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


async def clear_report_cache() -> None:
    await _report_detail_cache.clear()
    await _report_minmax_cache.clear()
    await clear_reporting_service_caches()
    await clear_response_cache()
    from app.api.v1.dashboard import clear_dashboard_cache
    from app.api.v1.distribution import clear_distribution_cache
    from app.api.v1.inventory_health import clear_inventory_health_cache

    await clear_dashboard_cache()
    await clear_distribution_cache()
    await clear_inventory_health_cache()


def _promo_template(row: PromoActivity) -> PromoActivityTemplate:
    return PromoActivityTemplate(
        promo_id=row.id,
        promo_name=row.promo_name,
        promo_channel=row.promo_channel,
        promo_branches=parse_promo_list(row.promo_branches),
        promo_sku_codes=parse_promo_list(row.promo_sku_codes),
        promo_start_date=format_promo_date(row.promo_start_date),
        promo_end_date=format_promo_date(row.promo_end_date),
        promo_effect=round(float(row.promo_effect_cases or 0.0), 2),
        promo_is_active=bool(row.promo_is_active),
    )


async def _promo_activity_row(
    db: DBSession,
    *,
    owner_user_id: int,
    promo: PromoActivity,
    view_type: str,
    filters,
    all_promos: list[PromoActivity],
) -> PromoActivityRow | None:
    values = await compute_promo_values(
        db,
        owner_user_id=owner_user_id,
        promo=promo,
        view_type=view_type,
        filters=filters,
        all_promos=all_promos,
        include_overlaps=True,
    )
    if values is None:
        return None
    return PromoActivityRow(
        promo_id=promo.id,
        promo_name=promo.promo_name,
        promo_channel=promo.promo_channel,
        promo_branches=parse_promo_list(promo.promo_branches),
        promo_sku_codes=parse_promo_list(promo.promo_sku_codes),
        promo_start_date=format_promo_date(promo.promo_start_date),
        promo_end_date=format_promo_date(promo.promo_end_date),
        fact_value=values.fact_value,
        baseline_forecast_value=values.baseline_forecast_value,
        promo_effect=values.promo_effect,
        promo_plan_value=values.promo_plan_value,
        promo_is_active=bool(promo.promo_is_active),
    )


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


async def _latest_previous_report_for_owner(
    db: DBSession,
    owner_user_id: int,
) -> DPReport | None:
    return (
        await db.execute(
            select(DPReport)
            .where(
                DPReport.created_by_id == owner_user_id,
                DPReport.is_deleted.is_(False),
            )
            .order_by(DPReport.created_at.desc(), DPReport.id.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
    ).scalar_one_or_none()


async def _overrides_from_latest_previous_report(
    db: DBSession,
    *,
    owner_user_id: int,
    date_from: date,
    date_to: date,
) -> list[dict]:
    latest_report = await _latest_previous_report_for_owner(db, owner_user_id)
    if latest_report is None:
        return []
    rows = (
        await db.execute(
            select(DPReportForecastOverride).where(
                DPReportForecastOverride.report_id == latest_report.id,
                DPReportForecastOverride.owner_user_id == owner_user_id,
                DPReportForecastOverride.period >= _month_start(date_from),
                DPReportForecastOverride.period <= _month_start(date_to),
            )
        )
    ).scalars().all()
    return [
        {
            "period": _month_start(row.period),
            "metric_type": row.metric_type,
            "value": float(row.value or 0.0),
            "adjustment_reason": row.adjustment_reason,
            "branch_name": row.branch_name,
            "brand": row.brand,
            "category": row.category,
            "sub_category": row.sub_category,
            "subline": row.subline,
            "sku_name": row.sku_name,
        }
        for row in rows
    ]


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


def _period_bounds_from_tables(*tables: list[dict]) -> tuple[date | None, date | None]:
    months: list[date] = []
    for table in tables:
        for row in table:
            raw_period = str(row.get("period") or "").strip()
            if not raw_period:
                continue
            try:
                months.append(_month_start(date.fromisoformat(raw_period)))
            except ValueError:
                continue
    if not months:
        return None, None
    return min(months), max(months)


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
    sku_statuses = {str(v).strip().lower() for v in (product_filter.get("sku_statuses") or []) if str(v).strip()}
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
    if sku_statuses and str(product.status or "").strip().lower() not in sku_statuses:
        return False
    return True


def _has_product_filters(product_filter: dict) -> bool:
    return any(
        str(v).strip()
        for key in (
            "sku_codes",
            "sku_names",
            "brands",
            "categories",
            "sub_categories",
            "sublines",
            "sku_statuses",
        )
        for v in (product_filter.get(key) or [])
    )


def _allowed_sku_codes_for_product_filter(product_by_code: dict[str, Product], product_filter: dict) -> set[str] | None:
    if not _has_product_filters(product_filter):
        return None
    return {
        sku_code
        for sku_code, product in product_by_code.items()
        if _matches_applied_filters(product, "", product_filter, [])
    }


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
    key = (
        "report-minmax",
        owner_user_id,
        _cache_product_filter(product_filter),
        _cache_list(branch_filter),
        _cache_list(hub_filter),
    )
    return await _report_minmax_cache.get_or_set(
        key,
        lambda: _compute_min_max_dates_uncached(db, owner_user_id, product_filter, branch_filter, hub_filter),
    )


async def _compute_min_max_dates_uncached(
    db: DBSession,
    owner_user_id: int,
    product_filter: dict,
    branch_filter: list[str],
    hub_filter: list[str],
) -> tuple[date | None, date | None, date | None]:
    if not _has_product_filters(product_filter) and not branch_filter and not hub_filter:
        hist_min, hist_max = (
            await db.execute(
                select(
                    func.min(HistoricalSalesMonthly.date),
                    func.max(HistoricalSalesMonthly.date),
                ).where(
                    HistoricalSalesMonthly.owner_user_id == owner_user_id,
                    HistoricalSalesMonthly.branch_id != "",
                )
            )
        ).one()
        forecast_max = (
            await db.execute(
                select(func.max(ForecastSalesMonthly.date)).where(
                    ForecastSalesMonthly.owner_user_id == owner_user_id
                )
            )
        ).scalar_one_or_none()
        min_hist = _month_start(hist_min) if hist_min else None
        max_hist = _month_start(hist_max) if hist_max else None
        max_forecast = _month_start(forecast_max) if forecast_max else None
        return min_hist, max_hist, (max_forecast or max_hist)

    products = (
        await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
    ).scalars().all()
    product_by_code = {str(p.sku_code or "").strip(): p for p in products}
    allowed_sku_codes = _allowed_sku_codes_for_product_filter(product_by_code, product_filter)
    if allowed_sku_codes == set():
        return None, None, None
    branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    branch_by_id = {str(b.branch_id).strip(): str(b.branch_name) for b in branches}
    latest_branch_hub_by_sku_branch: dict[tuple[str, str], tuple[date, str]] = {}
    latest_branch_hub_by_branch: dict[str, tuple[date, str]] = {}

    hist_stmt = select(HistoricalSalesMonthly).where(HistoricalSalesMonthly.owner_user_id == owner_user_id)
    forecast_stmt = select(ForecastSalesMonthly).where(ForecastSalesMonthly.owner_user_id == owner_user_id)
    if allowed_sku_codes is not None:
        hist_stmt = hist_stmt.where(HistoricalSalesMonthly.sku_code.in_(allowed_sku_codes))
        forecast_stmt = forecast_stmt.where(ForecastSalesMonthly.sku_code.in_(allowed_sku_codes))
    hist_rows = (
        await db.execute(hist_stmt)
    ).scalars().all()
    forecast_rows = (
        await db.execute(forecast_stmt)
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


async def _compute_min_max_dates_isolated(
    owner_user_id: int,
    product_filter: dict,
    branch_filter: list[str],
    hub_filter: list[str],
) -> tuple[date | None, date | None, date | None]:
    async with AsyncSessionLocal() as session:
        return await _compute_min_max_dates(
            db=session,
            owner_user_id=owner_user_id,
            product_filter=product_filter,
            branch_filter=branch_filter,
            hub_filter=hub_filter,
        )


async def _build_branch_filter_options_isolated(
    owner_user_id: int,
    product_filter: dict,
    hub_filter: list[str],
) -> list[str]:
    async with AsyncSessionLocal() as session:
        return await build_branch_filter_options(
            db=session,
            owner_user_id=owner_user_id,
            product_filter=product_filter,
            hub_filter=hub_filter,
        )


async def _build_hub_filter_options_isolated(
    owner_user_id: int,
    product_filter: dict,
) -> list[str]:
    async with AsyncSessionLocal() as session:
        return await build_hub_filter_options(
            db=session,
            owner_user_id=owner_user_id,
            product_filter=product_filter,
        )


async def _build_sku_status_filter_options_isolated(
    owner_user_id: int,
    product_filter: dict,
    branch_filter: list[str],
    hub_filter: list[str],
) -> list[str]:
    async with AsyncSessionLocal() as session:
        return await build_sku_status_filter_options(
            db=session,
            owner_user_id=owner_user_id,
            product_filter=product_filter,
            branch_filter=branch_filter,
            hub_filter=hub_filter,
        )


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
    # Report detail construction is expensive and read-heavy; cache only after access checks.
    report_updated_at = getattr(report, "updated_at", None)
    key = (
        "report-detail",
        int(report.id),
        int(report.created_by_id or 0),
        report_updated_at.isoformat() if report_updated_at is not None else None,
        view_type_override.strip().lower() if view_type_override else None,
        _cache_date(date_from_override),
        _cache_date(date_to_override),
        _cache_list(sku_code),
        _cache_list(sku_name),
        _cache_list(brand),
        _cache_list(category),
        _cache_list(sub_category),
        _cache_list(subline),
        _cache_list(sku_status),
        _cache_list(branch_name),
        _cache_list(hub_name),
        bool(project_by_view_type),
        bool(ignore_saved_product_filter),
        bool(ignore_saved_branch_filter),
    )
    return await _report_detail_cache.get_or_set(
        key,
        lambda: _build_report_detail_uncached(
            db,
            report,
            view_type_override=view_type_override,
            date_from_override=date_from_override,
            date_to_override=date_to_override,
            sku_code=sku_code,
            sku_name=sku_name,
            brand=brand,
            category=category,
            sub_category=sub_category,
            subline=subline,
            sku_status=sku_status,
            branch_name=branch_name,
            hub_name=hub_name,
            project_by_view_type=project_by_view_type,
            ignore_saved_product_filter=ignore_saved_product_filter,
            ignore_saved_branch_filter=ignore_saved_branch_filter,
        ),
    )


async def _build_report_detail_uncached(
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
    (
        (historical_table, forecast_table),
        (min_hist_month, max_hist_month_available, max_available_month),
        branch_options,
        hub_options,
        sku_status_options,
    ) = await asyncio.gather(
        build_report_tables(
            db=db,
            owner_user_id=owner_user_id,
            ctx=ctx,
            report_id=report.id,
        ),
        _compute_min_max_dates_isolated(
            owner_user_id=owner_user_id,
            product_filter=ctx.product_filter,
            branch_filter=ctx.branch_filter,
            hub_filter=ctx.hub_filter,
        ),
        _build_branch_filter_options_isolated(
            owner_user_id=owner_user_id,
            product_filter=ctx.product_filter,
            hub_filter=ctx.hub_filter,
        ),
        _build_hub_filter_options_isolated(
            owner_user_id=owner_user_id,
            product_filter=ctx.product_filter,
        ),
        _build_sku_status_filter_options_isolated(
            owner_user_id=owner_user_id,
            product_filter=ctx.product_filter,
            branch_filter=ctx.branch_filter,
            hub_filter=ctx.hub_filter,
        ),
    )
    if project_by_view_type:
        historical_table, forecast_table = _project_tables_for_view_type(
            historical_table=historical_table,
            forecast_table=forecast_table,
            view_type=ctx.view_type,
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

    response_min_month, response_max_month = _period_bounds_from_tables(
        historical_table,
        forecast_table,
    )
    card = report_card_payload(report)
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
        "min_date": _month_iso(response_min_month) if response_min_month else None,
        "max_date": _month_iso(response_max_month) if response_max_month else None,
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
    inherited_overrides = await _overrides_from_latest_previous_report(
        db,
        owner_user_id=user.id,
        date_from=ctx.date_from,
        date_to=ctx.date_to,
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
    payload_overrides = _forecast_table_to_overrides(
        forecast_table=payload.forecast_table,
        view_type=ctx.view_type,
    )
    await replace_report_overrides(
        db=db,
        report_id=report.id,
        owner_user_id=user.id,
        overrides=[*inherited_overrides, *payload_overrides],
    )
    await db.commit()
    await clear_report_cache()
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


@router.get("/{report_id:int}/download")
async def download_report(
    db: DBSession,
    user: CurrentUser,
    report_id: int,
    view_type: str | None = Query(
        default=None,
        description="Transient projection filter for this download. Values: DSP, Invoice price, Cases, Gross weight, Net weight.",
    ),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sku_status: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
    hub_name: list[str] | None = Query(default=None),
) -> StreamingResponse:
    report = await _get_accessible_report(db, user, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Отчет не найден")
    output = await _build_report_download_workbook(
        db,
        report,
        view_type_override=view_type,
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
    filename = f"report_{report_id}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/promo-activities/", response_model=PromoActivityListResponse)
@router.get("/promo-activities", response_model=PromoActivityListResponse, include_in_schema=False)
async def get_promo_activities(
    db: DBSession,
    user: CurrentUser,
    view_type: str | None = Query(
        default=None,
        description="Projection filter. Values: DSP, Invoice price, Cases, Gross weight, Net weight.",
    ),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sublines: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
) -> PromoActivityListResponse:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    merged_subline = (subline or []) + (sublines or [])
    filters = normalize_promo_filters(
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=merged_subline or None,
        branch_name=branch_name,
    )
    owner_user_id = int(user.id)
    promos = await load_owner_promos(db, owner_user_id)
    rows: list[PromoActivityRow] = []
    for promo in promos:
        row = await _promo_activity_row(
            db,
            owner_user_id=owner_user_id,
            promo=promo,
            view_type=view_type or "cases",
            filters=filters,
            all_promos=promos,
        )
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: (parse_promo_date(row.promo_start_date, field_name="promo_start_date"), row.promo_id))
    branch_options, sku_code_options = await load_promo_dropdowns(db, owner_user_id)
    return PromoActivityListResponse(
        rows=rows,
        available_promos=[_promo_template(promo) for promo in promos],
        branch_options=branch_options,
        sku_code_options=sku_code_options,
    )


@router.post("/promo-activities/", response_model=PromoActivityRow, status_code=status.HTTP_201_CREATED)
@router.post("/promo-activities", response_model=PromoActivityRow, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_promo_activity(
    db: DBSession,
    user: CurrentUser,
    payload: PromoActivityCreate,
    view_type: str | None = Query(
        default=None,
        description="Projection filter for the created row. Values: DSP, Invoice price, Cases, Gross weight, Net weight.",
    ),
) -> PromoActivityRow:
    start_date = parse_promo_date(payload.promo_start_date, field_name="promo_start_date")
    end_date = parse_promo_date(payload.promo_end_date, field_name="promo_end_date")
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="promo_start_date не может быть позже promo_end_date",
        )
    if not [v for v in payload.promo_branches if str(v).strip()]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="promo_branches не может быть пустым",
        )
    if not [v for v in payload.promo_sku_codes if str(v).strip()]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="promo_sku_codes не может быть пустым",
        )

    promo = PromoActivity(
        owner_user_id=int(user.id),
        promo_name=payload.promo_name.strip(),
        promo_channel=payload.promo_channel.strip() if payload.promo_channel else None,
        promo_branches=serialize_promo_list(payload.promo_branches),
        promo_sku_codes=serialize_promo_list(payload.promo_sku_codes),
        promo_start_date=start_date,
        promo_end_date=end_date,
        promo_effect_cases=float(payload.promo_effect or 0.0),
        promo_is_active=bool(payload.promo_is_active),
    )
    if not promo.promo_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="promo_name не может быть пустым",
        )
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    await clear_report_cache()

    promos = await load_owner_promos(db, int(user.id))
    row = await _promo_activity_row(
        db,
        owner_user_id=int(user.id),
        promo=promo,
        view_type=view_type or "cases",
        filters=normalize_promo_filters(),
        all_promos=promos,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Промо активность создана, но не найдены данные для расчета",
        )
    return row


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
    await clear_report_cache()
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
    await clear_report_cache()


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
        await clear_report_cache()
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
        await clear_report_cache()

