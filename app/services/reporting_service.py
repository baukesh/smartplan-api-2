from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_uploads import Branch, HistoricalSalesMonthly, Product
from app.models.derived import ForecastSalesMonthly
from app.models.reporting import DPReport, DPReportForecastOverride


VALID_VIEW_TYPES = {"dsp", "cases", "gross weight"}
VALID_OVERRIDE_METRICS = {
    "adjusted_forecast_quantity_in_mc": "baseline_forecast_quantity_in_mc",
    "adjusted_forecast_gross_weight_kg": "baseline_forecast_gross_weight_kg",
    "adjusted_forecast_volume_cbm": "baseline_forecast_volume_cbm",
    "adjusted_forecast_amount_kzt": "baseline_forecast_amount_kzt",
}


@dataclass
class ReportingContext:
    planning_month: date
    date_from: date
    date_to: date
    product_filter: dict
    branch_filter: list[str]
    view_type: str


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def parse_product_filter(value: object | None) -> dict:
    if value is None:
        return {
            "sku_codes": [],
            "brands": [],
            "categories": [],
            "sub_categories": [],
            "sublines": [],
        }
    if isinstance(value, dict):
        source = value
    elif isinstance(value, str) and value.strip():
        try:
            source = json.loads(value)
        except Exception:
            source = {}
    else:
        source = {}
    return {
        "sku_codes": list(source.get("sku_codes", []) or []),
        "brands": list(source.get("brands", []) or []),
        "categories": list(source.get("categories", []) or []),
        "sub_categories": list(source.get("sub_categories", []) or []),
        "sublines": list(source.get("sublines", []) or []),
    }


def parse_branch_filter(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def to_json_string(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


async def get_current_planning_month(db: AsyncSession, owner_user_id: int) -> date:
    row = (
        await db.execute(
            select(func.max(HistoricalSalesMonthly.date)).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot derive planning month without historical sales data",
        )
    return _add_months(_month_start(row), 1)


def default_period_for_planning(planning_month: date) -> tuple[date, date]:
    return _add_months(planning_month, -6), _add_months(planning_month, 5)


def validate_period_window(planning_month: date, date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )
    if date_from > planning_month:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than planning_month",
        )
    if date_to < planning_month:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_to cannot be less than planning_month",
        )
    if date_from < _add_months(planning_month, -12):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from is outside max historical window (12 months)",
        )
    if date_to > _add_months(planning_month, 11):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_to is outside max forecast window (12 months)",
        )


def validate_view_type(view_type: str) -> str:
    normalized = view_type.strip().lower()
    if normalized not in VALID_VIEW_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="view_type must be one of: DSP, Cases, Gross weight",
        )
    return normalized


def _matches_filters(
    product: Product,
    branch_name: str,
    product_filter: dict,
    branch_filter: list[str],
) -> bool:
    if branch_filter and branch_name not in branch_filter:
        return False
    sku_codes = {v for v in product_filter.get("sku_codes", []) if v}
    brands = {v for v in product_filter.get("brands", []) if v}
    categories = {v for v in product_filter.get("categories", []) if v}
    sub_categories = {v for v in product_filter.get("sub_categories", []) if v}
    sublines = {v for v in product_filter.get("sublines", []) if v}

    if sku_codes and product.sku_code not in sku_codes:
        return False
    if brands and product.brand not in brands:
        return False
    if categories and product.category not in categories:
        return False
    if sub_categories and product.sub_category not in sub_categories:
        return False
    if sublines and product.sub_line not in sublines:
        return False
    return True


async def _load_owner_maps(db: AsyncSession, owner_user_id: int) -> tuple[dict[str, Product], dict[str, str]]:
    products = (
        await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
    ).scalars().all()
    branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    product_map = {p.sku_id: p for p in products}
    branch_name_by_id = {b.branch_id: b.branch_name for b in branches}
    return product_map, branch_name_by_id


def _key_tuple(row: dict) -> tuple:
    return (
        row["period"],
        row["branch_name"],
        row["brand"],
        row["category"],
        row["sub_category"],
        row["subline"],
        row["sku_name"],
    )


