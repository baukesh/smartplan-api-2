from datetime import date, datetime
from io import BytesIO
import math

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.core.ttl_cache import AsyncTTLCache
from app.core.branch_localization import normalize_branch_lookup
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product, ProductBranch
from app.models.derived import (
    DistributionBranchAmountAdjustment,
    DistributionSkuAdjustment,
    ForecastSalesMonthly,
)
from app.models.reporting import DPReport, DPReportForecastOverride

router = APIRouter(prefix="/distribution", tags=["distribution"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}
_distribution_calc_cache: AsyncTTLCache[
    tuple[date, list["_BranchSkuCalc"], dict[tuple[int, str], str], dict[tuple[int, str], str]]
] = AsyncTTLCache(ttl_seconds=45.0, maxsize=64)
DISTRIBUTION_DOWNLOAD_HEADERS = {
    "hub_name": "Хаб",
    "branch_name": "Склад",
    "target_amount_dsp_per_branch": "План ₸",
    "available_amount_kzt_per_branch": "Остаток ₸",
    "recommended_amount_kzt_per_branch": "Рекомендуемое распределение ₸",
    "adjusted_amount_kzt_per_branch": "Распределить в кол-ве",
    "total_adjusted_volume_cbm_per_branch": "Объем распределения",
    "total_adjusted_gross_weight_kg_per_branch": "Общий вес распределения",
    "readiness_for_target_per_branch": "Готовность к плану",
}
DISTRIBUTION_DETAILS_DOWNLOAD_HEADERS = {
    "sku_code": "СКЮ код",
    "sku_name": "Наименование товара",
    "total_available_quantity_in_mc": "Наличие товара на хабе",
    "available_quantity_in_mc": "Наличие товара",
    "average_l3m_quantity_in_mc": "Средние продажи за последние 3 мес",
    "average_f3m_quantity_in_mc": "Средние продажи за будущие 3 мес",
    "recommended_quantity_in_mc": "Рекомендуемое кол-во",
    "adjusted_quantity_in_mc": "Распределить в кол-ве",
    "readiness_for_target_per_sku": "Готовность к плану",
}


class DistributionAggregateRow(BaseModel):
    hub_name: str
    branch_name: str
    target_amount_dsp_per_branch: int
    available_amount_kzt_per_branch: int
    recommended_amount_kzt_per_branch: float
    adjusted_amount_kzt_per_branch: float
    total_adjusted_volume_cbm_per_branch: float
    total_adjusted_gross_weight_kg_per_branch: float
    readiness_for_target_per_branch: int


class DistributionAggregateResponse(BaseModel):
    planning_date: str
    items: list[DistributionAggregateRow]
    total_items: int
    total_pages: int
    filter_options: "DistributionAggregateFilterOptions"


class DistributionAggregateFilterOptions(BaseModel):
    branch_name: list[str]
    sku_code: list[str] = Field(default_factory=list)
    sku_name: list[str] = Field(default_factory=list)
    brand: list[str] = Field(default_factory=list)
    category: list[str] = Field(default_factory=list)
    sub_category: list[str] = Field(default_factory=list)
    subline: list[str] = Field(default_factory=list)
    readiness_for_target_per_branch: list[int]


class DistributionSummaryResponse(BaseModel):
    planning_date: str
    total_target_amount_dsp: float
    total_fact_amount_dsp: float
    total_readiness_for_target: int


class DistributionSummaryInformationIconResponse(BaseModel):
    total_target_amount_dsp_text: str
    total_fact_amount_dsp_text: str


class DistributionDetailsSummaryInformationIconResponse(BaseModel):
    total_adjusted_volume_cbm_per_branch_text: str
    total_adjusted_gross_weight_kg_per_branch_text: str


class DistributionDetailsReadinessInformationIconResponse(BaseModel):
    readiness_for_target_per_sku_text: str


class DistributionDetailRow(BaseModel):
    sku_code: str
    sku_name: str
    total_available_quantity_in_mc: int
    available_quantity_in_mc: int
    average_l3m_quantity_in_mc: int
    average_f3m_quantity_in_mc: int
    recommended_quantity_in_mc: int
    adjusted_quantity_in_mc: int
    readiness_for_target_per_sku: int


class DistributionDetailsResponse(BaseModel):
    planning_date: str
    hub_name: str
    branch_name: str
    items: list[DistributionDetailRow]
    total_items: int
    total_pages: int
    filter_options: "DistributionDetailsFilterOptions"


class DistributionDetailsFilterOptions(BaseModel):
    sku_code: list[str]
    sku_name: list[str]
    brand: list[str] = Field(default_factory=list)
    category: list[str] = Field(default_factory=list)
    sub_category: list[str] = Field(default_factory=list)
    subline: list[str] = Field(default_factory=list)
    readiness_for_target_per_sku: list[int]


class DistributionDetailsSummaryResponse(BaseModel):
    planning_date: str
    hub_name: str
    branch_name: str
    total_adjusted_volume_cbm_per_branch: float
    total_adjusted_gross_weight_kg_per_branch: float
    total_adjusted_amount_dsp_per_branch: float


class DistributionBranchAdjustRow(BaseModel):
    branch_name: str
    adjusted_amount_kzt_per_branch: float


class DistributionBranchAdjustRequest(BaseModel):
    updates: list[DistributionBranchAdjustRow]


class DistributionSkuAdjustRow(BaseModel):
    sku_code: str
    adjusted_quantity_in_mc: int | None = None
    adjusted_quantity_in_mc_per_branch: int | None = None


class DistributionSkuAdjustRequest(BaseModel):
    updates: list[DistributionSkuAdjustRow]


class _BranchSkuCalc(BaseModel):
    owner_user_id: int
    branch_id: str
    branch_name: str
    hub_name: str
    sku_id: str
    sku_code: str
    sku_name: str
    brand: str
    category: str
    sub_category: str
    subline: str
    date: date
    target_qty: float
    stock_norm_target_qty: float
    fact_qty: float
    available_qty: float
    total_hub_available_qty: float
    recommended_qty: int
    pieces_in_master_carton: float
    master_carton_volume_cbm: float
    master_carton_gross_weight_kg: float
    dsp: float
    invoice_price: float
    avg_l3m: int
    avg_f3m: int


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр page_size должен быть одним из: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


def _paginate(items: list, page: int, page_size: str) -> tuple[list, int, int]:
    size = _parse_page_size(page_size)
    total_items = len(items)
    if size is None:
        return items, total_items, 1
    total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    offset = (page - 1) * size
    return items[offset : offset + size], total_items, total_pages


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _qty_int(value: float | None) -> int:
    return int(round(float(value or 0.0)))


def _branch_name_matches(query_value: str, branch_name: str, branch_id: str) -> bool:
    return (
        normalize_branch_lookup(query_value) == normalize_branch_lookup(branch_name)
        or str(query_value).strip() == str(branch_id).strip()
    )


PRODUCT_FILTER_FIELDS = {
    "sku_code": "sku_code",
    "sku_name": "sku_name",
    "brand": "brand",
    "category": "category",
    "sub_category": "sub_category",
    "subline": "subline",
}


def _clean_filter_values(values: list[str] | None) -> set[str]:
    return {str(v).strip() for v in (values or []) if str(v).strip()}


def _product_filter_payload(
    *,
    sku_code: list[str] | None = None,
    sku_name: list[str] | None = None,
    brand: list[str] | None = None,
    category: list[str] | None = None,
    sub_category: list[str] | None = None,
    subline: list[str] | None = None,
) -> dict[str, set[str]]:
    return {
        "sku_code": _clean_filter_values(sku_code),
        "sku_name": _clean_filter_values(sku_name),
        "brand": _clean_filter_values(brand),
        "category": _clean_filter_values(category),
        "sub_category": _clean_filter_values(sub_category),
        "subline": _clean_filter_values(subline),
    }


def _matches_product_filters(
    row: _BranchSkuCalc,
    product_filter: dict[str, set[str]],
    *,
    ignore_field: str | None = None,
) -> bool:
    for field_name in PRODUCT_FILTER_FIELDS:
        if field_name == ignore_field:
            continue
        values = product_filter.get(field_name) or set()
        if values and str(getattr(row, field_name) or "").strip() not in values:
            return False
    return True


def _product_filter_options(
    rows: list[_BranchSkuCalc],
    product_filter: dict[str, set[str]],
    *,
    extra_match=None,
) -> dict[str, list[str]]:
    options: dict[str, list[str]] = {}
    for option_name, attr_name in PRODUCT_FILTER_FIELDS.items():
        values: set[str] = set()
        for row in rows:
            if not _matches_product_filters(row, product_filter, ignore_field=option_name):
                continue
            if extra_match is not None and not extra_match(row):
                continue
            value = str(getattr(row, attr_name) or "").strip()
            if value:
                values.add(value)
        options[option_name] = sorted(values)
    return options


def _branch_matches_selected(branch_name: str, branch_filter: list[str] | None) -> bool:
    if not branch_filter:
        return True
    branch_name_values = {
        normalize_branch_lookup(v)
        for v in branch_filter
        if str(v).strip()
    }
    return normalize_branch_lookup(branch_name) in branch_name_values


def _normalize_distribution_view_type(view_type: str | None) -> str:
    normalized = str(view_type or "DSP").strip().lower()
    if normalized not in {"dsp", "invoice price", "cases"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр view_type должен быть одним из: DSP, Invoice price или Cases",
        )
    return normalized


def _pick_price_for_sales_date(prices: list[PriceList], sales_date: date, view_type: str = "dsp") -> float:
    if not prices:
        return 0.0
    sorted_prices = sorted(prices, key=lambda p: p.date)
    selected = None
    for p in sorted_prices:
        if p.date <= sales_date:
            selected = p
    if selected is None:
        # User-selected fallback: earliest price if there is no <= sales_date match.
        selected = sorted_prices[0]
    if _normalize_distribution_view_type(view_type) == "invoice price":
        return float(selected.invoice_price or 0.0)
    return float(selected.dsp or 0.0)


def _pick_dsp_for_sales_date(prices: list[PriceList], sales_date: date) -> float:
    return _pick_price_for_sales_date(prices, sales_date, "dsp")


def _safe_readiness(numerator: float, denominator: float) -> int:
    if denominator <= 0:
        return 0
    value = int(round((numerator / denominator) * 100.0))
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


def _target_amount_dsp(row: _BranchSkuCalc) -> float:
    return float(row.stock_norm_target_qty) * row.pieces_in_master_carton * row.dsp


def _available_amount_dsp(row: _BranchSkuCalc) -> float:
    return float(row.available_qty) * row.pieces_in_master_carton * row.dsp


def _recommended_amount_dsp(row: _BranchSkuCalc) -> float:
    return float(row.recommended_qty) * row.pieces_in_master_carton * row.dsp


def _distribution_display_value(row: _BranchSkuCalc, quantity: float, view_type: str) -> float:
    if view_type == "cases":
        return float(quantity)
    price = row.invoice_price if view_type == "invoice price" else row.dsp
    return float(quantity) * row.pieces_in_master_carton * price


def _target_display_value(row: _BranchSkuCalc, view_type: str) -> float:
    return _distribution_display_value(row, row.stock_norm_target_qty, view_type)


def _available_display_value(row: _BranchSkuCalc, view_type: str) -> float:
    return _distribution_display_value(row, row.available_qty, view_type)


def _recommended_display_value(row: _BranchSkuCalc, view_type: str) -> float:
    return _distribution_display_value(row, row.recommended_qty, view_type)


def _potential_amount_dsp(row: _BranchSkuCalc) -> float:
    target_qty = max(float(row.stock_norm_target_qty), 0.0)
    available_qty = max(float(row.available_qty), 0.0)
    return min(available_qty, target_qty) * row.pieces_in_master_carton * row.dsp


def _adjusted_qty_for_row(
    row: _BranchSkuCalc,
    detail_adj_map: dict[tuple[int, str, str], int],
) -> int:
    adjustment_key = (row.owner_user_id, row.branch_id, row.sku_code)
    if adjustment_key in detail_adj_map:
        return int(detail_adj_map[adjustment_key])
    return int(row.recommended_qty)


def _adjusted_metrics_for_rows(
    rows: list[_BranchSkuCalc],
    detail_adj_map: dict[tuple[int, str, str], int],
) -> tuple[float, float, float]:
    total_volume = 0.0
    total_gross_weight = 0.0
    total_amount = 0.0
    for row in rows:
        adjusted = float(_adjusted_qty_for_row(row, detail_adj_map))
        total_volume += adjusted * row.master_carton_volume_cbm
        total_gross_weight += adjusted * row.master_carton_gross_weight_kg
        total_amount += adjusted * row.pieces_in_master_carton * row.dsp
    return round(total_volume, 2), round(total_gross_weight, 2), round(total_amount, 2)


def _build_aggregate_rows(
    *,
    calc_rows: list[_BranchSkuCalc],
    branch_adj_map: dict[tuple[int, str], float],
    detail_adj_map: dict[tuple[int, str, str], int],
    view_type: str = "dsp",
) -> tuple[
    list[DistributionAggregateRow],
    dict[tuple[int, str, str, str], list[_BranchSkuCalc]],
    dict[tuple[int, str, str, str], DistributionAggregateRow],
]:
    buckets: dict[tuple[int, str, str, str], dict[str, float]] = {}
    rows_by_branch: dict[tuple[int, str, str, str], list[_BranchSkuCalc]] = {}
    for row in calc_rows:
        key = (row.owner_user_id, row.branch_id, row.branch_name, row.hub_name)
        rows_by_branch.setdefault(key, []).append(row)
        if key not in buckets:
            buckets[key] = {
                "available_amount": 0.0,
                "recommended_amount": 0.0,
                "target_amount": 0.0,
                "potential_amount": 0.0,
            }
        bucket = buckets[key]
        bucket["available_amount"] += _available_display_value(row, view_type)
        bucket["recommended_amount"] += _recommended_display_value(row, view_type)
        bucket["target_amount"] += _target_display_value(row, view_type)
        bucket["potential_amount"] += _potential_amount_dsp(row)

    out: list[DistributionAggregateRow] = []
    row_by_key: dict[tuple[int, str, str, str], DistributionAggregateRow] = {}
    for (owner_id, branch_id, branch_name_value, hub_name), vals in buckets.items():
        target_amount = sum(
            _target_amount_dsp(row)
            for row in rows_by_branch.get((owner_id, branch_id, branch_name_value, hub_name), [])
        )
        display_target_amount = float(vals["target_amount"])
        explicit_adjustment_key = (owner_id, branch_id)
        readiness_adjusted_amount = float(branch_adj_map.get(explicit_adjustment_key, 0.0))
        display_adjusted_amount = (
            readiness_adjusted_amount
            if explicit_adjustment_key in branch_adj_map
            else sum(
                _recommended_amount_dsp(row)
                for row in rows_by_branch.get((owner_id, branch_id, branch_name_value, hub_name), [])
            )
        )
        readiness = _safe_readiness(
            numerator=float(vals["potential_amount"]),
            denominator=target_amount,
        )
        adjusted_volume, adjusted_gross_weight, _adjusted_amount = _adjusted_metrics_for_rows(
            rows_by_branch.get((owner_id, branch_id, branch_name_value, hub_name), []),
            detail_adj_map,
        )
        aggregate_row = DistributionAggregateRow(
            hub_name=hub_name,
            branch_name=branch_name_value,
            target_amount_dsp_per_branch=_qty_int(display_target_amount),
            available_amount_kzt_per_branch=_qty_int(vals["available_amount"]),
            recommended_amount_kzt_per_branch=round(float(vals["recommended_amount"]), 2),
            adjusted_amount_kzt_per_branch=round(display_adjusted_amount, 2),
            total_adjusted_volume_cbm_per_branch=adjusted_volume,
            total_adjusted_gross_weight_kg_per_branch=adjusted_gross_weight,
            readiness_for_target_per_branch=int(readiness),
        )
        out.append(aggregate_row)
        row_by_key[(owner_id, branch_id, branch_name_value, hub_name)] = aggregate_row
    return out, rows_by_branch, row_by_key


def _aggregate_filter_options(
    *,
    calc_rows: list[_BranchSkuCalc],
    product_filter: dict[str, set[str]],
    branch_name: list[str] | None,
    readiness_for_target_per_branch: list[int] | None,
    branch_adj_map: dict[tuple[int, str], float],
    detail_adj_map: dict[tuple[int, str, str], int],
) -> DistributionAggregateFilterOptions:
    readiness_values = {int(v) for v in (readiness_for_target_per_branch or [])}
    product_options: dict[str, list[str]] = {}
    for option_name in PRODUCT_FILTER_FIELDS:
        candidate_rows = [
            row
            for row in calc_rows
            if _matches_product_filters(row, product_filter, ignore_field=option_name)
        ]
        _candidate_aggregate_rows, _candidate_rows_by_branch, candidate_row_by_key = _build_aggregate_rows(
            calc_rows=candidate_rows,
            branch_adj_map=branch_adj_map,
            detail_adj_map=detail_adj_map,
        )
        eligible_branch_keys = {
            key
            for key, aggregate_row in candidate_row_by_key.items()
            if _branch_matches_selected(aggregate_row.branch_name, branch_name)
            and (
                not readiness_values
                or int(aggregate_row.readiness_for_target_per_branch) in readiness_values
            )
        }
        values = {
            str(getattr(row, option_name) or "").strip()
            for row in candidate_rows
            if (row.owner_user_id, row.branch_id, row.branch_name, row.hub_name) in eligible_branch_keys
            and str(getattr(row, option_name) or "").strip()
        }
        product_options[option_name] = sorted(values)

    product_filtered_rows = [
        row
        for row in calc_rows
        if _matches_product_filters(row, product_filter)
    ]
    aggregate_rows, _rows_by_branch, _row_by_key = _build_aggregate_rows(
        calc_rows=product_filtered_rows,
        branch_adj_map=branch_adj_map,
        detail_adj_map=detail_adj_map,
    )
    branch_options_rows = [
        row
        for row in aggregate_rows
        if (
            not readiness_for_target_per_branch
            or int(row.readiness_for_target_per_branch)
            in {int(v) for v in readiness_for_target_per_branch}
        )
    ]
    readiness_options_rows = [
        row
        for row in aggregate_rows
        if _branch_matches_selected(row.branch_name, branch_name)
    ]
    return DistributionAggregateFilterOptions(
        branch_name=sorted({row.branch_name for row in branch_options_rows}),
        sku_code=product_options["sku_code"],
        sku_name=product_options["sku_name"],
        brand=product_options["brand"],
        category=product_options["category"],
        sub_category=product_options["sub_category"],
        subline=product_options["subline"],
        readiness_for_target_per_branch=sorted(
            {int(row.readiness_for_target_per_branch) for row in readiness_options_rows}
        ),
    )


def _build_detail_row(
    row: _BranchSkuCalc,
    detail_adj_map: dict[tuple[int, str, str], int],
) -> DistributionDetailRow:
    explicit_adjustment_key = (row.owner_user_id, row.branch_id, row.sku_code)
    readiness_adjusted = int(detail_adj_map.get(explicit_adjustment_key, 0))
    recommended_for_target = int(row.recommended_qty)
    display_adjusted = (
        readiness_adjusted
        if explicit_adjustment_key in detail_adj_map
        else recommended_for_target
    )
    readiness = _safe_readiness(
        numerator=float(row.available_qty) + float(readiness_adjusted),
        denominator=float(row.stock_norm_target_qty),
    )
    return DistributionDetailRow(
        sku_code=row.sku_code,
        sku_name=row.sku_name,
        total_available_quantity_in_mc=int(math.ceil(float(row.total_hub_available_qty))),
        available_quantity_in_mc=int(math.ceil(float(row.available_qty))),
        average_l3m_quantity_in_mc=int(row.avg_l3m),
        average_f3m_quantity_in_mc=int(row.avg_f3m),
        recommended_quantity_in_mc=int(recommended_for_target),
        adjusted_quantity_in_mc=int(display_adjusted),
        readiness_for_target_per_sku=int(readiness),
    )


def _details_filter_options(
    *,
    selected_rows: list[_BranchSkuCalc],
    product_filter: dict[str, set[str]],
    readiness_for_target_per_sku_filter: list[int] | None,
    detail_adj_map: dict[tuple[int, str, str], int],
) -> DistributionDetailsFilterOptions:
    product_options = _product_filter_options(selected_rows, product_filter)
    product_filtered_rows = [
        row
        for row in selected_rows
        if _matches_product_filters(row, product_filter)
    ]
    detail_rows = [_build_detail_row(row, detail_adj_map) for row in product_filtered_rows]
    return DistributionDetailsFilterOptions(
        sku_code=product_options["sku_code"],
        sku_name=product_options["sku_name"],
        brand=product_options["brand"],
        category=product_options["category"],
        sub_category=product_options["sub_category"],
        subline=product_options["subline"],
        readiness_for_target_per_sku=sorted(
            {int(row.readiness_for_target_per_sku) for row in detail_rows}
        ),
    )


def _forecast_target_for_planning_month(
    forecast_qty_by_key: dict[tuple[int, str, str, date], float],
    owner_user_id: int,
    branch_id: str,
    sku_code: str,
    planning_date: date,
) -> float:
    return float(
        forecast_qty_by_key.get(
            (owner_user_id, branch_id, sku_code, _month_start(planning_date)),
            0.0,
        )
    )


def _forecast_target_for_stock_norm(
    forecast_qty_by_key: dict[tuple[int, str, str, date], float],
    owner_user_id: int,
    branch_id: str,
    sku_code: str,
    planning_date: date,
    stock_norm_days: float,
) -> float:
    if stock_norm_days <= 0:
        return 0.0

    target_qty = 0.0
    remaining_days = float(stock_norm_days)
    month_offset = 0
    while remaining_days > 0:
        covered_days = min(30.0, remaining_days)
        month = _add_months(planning_date, month_offset)
        monthly_qty = forecast_qty_by_key.get((owner_user_id, branch_id, sku_code, month), 0.0)
        target_qty += float(monthly_qty) * (covered_days / 30.0)
        remaining_days -= covered_days
        month_offset += 1

    return target_qty


def _latest_report_by_owner(report_rows: list[DPReport]) -> dict[int, DPReport]:
    latest: dict[int, DPReport] = {}
    for report in report_rows:
        owner_id = int(report.created_by_id or 0)
        if owner_id <= 0:
            continue
        current = latest.get(owner_id)
        report_updated = report.created_at or report.updated_at or datetime.min
        current_updated = (
            current.created_at or current.updated_at or datetime.min
            if current is not None
            else datetime.min
        )
        if current is None or (report_updated, int(report.id)) > (current_updated, int(current.id)):
            latest[owner_id] = report
    return latest


def _override_specificity(row: DPReportForecastOverride) -> int:
    return sum(
        1
        for value in [
            row.branch_name,
            row.brand,
            row.category,
            row.sub_category,
            row.subline,
            row.sku_name,
        ]
        if value
    )


def _apply_report_case_overrides_to_forecasts(
    *,
    forecast_atoms: list[dict],
    report_rows: list[DPReport],
    override_rows: list[DPReportForecastOverride],
) -> None:
    latest_reports = _latest_report_by_owner(report_rows)
    latest_report_ids = {int(report.id) for report in latest_reports.values()}
    if not latest_report_ids:
        return

    relevant_overrides = [
        row
        for row in override_rows
        if int(row.report_id) in latest_report_ids
        and row.metric_type == "adjusted_forecast_quantity_in_mc"
    ]
    for ov in sorted(relevant_overrides, key=lambda row: (_override_specificity(row), int(row.id))):
        target_period = _month_start(ov.period)
        matched = [
            atom
            for atom in forecast_atoms
            if int(atom["owner_user_id"]) == int(ov.owner_user_id)
            and atom["period"] == target_period
            and (ov.branch_name is None or atom["branch_name"] == ov.branch_name)
            and (ov.brand is None or atom["brand"] == ov.brand)
            and (ov.category is None or atom["category"] == ov.category)
            and (ov.sub_category is None or atom["sub_category"] == ov.sub_category)
            and (ov.subline is None or atom["subline"] == ov.subline)
            and (ov.sku_name is None or atom["sku_name"] == ov.sku_name)
        ]
        if not matched:
            continue
        baseline_sum = sum(float(atom["baseline_qty"]) for atom in matched)
        if baseline_sum > 0:
            for atom in matched:
                atom["effective_qty"] = float(ov.value) * (float(atom["baseline_qty"]) / baseline_sum)
        else:
            even_share = float(ov.value) / len(matched)
            for atom in matched:
                atom["effective_qty"] = even_share


def _format_ru_month_year_short(value: date) -> str:
    month_names = {
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
    return f"{month_names.get(value.month, '')} {value.strftime('%y')}".strip()


def _allocate_recommended_proportional(need_by_branch: dict[str, int], hub_stock_qty: float) -> dict[str, int]:
    total_need = sum(max(v, 0) for v in need_by_branch.values())
    if total_need <= 0:
        return {k: 0 for k in need_by_branch}
    pool = min(total_need, max(_qty_int(hub_stock_qty), 0))
    if pool <= 0:
        return {k: 0 for k in need_by_branch}

    alloc_int: dict[str, int] = {}
    fractions: list[tuple[float, str]] = []
    for branch_id, need in need_by_branch.items():
        need_pos = max(int(need), 0)
        raw = (need_pos / total_need) * pool
        base = min(int(math.floor(raw)), need_pos)
        alloc_int[branch_id] = base
        fractions.append((raw - base, branch_id))

    remaining = pool - sum(alloc_int.values())
    for _, branch_id in sorted(fractions, key=lambda x: x[0], reverse=True):
        if remaining <= 0:
            break
        if alloc_int[branch_id] >= max(int(need_by_branch.get(branch_id, 0)), 0):
            continue
        alloc_int[branch_id] += 1
        remaining -= 1
    return alloc_int


async def _resolve_planning_date(db: DBSession, user: CurrentUser) -> date:
    stmt = select(func.max(HistoricalSalesMonthly.date))
    if not is_admin(user):
        stmt = stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    max_hist = (await db.execute(stmt)).scalar_one_or_none()
    if max_hist is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Невозможно определить planning_date без данных historical_sales_monthly",
        )
    return _add_months(_month_start(max_hist), 1)


def _user_cache_scope(user: CurrentUser) -> tuple[str, int]:
    return ("admin" if is_admin(user) else "user", int(user.id))


async def clear_distribution_cache() -> None:
    await _distribution_calc_cache.clear()


async def _build_distribution_calc(
    db: DBSession,
    user: CurrentUser,
) -> tuple[date, list[_BranchSkuCalc], dict[tuple[int, str], str], dict[tuple[int, str], str]]:
    key = (*_user_cache_scope(user), "distribution-calc")
    return await _distribution_calc_cache.get_or_set(
        key,
        lambda: _build_distribution_calc_uncached(db, user),
    )


async def _build_distribution_calc_uncached(
    db: DBSession,
    user: CurrentUser,
) -> tuple[date, list[_BranchSkuCalc], dict[tuple[int, str], str], dict[tuple[int, str], str]]:
    planning_date = await _resolve_planning_date(db, user)

    hs_stmt = select(HistoricalSalesMonthly)
    p_stmt = select(Product)
    b_stmt = select(Branch)
    pr_stmt = select(PriceList)
    fs_stmt = select(ForecastSalesMonthly)
    report_stmt = select(DPReport).where(DPReport.is_deleted.is_(False))
    if not is_admin(user):
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
        b_stmt = b_stmt.where(Branch.owner_user_id == user.id)
        pr_stmt = pr_stmt.where(PriceList.owner_user_id == user.id)
        fs_stmt = fs_stmt.where(ForecastSalesMonthly.owner_user_id == user.id)
        report_stmt = report_stmt.where(DPReport.created_by_id == user.id)

    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    product_rows = (await db.execute(p_stmt)).scalars().all()
    branch_rows = (await db.execute(b_stmt)).scalars().all()
    price_rows = (await db.execute(pr_stmt)).scalars().all()
    forecast_rows = (await db.execute(fs_stmt)).scalars().all()
    report_rows = (await db.execute(report_stmt)).scalars().all()

    product_by_key = {(p.owner_user_id, str(p.sku_code or "").strip()): p for p in product_rows}
    default_stock_norm_days_by_key = {
        (p.owner_user_id, str(p.sku_code or "").strip()): float(p.general_stock_norm_days or 0.0)
        for p in product_rows
    }
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
    for pr in price_rows:
        prices_by_key.setdefault((pr.owner_user_id, str(pr.sku_code or "").strip()), []).append(pr)
    for key in prices_by_key:
        prices_by_key[key].sort(key=lambda x: x.date)

    branch_name_map = {(b.owner_user_id, b.branch_id): b.branch_name for b in branch_rows}
    branch_id_by_name = {(b.owner_user_id, normalize_branch_lookup(b.branch_name)): b.branch_id for b in branch_rows}

    # Branch rows are normal branch stats; hub rows have empty branch_id.
    branch_hist = [r for r in hist_rows if str(r.branch_id or "").strip()]
    hub_hist = [r for r in hist_rows if not str(r.branch_id or "").strip()]

    branch_max_date: dict[tuple[int, str], date] = {}
    for r in branch_hist:
        k = (r.owner_user_id, str(r.branch_id).strip())
        branch_max_date[k] = max(branch_max_date.get(k, r.date), r.date)

    # Snapshot per branch at its own latest date.
    branch_snapshot_rows = [
        r for r in branch_hist if branch_max_date.get((r.owner_user_id, str(r.branch_id).strip())) == r.date
    ]

    # Hub stock snapshot per hub+sku on its own latest date.
    hub_max_date: dict[tuple[int, str, str], date] = {}
    for r in hub_hist:
        key = (r.owner_user_id, str(r.hub_name or "").strip(), str(r.sku_code or "").strip())
        if not key[1] or not key[2]:
            continue
        hub_max_date[key] = max(hub_max_date.get(key, r.date), r.date)
    hub_stock_by_key: dict[tuple[int, str, str], float] = {}
    for r in hub_hist:
        key = (r.owner_user_id, str(r.hub_name or "").strip(), str(r.sku_code or "").strip())
        if not key[1] or not key[2]:
            continue
        if hub_max_date.get(key) != r.date:
            continue
        hub_stock_by_key[key] = hub_stock_by_key.get(key, 0.0) + float(r.past_available_stock or 0.0)

    # Average l3m from historical + average f3m from forecast (kept as existing detail fields).
    l3_months = {_add_months(planning_date, -1), _add_months(planning_date, -2), _add_months(planning_date, -3)}
    f3_months = {_add_months(planning_date, 0), _add_months(planning_date, 1), _add_months(planning_date, 2)}
    l3_by_key: dict[tuple[int, str, str], list[float]] = {}
    for r in branch_hist:
        m = _month_start(r.date)
        if m in l3_months:
            l3_by_key.setdefault((r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()), []).append(
                float(r.fact_quantity_in_mc or 0.0)
            )
    f3_by_key: dict[tuple[int, str, str], list[float]] = {}
    forecast_atoms: list[dict] = []
    for r in forecast_rows:
        m = _month_start(r.date)
        sku_code = str(r.sku_code or "").strip()
        product = product_by_key.get((r.owner_user_id, sku_code))
        baseline_qty = float(r.baseline_forecast_quantity_in_mc or 0.0)
        effective_qty = (
            float(r.adjusted_forecast_quantity_in_mc)
            if r.adjusted_forecast_quantity_in_mc is not None
            else baseline_qty
        )
        branch_id = str(r.branch_id).strip()
        forecast_atoms.append(
            {
                "owner_user_id": int(r.owner_user_id),
                "branch_id": branch_id,
                "branch_name": branch_name_map.get((r.owner_user_id, branch_id), branch_id),
                "period": m,
                "sku_code": sku_code,
                "sku_name": str(product.sku_name) if product is not None else "",
                "brand": str(product.brand) if product is not None else "",
                "category": str(product.category) if product is not None else "",
                "sub_category": str(product.sub_category) if product is not None else "",
                "subline": str(product.sub_line) if product is not None else "",
                "baseline_qty": baseline_qty,
                "effective_qty": effective_qty,
            }
        )

    report_ids = [int(report.id) for report in report_rows]
    override_rows: list[DPReportForecastOverride] = []
    if report_ids:
        override_stmt = select(DPReportForecastOverride).where(
            DPReportForecastOverride.report_id.in_(report_ids),
            DPReportForecastOverride.metric_type == "adjusted_forecast_quantity_in_mc",
        )
        if not is_admin(user):
            override_stmt = override_stmt.where(DPReportForecastOverride.owner_user_id == user.id)
        override_rows = (await db.execute(override_stmt)).scalars().all()
    _apply_report_case_overrides_to_forecasts(
        forecast_atoms=forecast_atoms,
        report_rows=report_rows,
        override_rows=override_rows,
    )

    forecast_qty_by_key: dict[tuple[int, str, str, date], float] = {}
    for atom in forecast_atoms:
        qty = float(atom["effective_qty"])
        forecast_key = (
            int(atom["owner_user_id"]),
            str(atom["branch_id"]),
            str(atom["sku_code"]),
            atom["period"],
        )
        forecast_qty_by_key[forecast_key] = forecast_qty_by_key.get(forecast_key, 0.0) + qty
        if atom["period"] in f3_months:
            f3_by_key.setdefault(
                (int(atom["owner_user_id"]), str(atom["branch_id"]), str(atom["sku_code"])),
                [],
            ).append(qty)

    # Aggregate latest rows by branch+sku.
    branch_sku_rows: dict[tuple[int, str, str], dict[str, float | str | date]] = {}
    for r in branch_snapshot_rows:
        branch_id = str(r.branch_id).strip()
        sku_code = str(r.sku_code or "").strip()
        if not branch_id or not sku_code:
            continue
        key = (r.owner_user_id, branch_id, sku_code)
        existing = branch_sku_rows.get(key)
        if existing is None:
            branch_sku_rows[key] = {
                "date": r.date,
                "target_qty": 0.0,
                "fact_qty": 0.0,
                "available_qty": 0.0,
                "hub_name": str(r.hub_name or "").strip() or "KZ-HUB",
            }
            existing = branch_sku_rows[key]
        existing["target_qty"] = float(existing["target_qty"]) + float(r.target_quantity_in_mc or 0.0)
        existing["fact_qty"] = float(existing["fact_qty"]) + float(r.fact_quantity_in_mc or 0.0)
        existing["available_qty"] = float(existing["available_qty"]) + float(r.past_available_stock or 0.0)
        if not str(existing["hub_name"]).strip():
            existing["hub_name"] = str(r.hub_name or "").strip() or "KZ-HUB"

    # Branch-level stock norm is used only for recommendation need, not for the
    # displayed target/readiness calculations.
    branch_norm_stmt = select(ProductBranch)
    if not is_admin(user):
        branch_norm_stmt = branch_norm_stmt.where(ProductBranch.owner_user_id == user.id)
    branch_norm_rows = (await db.execute(branch_norm_stmt)).scalars().all()
    branch_norm_days_by_key = {
        (r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()): float(r.stock_norm or 0.0)
        for r in branch_norm_rows
    }

    # Unconstrained recommendation need per branch+sku and proportional allocation
    # from hub pool. Displayed target/readiness still use planning-month target only.
    need_by_hub_sku: dict[tuple[int, str, str], dict[str, int]] = {}
    stock_norm_target_by_branch_sku: dict[tuple[int, str, str], float] = {}
    for (owner_id, branch_id, sku_code), vals in branch_sku_rows.items():
        planning_month_target_qty = _forecast_target_for_planning_month(
            forecast_qty_by_key=forecast_qty_by_key,
            owner_user_id=owner_id,
            branch_id=branch_id,
            sku_code=sku_code,
            planning_date=planning_date,
        )
        stock_norm_days = branch_norm_days_by_key.get(
            (owner_id, branch_id, sku_code),
            float(default_stock_norm_days_by_key.get((owner_id, sku_code), 0.0)),
        )
        recommendation_target_qty = _forecast_target_for_stock_norm(
            forecast_qty_by_key=forecast_qty_by_key,
            owner_user_id=owner_id,
            branch_id=branch_id,
            sku_code=sku_code,
            planning_date=planning_date,
            stock_norm_days=stock_norm_days,
        )
        stock_norm_target_by_branch_sku[(owner_id, branch_id, sku_code)] = planning_month_target_qty
        need_qty = max(int(math.ceil(recommendation_target_qty - float(vals["available_qty"]))), 0)
        hub_name = str(vals["hub_name"]).strip() or "KZ-HUB"
        need_by_hub_sku.setdefault((owner_id, hub_name, sku_code), {})[branch_id] = need_qty

    recommended_by_branch_sku: dict[tuple[int, str, str], int] = {}
    for (owner_id, hub_name, sku_code), need_map in need_by_hub_sku.items():
        alloc = _allocate_recommended_proportional(
            need_by_branch=need_map,
            hub_stock_qty=hub_stock_by_key.get((owner_id, hub_name, sku_code), 0.0),
        )
        for branch_id, qty in alloc.items():
            recommended_by_branch_sku[(owner_id, branch_id, sku_code)] = int(qty)

    calc_rows: list[_BranchSkuCalc] = []
    for (owner_id, branch_id, sku_code), vals in branch_sku_rows.items():
        product = product_by_key.get((owner_id, sku_code))
        if product is None:
            continue
        hub_name = str(vals["hub_name"]).strip() or "KZ-HUB"
        row_date = vals["date"]
        sku_prices = prices_by_key.get((owner_id, sku_code), [])
        dsp = _pick_price_for_sales_date(sku_prices, row_date, "dsp")
        invoice_price = _pick_price_for_sales_date(sku_prices, row_date, "invoice price")
        avg_l3_vals = l3_by_key.get((owner_id, branch_id, sku_code), [])
        avg_f3_vals = f3_by_key.get((owner_id, branch_id, sku_code), [])
        calc_rows.append(
            _BranchSkuCalc(
                owner_user_id=owner_id,
                branch_id=branch_id,
                branch_name=branch_name_map.get((owner_id, branch_id), branch_id),
                hub_name=hub_name,
                sku_id=str(product.sku_id),
                sku_code=sku_code,
                sku_name=str(product.sku_name),
                brand=str(product.brand or ""),
                category=str(product.category or ""),
                sub_category=str(product.sub_category or ""),
                subline=str(product.sub_line or ""),
                date=row_date,
                target_qty=float(vals["target_qty"]),
                stock_norm_target_qty=float(stock_norm_target_by_branch_sku.get((owner_id, branch_id, sku_code), 0.0)),
                fact_qty=float(vals["fact_qty"]),
                available_qty=float(vals["available_qty"]),
                total_hub_available_qty=float(hub_stock_by_key.get((owner_id, hub_name, sku_code), 0.0)),
                recommended_qty=int(recommended_by_branch_sku.get((owner_id, branch_id, sku_code), 0)),
                pieces_in_master_carton=float(product.pieces_in_master_carton or 0.0),
                master_carton_volume_cbm=float(product.master_carton_volume_cbm or 0.0),
                master_carton_gross_weight_kg=float(product.master_carton_gross_weight_kg or 0.0),
                dsp=float(dsp),
                invoice_price=float(invoice_price),
                avg_l3m=_qty_int((sum(avg_l3_vals) / len(avg_l3_vals)) if avg_l3_vals else 0.0),
                avg_f3m=_qty_int((sum(avg_f3_vals) / len(avg_f3_vals)) if avg_f3_vals else 0.0),
            )
        )

    return planning_date, calc_rows, branch_id_by_name, branch_name_map


@router.get("", response_model=DistributionAggregateResponse, include_in_schema=False)
@router.get("/", response_model=DistributionAggregateResponse)
async def get_distribution_aggregated(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: list[str] | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    readiness_for_target_per_branch: list[int] | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> DistributionAggregateResponse:
    normalized_view_type = _normalize_distribution_view_type(view_type)
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    adj_stmt = select(DistributionBranchAmountAdjustment).where(
        DistributionBranchAmountAdjustment.planning_date == planning_date
    )
    if not is_admin(user):
        adj_stmt = adj_stmt.where(DistributionBranchAmountAdjustment.owner_user_id == user.id)
    adj_rows = (await db.execute(adj_stmt)).scalars().all()
    branch_adj_map = {
        (r.owner_user_id, str(r.branch_id).strip()): float(r.adjusted_amount_kzt_per_branch or 0.0)
        for r in adj_rows
    }
    detail_adj_stmt = select(DistributionSkuAdjustment).where(DistributionSkuAdjustment.planning_date == planning_date)
    if not is_admin(user):
        detail_adj_stmt = detail_adj_stmt.where(DistributionSkuAdjustment.owner_user_id == user.id)
    detail_adj_rows = (await db.execute(detail_adj_stmt)).scalars().all()
    detail_adj_map = {
        (r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()): _qty_int(r.adjusted_quantity_in_mc)
        for r in detail_adj_rows
    }
    product_filter = _product_filter_payload(
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
    )
    filtered_calc_rows = [
        row for row in calc_rows if _matches_product_filters(row, product_filter)
    ]
    rows, _rows_by_branch, _row_by_key = _build_aggregate_rows(
        calc_rows=filtered_calc_rows,
        branch_adj_map=branch_adj_map,
        detail_adj_map=detail_adj_map,
        view_type=normalized_view_type,
    )
    filtered_rows = rows
    if branch_name:
        filtered_rows = [
            r
            for r in filtered_rows
            if _branch_matches_selected(r.branch_name, branch_name)
        ]
    if readiness_for_target_per_branch:
        readiness_values = {int(v) for v in readiness_for_target_per_branch}
        filtered_rows = [
            r
            for r in filtered_rows
            if int(r.readiness_for_target_per_branch) in readiness_values
        ]

    filter_options = _aggregate_filter_options(
        calc_rows=calc_rows,
        product_filter=product_filter,
        branch_name=branch_name,
        readiness_for_target_per_branch=readiness_for_target_per_branch,
        branch_adj_map=branch_adj_map,
        detail_adj_map=detail_adj_map,
    )
    filtered_rows.sort(
        key=lambda x: (-x.target_amount_dsp_per_branch, x.hub_name, x.branch_name)
    )
    paged, total_items, total_pages = _paginate(filtered_rows, page=page, page_size=page_size)
    return DistributionAggregateResponse(
        planning_date=planning_date.isoformat(),
        items=paged,
        total_items=total_items,
        total_pages=total_pages,
        filter_options=filter_options,
    )


@router.get("/summary/", response_model=DistributionSummaryResponse, include_in_schema=False)
@router.get("/summary", response_model=DistributionSummaryResponse)
async def get_distribution_summary(
    db: DBSession,
    user: CurrentUser,
) -> DistributionSummaryResponse:
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    total_target = sum(_target_amount_dsp(r) for r in calc_rows)
    total_fact = sum(_potential_amount_dsp(r) for r in calc_rows)
    return DistributionSummaryResponse(
        planning_date=planning_date.isoformat(),
        total_target_amount_dsp=round(total_target, 2),
        total_fact_amount_dsp=round(total_fact, 2),
        total_readiness_for_target=_safe_readiness(total_fact, total_target),
    )


@router.get(
    "/summary/information-icon/",
    response_model=DistributionSummaryInformationIconResponse,
    include_in_schema=False,
)
@router.get(
    "/summary/information-icon",
    response_model=DistributionSummaryInformationIconResponse,
)
async def get_distribution_summary_information_icon(
    db: DBSession,
    user: CurrentUser,
) -> DistributionSummaryInformationIconResponse:
    planning_date, _, _, _ = await _build_distribution_calc(db, user)
    period_label = _format_ru_month_year_short(planning_date)
    return DistributionSummaryInformationIconResponse(
        total_target_amount_dsp_text=(
            f"Плановая сумма распределения за {period_label}: сколько товара нужно распределить "
            "по филиалам для покрытия потребности."
        ),
        total_fact_amount_dsp_text=(
            f"Фактическая сумма распределения за {period_label}: сколько товара уже доступно "
            "или запланировано к распределению по филиалам."
        ),
    )


@router.get("/details/", response_model=DistributionDetailsResponse, include_in_schema=False)
@router.get("/details", response_model=DistributionDetailsResponse)
async def get_distribution_details(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
    sku_code_filter: list[str] | None = Query(default=None, alias="sku_code"),
    sku_name_filter: list[str] | None = Query(default=None, alias="sku_name"),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    readiness_for_target_per_sku_filter: list[int] | None = Query(
        default=None, alias="readiness_for_target_per_sku"
    ),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> DistributionDetailsResponse:
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    selected = [r for r in calc_rows if _branch_name_matches(branch_name, r.branch_name, r.branch_id)]
    if not selected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Филиал не найден")

    adj_stmt = select(DistributionSkuAdjustment).where(DistributionSkuAdjustment.planning_date == planning_date)
    if not is_admin(user):
        adj_stmt = adj_stmt.where(DistributionSkuAdjustment.owner_user_id == user.id)
    adj_rows = (await db.execute(adj_stmt)).scalars().all()
    detail_adj_map = {
        (r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()): _qty_int(r.adjusted_quantity_in_mc)
        for r in adj_rows
    }

    product_filter = _product_filter_payload(
        sku_code=sku_code_filter,
        sku_name=sku_name_filter,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
    )
    filtered_calc_rows = [
        row for row in selected if _matches_product_filters(row, product_filter)
    ]
    filtered_rows = [_build_detail_row(row, detail_adj_map) for row in filtered_calc_rows]
    if readiness_for_target_per_sku_filter:
        readiness_values = {int(v) for v in readiness_for_target_per_sku_filter}
        filtered_rows = [r for r in filtered_rows if int(r.readiness_for_target_per_sku) in readiness_values]

    filter_options = _details_filter_options(
        selected_rows=selected,
        product_filter=product_filter,
        readiness_for_target_per_sku_filter=readiness_for_target_per_sku_filter,
        detail_adj_map=detail_adj_map,
    )

    filtered_rows.sort(key=lambda x: (-x.recommended_quantity_in_mc, x.sku_code))
    paged, total_items, total_pages = _paginate(filtered_rows, page=page, page_size=page_size)
    first = selected[0]
    return DistributionDetailsResponse(
        planning_date=planning_date.isoformat(),
        hub_name=first.hub_name,
        branch_name=first.branch_name,
        items=paged,
        total_items=total_items,
        total_pages=total_pages,
        filter_options=filter_options,
    )


@router.get("/details/summary/", response_model=DistributionDetailsSummaryResponse, include_in_schema=False)
@router.get("/details/summary", response_model=DistributionDetailsSummaryResponse)
async def get_distribution_details_summary(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
) -> DistributionDetailsSummaryResponse:
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    adj_stmt = select(DistributionSkuAdjustment).where(DistributionSkuAdjustment.planning_date == planning_date)
    if not is_admin(user):
        adj_stmt = adj_stmt.where(DistributionSkuAdjustment.owner_user_id == user.id)
    adj_rows = (await db.execute(adj_stmt)).scalars().all()
    detail_adj_map = {
        (r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()): _qty_int(r.adjusted_quantity_in_mc)
        for r in adj_rows
    }

    selected_rows = [r for r in calc_rows if _branch_name_matches(branch_name, r.branch_name, r.branch_id)]
    if not selected_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Филиал не найден")

    total_volume, total_gross_weight, total_adjusted_amount = _adjusted_metrics_for_rows(
        selected_rows,
        detail_adj_map,
    )

    first = selected_rows[0]
    return DistributionDetailsSummaryResponse(
        planning_date=planning_date.isoformat(),
        hub_name=first.hub_name,
        branch_name=first.branch_name,
        total_adjusted_volume_cbm_per_branch=round(total_volume, 2),
        total_adjusted_gross_weight_kg_per_branch=round(total_gross_weight, 2),
        total_adjusted_amount_dsp_per_branch=round(total_adjusted_amount, 2),
    )


@router.get(
    "/details/summary/information-icon/",
    response_model=DistributionDetailsSummaryInformationIconResponse,
    include_in_schema=False,
)
@router.get(
    "/details/summary/information-icon",
    response_model=DistributionDetailsSummaryInformationIconResponse,
)
async def get_distribution_details_summary_information_icon(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
) -> DistributionDetailsSummaryInformationIconResponse:
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    selected_rows = [r for r in calc_rows if _branch_name_matches(branch_name, r.branch_name, r.branch_id)]
    if not selected_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Филиал не найден")
    period_label = _format_ru_month_year_short(planning_date)
    return DistributionDetailsSummaryInformationIconResponse(
        total_adjusted_volume_cbm_per_branch_text=(
            f'Объем распределения за {period_label}: сколько места займет товар, выбранный '
            f'для отправки в этот филиал. Значение обновляется при изменении колонки '
            f'"Распределить в кол-ве".'
        ),
        total_adjusted_gross_weight_kg_per_branch_text=(
            f'Общий вес за {period_label}: сколько будет весить товар, выбранный для '
            f'отправки в этот филиал. Значение обновляется при изменении колонки '
            f'"Распределить в кол-ве".'
        ),
    )


@router.get(
    "/details/readiness-for-target-per-sku-information-icon/",
    response_model=DistributionDetailsReadinessInformationIconResponse,
    include_in_schema=False,
)
@router.get(
    "/details/readiness-for-target-per-sku-information-icon",
    response_model=DistributionDetailsReadinessInformationIconResponse,
)
async def get_distribution_details_readiness_information_icon(
    user: CurrentUser,
) -> DistributionDetailsReadinessInformationIconResponse:
    _ = user
    return DistributionDetailsReadinessInformationIconResponse(
        readiness_for_target_per_sku_text=(
            "Готовность показывает, насколько текущий запас и выбранное к распределению количество "
            "покрывают потребность филиала по этому товару. Значение ограничено от 0% до 100%. "
            "До ручного изменения распределения показатель считается так, как будто к отправке выбрано 0; "
            'после изменения колонки "Распределить в кол-ве" он пересчитывается.'
        )
    )


@router.patch("", include_in_schema=False)
@router.patch("/")
async def patch_distribution_branch_adjustments(
    db: DBSession,
    user: CurrentUser,
    payload: DistributionBranchAdjustRequest,
) -> dict:
    if not payload.updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Список updates не может быть пустым")
    planning_date, _, branch_id_by_name, _ = await _build_distribution_calc(db, user)
    owner_user_id = user.id if not is_admin(user) else None

    updated = 0
    for row in payload.updates:
        matched = False
        row_branch_norm = normalize_branch_lookup(row.branch_name)
        for (owner_id, bname_norm), branch_id in branch_id_by_name.items():
            if bname_norm != row_branch_norm:
                continue
            if owner_user_id is not None and owner_id != owner_user_id:
                continue
            matched = True
            await db.execute(
                delete(DistributionBranchAmountAdjustment).where(
                    DistributionBranchAmountAdjustment.owner_user_id == owner_id,
                    DistributionBranchAmountAdjustment.planning_date == planning_date,
                    DistributionBranchAmountAdjustment.branch_id == branch_id,
                )
            )
            db.add(
                DistributionBranchAmountAdjustment(
                    owner_user_id=owner_id,
                    planning_date=planning_date,
                    branch_id=branch_id,
                    adjusted_amount_kzt_per_branch=float(row.adjusted_amount_kzt_per_branch),
                )
            )
            updated += 1
        if not matched and row.branch_name and owner_user_id is not None:
            db.add(
                DistributionBranchAmountAdjustment(
                    owner_user_id=owner_user_id,
                    planning_date=planning_date,
                    branch_id=row.branch_name,
                    adjusted_amount_kzt_per_branch=float(row.adjusted_amount_kzt_per_branch),
                )
            )
            updated += 1
    await db.commit()
    await clear_distribution_cache()
    return {"rows_updated": updated}


@router.patch("/details/", include_in_schema=False)
@router.patch("/details")
async def patch_distribution_detail_adjustments(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
    payload: DistributionSkuAdjustRequest = ...,
) -> dict:
    if not payload.updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Список updates не может быть пустым")
    wrong_field_rows = [
        {
            "sku_code": item.sku_code,
            "provided_field": "adjusted_quantity_in_mc_per_branch",
            "expected_field": "adjusted_quantity_in_mc",
        }
        for item in payload.updates
        if item.adjusted_quantity_in_mc is None and item.adjusted_quantity_in_mc_per_branch is not None
    ]
    if wrong_field_rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Неверный payload для /distribution/details. Используйте adjusted_quantity_in_mc, а не adjusted_quantity_in_mc_per_branch.",
                "invalid_updates": wrong_field_rows,
            },
        )
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    branch_rows = [r for r in calc_rows if _branch_name_matches(branch_name, r.branch_name, r.branch_id)]
    if not branch_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Филиал не найден")

    by_owner_code = {(r.owner_user_id, r.sku_code): r for r in branch_rows}
    updated = 0
    for item in payload.updates:
        for (owner_id, sku_code), row in by_owner_code.items():
            if sku_code != item.sku_code:
                continue
            await db.execute(
                delete(DistributionSkuAdjustment).where(
                    DistributionSkuAdjustment.owner_user_id == owner_id,
                    DistributionSkuAdjustment.planning_date == planning_date,
                    DistributionSkuAdjustment.branch_id == row.branch_id,
                    DistributionSkuAdjustment.sku_code == row.sku_code,
                )
            )
            if item.adjusted_quantity_in_mc is not None:
                db.add(
                    DistributionSkuAdjustment(
                        owner_user_id=owner_id,
                        planning_date=planning_date,
                        branch_id=row.branch_id,
                        sku_id=row.sku_id,
                        sku_code=row.sku_code,
                        adjusted_quantity_in_mc=int(item.adjusted_quantity_in_mc),
                    )
                )
            updated += 1
    await db.commit()
    await clear_distribution_cache()
    return {"rows_updated": updated}


@router.get("/download/", include_in_schema=False)
@router.get("/download")
async def download_distribution(
    db: DBSession,
    user: CurrentUser,
    view_type: str = Query("DSP", description="DSP, Invoice price or Cases"),
    branch_name: str | None = Query(None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    readiness_for_target_per_branch: list[int] | None = Query(default=None),
):
    response = await get_distribution_aggregated(
        db=db,
        user=user,
        view_type=view_type,
        branch_name=[branch_name] if branch_name else None,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        readiness_for_target_per_branch=readiness_for_target_per_branch,
        page=1,
        page_size="all",
    )
    rows = response.items
    export_rows = [r.model_dump() for r in rows]
    output = BytesIO()
    pd.DataFrame(export_rows).rename(columns=DISTRIBUTION_DOWNLOAD_HEADERS).to_excel(
        output, index=False, sheet_name="distribution"
    )
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="distribution.xlsx"'},
    )


@router.get("/details/download/", include_in_schema=False)
@router.get("/details/download")
async def download_distribution_details(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
    sku_code: str | None = Query(None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    readiness_for_target_per_sku: list[int] | None = Query(default=None),
):
    response = await get_distribution_details(
        db=db,
        user=user,
        branch_name=branch_name,
        sku_code_filter=[sku_code] if sku_code else None,
        sku_name_filter=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=subline,
        readiness_for_target_per_sku_filter=readiness_for_target_per_sku,
        page=1,
        page_size="all",
    )
    rows = response.items
    export_rows = [r.model_dump() for r in rows]
    output = BytesIO()
    pd.DataFrame(export_rows).rename(columns=DISTRIBUTION_DETAILS_DOWNLOAD_HEADERS).to_excel(
        output, index=False, sheet_name="distribution_details"
    )
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="distribution_details.xlsx"'},
    )