def _aggregate_period_totals(rows: list[dict], metric_fields: list[str]) -> list[dict]:
    buckets: dict[str, dict[str, float | str]] = {}
    for row in rows:
        period = str(row["period"])
        if period not in buckets:
            buckets[period] = {"period": period}
            for m in metric_fields:
                buckets[period][m] = 0.0
        b = buckets[period]
        for m in metric_fields:
            b[m] = float(b[m]) + float(row.get(m, 0.0) or 0.0)

    out: list[dict] = []
    for period, payload in buckets.items():
        out_row: dict[str, float | str] = {"period": period}
        for m in metric_fields:
            out_row[m] = round(float(payload[m]), 2)
        out.append(out_row)
    out.sort(key=lambda x: x["period"])
    return out


async def build_report_tables(
    db: AsyncSession,
    owner_user_id: int,
    ctx: ReportingContext,
    report_id: int | None = None,
) -> tuple[list[dict], list[dict]]:
    product_map, branch_name_by_id = await _load_owner_maps(db, owner_user_id)

    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id,
                HistoricalSalesMonthly.date >= ctx.date_from,
                HistoricalSalesMonthly.date <= ctx.date_to,
            )
        )
    ).scalars().all()

    hist_buckets: dict[tuple, dict] = {}
    for r in hist_rows:
        if r.date >= ctx.planning_month:
            continue
        product = product_map.get(r.sku_id)
        if not product:
            continue
        branch_name = branch_name_by_id.get(r.branch_id, r.branch_id)
        if not _matches_filters(product, branch_name, ctx.product_filter, ctx.branch_filter):
            continue
        payload = {
            "period": _month_start(r.date).isoformat(),
            "branch_name": branch_name,
            "brand": product.brand,
            "category": product.category,
            "sub_category": product.sub_category,
            "subline": product.sub_line,
            "sku_name": product.sku_name,
        }
        key = _key_tuple(payload)
        if key not in hist_buckets:
            hist_buckets[key] = {
                **payload,
                "fact_quantity_in_mc": 0.0,
                "fact_gross_weight_kg": 0.0,
                "fact_volume_cbm": 0.0,
                "fact_amount_kzt": 0.0,
                "target_quantity_in_mc": 0.0,
                "target_gross_weight_kg": 0.0,
                "target_volume_cbm": 0.0,
                "target_amount_kzt": 0.0,
                "past_available_stock": 0.0,
            }
        bucket = hist_buckets[key]
        bucket["fact_quantity_in_mc"] += float(r.fact_quantity_in_mc or 0.0)
        bucket["fact_gross_weight_kg"] += float(r.fact_gross_weight_kg or 0.0)
        bucket["fact_volume_cbm"] += float(r.fact_volume_cbm or 0.0)
        bucket["fact_amount_kzt"] += float(r.fact_amount_kzt or 0.0)
        bucket["target_quantity_in_mc"] += float(r.target_quantity_in_mc or 0.0)
        bucket["target_gross_weight_kg"] += float(r.target_gross_weight_kg or 0.0)
        bucket["target_volume_cbm"] += float(r.target_volume_cbm or 0.0)
        bucket["target_amount_kzt"] += float(r.target_amount_kzt or 0.0)
        bucket["past_available_stock"] += float(r.past_available_stock or 0.0)

    historical_table = _aggregate_period_totals(
        list(hist_buckets.values()),
        [
            "fact_quantity_in_mc",
            "fact_gross_weight_kg",
            "fact_volume_cbm",
            "fact_amount_kzt",
            "target_quantity_in_mc",
            "target_gross_weight_kg",
            "target_volume_cbm",
            "target_amount_kzt",
            "past_available_stock",
        ],
    )

    fc_rows = (
        await db.execute(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.owner_user_id == owner_user_id,
                ForecastSalesMonthly.date >= ctx.date_from,
                ForecastSalesMonthly.date <= ctx.date_to,
            )
        )
    ).scalars().all()

    atomic_rows: list[dict] = []
    for r in fc_rows:
        if r.date < ctx.planning_month:
            continue
        product = product_map.get(r.sku_id)
        if not product:
            continue
        branch_name = branch_name_by_id.get(r.branch_id, r.branch_id)
        if not _matches_filters(product, branch_name, ctx.product_filter, ctx.branch_filter):
            continue
        atomic_rows.append(
            {
                "period": _month_start(r.date).isoformat(),
                "branch_name": branch_name,
                "brand": product.brand,
                "category": product.category,
                "sub_category": product.sub_category,
                "subline": product.sub_line,
                "sku_name": product.sku_name,
                "baseline_forecast_quantity_in_mc": float(r.baseline_forecast_quantity_in_mc or 0.0),
                "baseline_forecast_gross_weight_kg": float(r.baseline_forecast_gross_weight_kg or 0.0),
                "baseline_forecast_volume_cbm": float(r.baseline_forecast_volume_cbm or 0.0),
                "baseline_forecast_amount_kzt": float(r.baseline_forecast_amount_kzt or 0.0),
                "adjusted_forecast_quantity_in_mc": float(r.adjusted_forecast_quantity_in_mc) if r.adjusted_forecast_quantity_in_mc is not None else float(r.baseline_forecast_quantity_in_mc or 0.0),
                "adjusted_forecast_gross_weight_kg": float(r.adjusted_forecast_gross_weight_kg) if r.adjusted_forecast_gross_weight_kg is not None else float(r.baseline_forecast_gross_weight_kg or 0.0),
                "adjusted_forecast_volume_cbm": float(r.adjusted_forecast_volume_cbm) if r.adjusted_forecast_volume_cbm is not None else float(r.baseline_forecast_volume_cbm or 0.0),
                "adjusted_forecast_amount_kzt": float(r.adjusted_forecast_amount_kzt) if r.adjusted_forecast_amount_kzt is not None else float(r.baseline_forecast_amount_kzt or 0.0),
                "future_available_stock": float(r.future_available_stock or 0.0),
            }
        )

    if report_id is not None:
        override_rows = (
            await db.execute(
                select(DPReportForecastOverride).where(
                    DPReportForecastOverride.report_id == report_id,
                    DPReportForecastOverride.owner_user_id == owner_user_id,
                )
            )
        ).scalars().all()
        sorted_overrides = sorted(
            override_rows,
            key=lambda o: sum(
                1
                for v in [o.branch_name, o.brand, o.category, o.sub_category, o.subline, o.sku_name]
                if v
            ),
        )
        for ov in sorted_overrides:
            if ov.metric_type not in VALID_OVERRIDE_METRICS:
                continue
            adjusted_metric = ov.metric_type
            baseline_metric = VALID_OVERRIDE_METRICS[ov.metric_type]
            target_period = _month_start(ov.period).isoformat()
            matched = [
                r
                for r in atomic_rows
                if r["period"] == target_period
                and (ov.branch_name is None or r["branch_name"] == ov.branch_name)
                and (ov.brand is None or r["brand"] == ov.brand)
                and (ov.category is None or r["category"] == ov.category)
                and (ov.sub_category is None or r["sub_category"] == ov.sub_category)
                and (ov.subline is None or r["subline"] == ov.subline)
                and (ov.sku_name is None or r["sku_name"] == ov.sku_name)
            ]
            if not matched:
                continue
            baseline_sum = sum(float(r[baseline_metric]) for r in matched)
            if baseline_sum > 0:
                for r in matched:
                    share = float(r[baseline_metric]) / baseline_sum
                    r[adjusted_metric] = float(ov.value) * share
            else:
                even_share = float(ov.value) / len(matched)
                for r in matched:
                    r[adjusted_metric] = even_share

    forecast_buckets: dict[tuple, dict] = {}
    for r in atomic_rows:
        key = _key_tuple(r)
        if key not in forecast_buckets:
            forecast_buckets[key] = {
                "period": r["period"],
                "branch_name": r["branch_name"],
                "brand": r["brand"],
                "category": r["category"],
                "sub_category": r["sub_category"],
                "subline": r["subline"],
                "sku_name": r["sku_name"],
                "baseline_forecast_quantity_in_mc": 0.0,
                "baseline_forecast_gross_weight_kg": 0.0,
                "baseline_forecast_volume_cbm": 0.0,
                "baseline_forecast_amount_kzt": 0.0,
                "adjusted_forecast_quantity_in_mc": 0.0,
                "adjusted_forecast_gross_weight_kg": 0.0,
                "adjusted_forecast_volume_cbm": 0.0,
                "adjusted_forecast_amount_kzt": 0.0,
                "future_available_stock": 0.0,
            }
        b = forecast_buckets[key]
        for metric in [
            "baseline_forecast_quantity_in_mc",
            "baseline_forecast_gross_weight_kg",
            "baseline_forecast_volume_cbm",
            "baseline_forecast_amount_kzt",
            "adjusted_forecast_quantity_in_mc",
            "adjusted_forecast_gross_weight_kg",
            "adjusted_forecast_volume_cbm",
            "adjusted_forecast_amount_kzt",
            "future_available_stock",
        ]:
            b[metric] += float(r[metric] or 0.0)

    forecast_table = _aggregate_period_totals(
        list(forecast_buckets.values()),
        [
            "baseline_forecast_quantity_in_mc",
            "baseline_forecast_gross_weight_kg",
            "baseline_forecast_volume_cbm",
            "baseline_forecast_amount_kzt",
            "adjusted_forecast_quantity_in_mc",
            "adjusted_forecast_gross_weight_kg",
            "adjusted_forecast_volume_cbm",
            "adjusted_forecast_amount_kzt",
            "future_available_stock",
        ],
    )
    return historical_table, forecast_table


async def build_reporting_context(
    db: AsyncSession,
    owner_user_id: int,
    view_type: str,
    product_filter: object | None,
    branch_filter: object | None,
    planning_month: date | None,
    date_from: date | None,
    date_to: date | None,
) -> ReportingContext:
    normalized_view = validate_view_type(view_type)
    resolved_planning = planning_month or await get_current_planning_month(db, owner_user_id)
    default_from, default_to = default_period_for_planning(resolved_planning)
    effective_from = _month_start(date_from or default_from)
    effective_to = _month_start(date_to or default_to)
    validate_period_window(resolved_planning, effective_from, effective_to)
    return ReportingContext(
        planning_month=resolved_planning,
        date_from=effective_from,
        date_to=effective_to,
        product_filter=parse_product_filter(product_filter),
        branch_filter=parse_branch_filter(branch_filter),
        view_type=normalized_view,
    )


async def replace_report_overrides(
    db: AsyncSession,
    report_id: int,
    owner_user_id: int,
    overrides: list[dict],
) -> None:
    await db.execute(
        delete(DPReportForecastOverride).where(
            DPReportForecastOverride.report_id == report_id,
            DPReportForecastOverride.owner_user_id == owner_user_id,
        )
    )
    inserts: list[DPReportForecastOverride] = []
    for ov in overrides:
        metric = str(ov.get("metric_type", "")).strip()
        if metric not in VALID_OVERRIDE_METRICS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported metric_type: {metric}",
            )
        period_value = ov.get("period")
        if not isinstance(period_value, date):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Override period must be a valid date",
            )
        inserts.append(
            DPReportForecastOverride(
                report_id=report_id,
                owner_user_id=owner_user_id,
                period=_month_start(period_value),
                metric_type=metric,
                branch_name=ov.get("branch_name"),
                brand=ov.get("brand"),
                category=ov.get("category"),
                sub_category=ov.get("sub_category"),
                subline=ov.get("subline"),
                sku_name=ov.get("sku_name"),
                adjustment_reason=ov.get("adjustment_reason"),
                value=float(ov.get("value", 0.0)),
            )
        )
    if inserts:
        db.add_all(inserts)


def report_card_payload(report: DPReport) -> dict:
    planning_month = _month_start(report.planning_month or report.date_to or report.date_from or date.today())
    default_from, default_to = default_period_for_planning(planning_month)
    return {
        "report_id": report.id,
        "report_name": report.name,
        "product_filter": parse_product_filter(report.product_filter_json or report.product_filter),
        "branch_filter": parse_branch_filter(report.branch_filter_json or report.branch_filter),
        "view_type": report.view_type,
        "date_from": report.date_from or default_from,
        "date_to": report.date_to or default_to,
        "is_draft": report.is_draft,
        "planning_month": planning_month,
    }
