from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branch_localization import localize_branch_name, normalize_branch_lookup
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product
from app.models.derived import ForecastSalesMonthly
from app.models.reporting import DPReport, DPReportForecastOverride


VALID_VIEW_TYPES = {"dsp", "invoice price", "cases", "gross weight", "net weight"}
VALID_OVERRIDE_METRICS = {
    "adjusted_forecast_quantity_in_mc": "baseline_forecast_quantity_in_mc",
    "adjusted_forecast_gross_weight_kg": "baseline_forecast_gross_weight_kg",
    "adjusted_forecast_net_weight_kg": "baseline_forecast_net_weight_kg",
    "adjusted_forecast_volume_cbm": "baseline_forecast_volume_cbm",
    "adjusted_forecast_amount_kzt": "baseline_forecast_amount_kzt",
    "adjusted_forecast_invoice_amount_kzt": "baseline_forecast_invoice_amount_kzt",
}
VIEW_TYPE_TO_OVERRIDE_METRIC = {
    "cases": "adjusted_forecast_quantity_in_mc",
    "gross weight": "adjusted_forecast_gross_weight_kg",
    "net weight": "adjusted_forecast_net_weight_kg",
    "dsp": "adjusted_forecast_amount_kzt",
    "invoice price": "adjusted_forecast_invoice_amount_kzt",
}


@dataclass
class ReportingContext:
    planning_month: date
    date_from: date
    date_to: date
    product_filter: dict
    branch_filter: list[str]
    hub_filter: list[str]
    view_type: str


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _qty_int(value: float | None) -> int:
    return int(round(float(value or 0.0)))


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def parse_product_filter(value: object | None) -> dict:
    if value is None:
        return {
            "sku_codes": [],
            "sku_names": [],
            "brands": [],
            "categories": [],
            "sub_categories": [],
            "sublines": [],
            "sku_statuses": [],
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
        "sku_names": list(source.get("sku_names", []) or []),
        "brands": list(source.get("brands", []) or []),
        "categories": list(source.get("categories", []) or []),
        "sub_categories": list(source.get("sub_categories", []) or []),
        "sublines": list(source.get("sublines", []) or []),
        "sku_statuses": list(source.get("sku_statuses", []) or []),
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


def parse_hub_filter(value: object | None) -> list[str]:
    return parse_branch_filter(value)


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
            detail="Невозможно определить planning_month без данных historical_sales_monthly",
        )
    return _add_months(_month_start(row), 1)


def default_period_for_planning(planning_month: date) -> tuple[date, date]:
    return _add_months(planning_month, -6), _add_months(planning_month, 5)


def validate_period_window(planning_month: date, date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр date_from не может быть больше date_to",
        )


def validate_view_type(view_type: str) -> str:
    normalized = view_type.strip().lower()
    if normalized not in VALID_VIEW_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Параметр view_type должен быть одним из: DSP, Invoice price, Cases, Gross weight, Net weight",
        )
    return normalized


def normalize_override_metric(metric_type: str) -> str | None:
    normalized = str(metric_type or "").strip().lower()
    if normalized in VIEW_TYPE_TO_OVERRIDE_METRIC:
        return VIEW_TYPE_TO_OVERRIDE_METRIC[normalized]
    # Backward-compatible support for already-internal metric ids.
    if normalized in VALID_OVERRIDE_METRICS:
        return normalized
    return None


def _matches_filters(
    product: Product,
    branch_name: str,
    product_filter: dict,
    branch_filter: list[str],
    hub_name: str | None = None,
    hub_filter: list[str] | None = None,
) -> bool:
    if branch_filter:
        normalized_filter = {normalize_branch_lookup(x) for x in branch_filter if str(x).strip()}
        if normalize_branch_lookup(branch_name) not in normalized_filter:
            return False
    if hub_filter:
        hub_values = {str(x).strip() for x in hub_filter if str(x).strip()}
        if str(hub_name or "").strip() not in hub_values:
            return False
    sku_codes = {v for v in product_filter.get("sku_codes", []) if v}
    sku_names = {v for v in product_filter.get("sku_names", []) if v}
    brands = {v for v in product_filter.get("brands", []) if v}
    categories = {v for v in product_filter.get("categories", []) if v}
    sub_categories = {v for v in product_filter.get("sub_categories", []) if v}
    sublines = {v for v in product_filter.get("sublines", []) if v}
    sku_statuses = {v for v in product_filter.get("sku_statuses", []) if v}

    if sku_codes and product.sku_code not in sku_codes:
        return False
    if sku_names and product.sku_name not in sku_names:
        return False
    if brands and product.brand not in brands:
        return False
    if categories and product.category not in categories:
        return False
    if sub_categories and product.sub_category not in sub_categories:
        return False
    if sublines and product.sub_line not in sublines:
        return False
    if sku_statuses and product.status not in sku_statuses:
        return False
    return True


async def _load_owner_maps(db: AsyncSession, owner_user_id: int) -> tuple[dict[str, Product], dict[str, str]]:
    products = (
        await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
    ).scalars().all()
    branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    product_map = {str(p.sku_code).strip(): p for p in products}
    branch_name_by_id = {b.branch_id: b.branch_name for b in branches}
    return product_map, branch_name_by_id


def _build_branch_hub_maps(
    hist_rows: list[HistoricalSalesMonthly],
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    latest_by_sku_branch: dict[tuple[str, str], tuple[date, str]] = {}
    latest_by_branch: dict[str, tuple[date, str]] = {}
    for row in hist_rows:
        branch_id = str(row.branch_id or "").strip()
        hub_name = str(row.hub_name or "").strip() or "KZ-HUB"
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        if not branch_id:
            continue
        sku_branch_key = (sku_code, branch_id)
        branch_key = branch_id
        if sku_code and (
            sku_branch_key not in latest_by_sku_branch
            or latest_by_sku_branch[sku_branch_key][0] <= row.date
        ):
            latest_by_sku_branch[sku_branch_key] = (row.date, hub_name)
        if branch_key not in latest_by_branch or latest_by_branch[branch_key][0] <= row.date:
            latest_by_branch[branch_key] = (row.date, hub_name)
    return (
        {key: value[1] for key, value in latest_by_sku_branch.items()},
        {key: value[1] for key, value in latest_by_branch.items()},
    )


def _is_exit_sku_status(value: str | None) -> bool:
    return str(value or "").strip().lower() == "на вывод"


async def build_branch_filter_options(
    db: AsyncSession,
    owner_user_id: int,
    product_filter: dict,
    hub_filter: list[str],
) -> list[str]:
    """Branch dropdown options: respect product + hub (and other product facets), but not branch (Excel-style)."""
    product_map, branch_name_by_id = await _load_owner_maps(db, owner_user_id)
    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
    branch_hub_by_sku_branch, branch_hub_by_branch = _build_branch_hub_maps(hist_rows)

    values: set[str] = set()
    for row in hist_rows:
        branch_id = str(row.branch_id or "").strip()
        if not branch_id:
            continue
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        product = product_map.get(sku_code)
        if product is None:
            continue
        branch_name = branch_name_by_id.get(branch_id, branch_id)
        hub_name = (
            branch_hub_by_sku_branch.get((sku_code, branch_id))
            or branch_hub_by_branch.get(branch_id)
            or str(row.hub_name or "").strip()
            or "KZ-HUB"
        )
        if not _matches_filters(
            product,
            branch_name,
            product_filter,
            [],
            hub_name,
            hub_filter,
        ):
            continue
        display = str(localize_branch_name(branch_name) or branch_name).strip()
        if display:
            values.add(display)
    return sorted(values)


async def build_hub_filter_options(
    db: AsyncSession,
    owner_user_id: int,
    product_filter: dict,
) -> list[str]:
    """Hub dropdown options: respect product + SKU facets, but not branch (Excel-style)."""
    product_map, branch_name_by_id = await _load_owner_maps(db, owner_user_id)
    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()

    values: set[str] = set()
    for row in hist_rows:
        hub_name = str(row.hub_name or "").strip()
        if not hub_name:
            continue
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        product = product_map.get(sku_code)
        if product is None:
            continue
        branch_id = str(row.branch_id or "").strip()
        if branch_id:
            branch_name = branch_name_by_id.get(branch_id, branch_id)
            if not _matches_filters(
                product,
                branch_name,
                product_filter,
                [],
            ):
                continue
        else:
            if not _matches_filters(product, "", product_filter, []):
                continue
        values.add(hub_name)
    return sorted(values)


async def build_sku_status_filter_options(
    db: AsyncSession,
    owner_user_id: int,
    product_filter: dict,
    branch_filter: list[str],
    hub_filter: list[str],
) -> list[str]:
    product_map, branch_name_by_id = await _load_owner_maps(db, owner_user_id)
    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
    branch_hub_by_sku_branch, branch_hub_by_branch = _build_branch_hub_maps(hist_rows)
    product_filter_without_status = {
        **product_filter,
        "sku_statuses": [],
    }

    values: set[str] = set()
    for row in hist_rows:
        branch_id = str(row.branch_id or "").strip()
        if not branch_id:
            continue
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        product = product_map.get(sku_code)
        if product is None:
            continue
        sku_status = str(product.status or "").strip()
        if not sku_status:
            continue
        branch_name = branch_name_by_id.get(branch_id, branch_id)
        hub_name = (
            branch_hub_by_sku_branch.get((sku_code, branch_id))
            or branch_hub_by_branch.get(branch_id)
            or str(row.hub_name or "").strip()
            or "KZ-HUB"
        )
        if not _matches_filters(
            product,
            branch_name,
            product_filter_without_status,
            branch_filter,
            hub_name,
            hub_filter,
        ):
            continue
        values.add(sku_status)
    return sorted(values)


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
    all_hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
    branch_hub_by_sku_branch, branch_hub_by_branch = _build_branch_hub_maps(all_hist_rows)
    price_rows = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for row in price_rows:
        prices_by_sku[str(row.sku_code or "").strip()].append(row)
    for sku_code in prices_by_sku:
        prices_by_sku[sku_code].sort(key=lambda x: x.date)

    def _closest_price(sku_code: str, target_month: date) -> PriceList | None:
        prices = prices_by_sku.get(str(sku_code), [])
        best: PriceList | None = None
        for p in prices:
            p_month = _month_start(p.date)
            if p_month <= target_month:
                if best is None or _month_start(best.date) < p_month:
                    best = p
        return best

    def _closest_dsp(sku_code: str, target_month: date) -> float:
        best = _closest_price(sku_code, target_month)
        return float(best.dsp or 0.0) if best is not None else 0.0

    def _closest_invoice_price(sku_code: str, target_month: date) -> float:
        best = _closest_price(sku_code, target_month)
        return float(best.invoice_price or 0.0) if best is not None else 0.0

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
    historical_hub_stock_by_period: dict[str, dict[str, float]] = {}
    allowed_hubs_for_branch_filter: set[str] | None = None
    if ctx.branch_filter:
        allowed_hubs_for_branch_filter = set()
        for row in all_hist_rows:
            branch_id = str(row.branch_id or "").strip()
            if not branch_id:
                continue
            sku_code = str(row.sku_code or row.sku_id or "").strip()
            product = product_map.get(sku_code)
            if product is None:
                continue
            branch_name = branch_name_by_id.get(branch_id, branch_id)
            hub_name = str(row.hub_name or "").strip() or "KZ-HUB"
            if _matches_filters(
                product,
                branch_name,
                ctx.product_filter,
                ctx.branch_filter,
                hub_name,
                [],
            ):
                allowed_hubs_for_branch_filter.add(hub_name)

    for r in hist_rows:
        if r.date >= ctx.planning_month:
            continue
        sku_code = str(r.sku_code or "").strip()
        product = product_map.get(sku_code)
        if not product:
            continue
        branch_id = str(r.branch_id or "").strip()
        hub_name = str(r.hub_name or "").strip() or "KZ-HUB"
        if not branch_id:
            if ctx.hub_filter and hub_name not in set(ctx.hub_filter):
                continue
            if allowed_hubs_for_branch_filter is not None and hub_name not in allowed_hubs_for_branch_filter:
                continue
            if not _matches_filters(product, "", ctx.product_filter, []):
                continue
            dsp = _closest_dsp(sku_code, _month_start(r.date))
            invoice_price = _closest_invoice_price(sku_code, _month_start(r.date))
            stock_mc = float(r.past_available_stock or 0.0)
            period_key = _month_start(r.date).isoformat()
            bucket = historical_hub_stock_by_period.setdefault(
                period_key,
                {
                    "past_hub_stock": 0.0,
                    "past_hub_stock_gross_weight_kg": 0.0,
                    "past_hub_stock_net_weight_kg": 0.0,
                    "past_hub_stock_amount_kzt": 0.0,
                    "past_hub_stock_invoice_amount_kzt": 0.0,
                },
            )
            bucket["past_hub_stock"] += stock_mc
            bucket["past_hub_stock_gross_weight_kg"] += stock_mc * float(product.master_carton_gross_weight_kg or 0.0)
            bucket["past_hub_stock_net_weight_kg"] += stock_mc * float(product.master_carton_net_weight_kg or 0.0)
            bucket["past_hub_stock_amount_kzt"] += stock_mc * float(product.pieces_in_master_carton or 0.0) * dsp
            bucket["past_hub_stock_invoice_amount_kzt"] += stock_mc * float(product.pieces_in_master_carton or 0.0) * invoice_price
            continue
        branch_name = branch_name_by_id.get(branch_id, branch_id)
        if not _matches_filters(
            product,
            branch_name,
            ctx.product_filter,
            ctx.branch_filter,
            hub_name,
            ctx.hub_filter,
        ):
            continue
        payload = {
            "period": _month_start(r.date).isoformat(),
            "branch_name": branch_name,
            "brand": product.brand,
            "category": product.category,
            "sub_category": product.sub_category,
            "subline": product.sub_line,
            "sku_status": product.status,
            "sku_name": product.sku_name,
        }
        key = _key_tuple(payload)
        fact_qty = float(r.fact_quantity_in_mc or 0.0)
        target_qty = float(r.target_quantity_in_mc or 0.0)
        fact_amount = float(r.fact_amount_kzt or 0.0)
        target_amount = float(r.target_amount_kzt or 0.0)
        if abs(fact_amount) < 1e-9 or abs(target_amount) < 1e-9:
            dsp = _closest_dsp(sku_code, _month_start(r.date))
            pieces = float(product.pieces_in_master_carton or 0.0)
            if abs(fact_amount) < 1e-9:
                fact_amount = fact_qty * pieces * dsp
            if abs(target_amount) < 1e-9:
                target_amount = target_qty * pieces * dsp
        if key not in hist_buckets:
            hist_buckets[key] = {
                **payload,
                "fact_quantity_in_mc": 0.0,
                "fact_gross_weight_kg": 0.0,
                "fact_net_weight_kg": 0.0,
                "fact_volume_cbm": 0.0,
                "fact_amount_kzt": 0.0,
                "fact_invoice_amount_kzt": 0.0,
                "target_quantity_in_mc": 0.0,
                "target_gross_weight_kg": 0.0,
                "target_net_weight_kg": 0.0,
                "target_volume_cbm": 0.0,
                "target_amount_kzt": 0.0,
                "target_invoice_amount_kzt": 0.0,
                "past_available_stock": 0.0,
                "past_available_stock_gross_weight_kg": 0.0,
                "past_available_stock_net_weight_kg": 0.0,
                "past_available_stock_amount_kzt": 0.0,
                "past_available_stock_invoice_amount_kzt": 0.0,
            }
        bucket = hist_buckets[key]
        dsp = _closest_dsp(sku_code, _month_start(r.date))
        invoice_price = _closest_invoice_price(sku_code, _month_start(r.date))
        stock_mc = float(r.past_available_stock or 0.0)
        net_weight = float(product.master_carton_net_weight_kg or 0.0)
        bucket["fact_quantity_in_mc"] += fact_qty
        bucket["fact_gross_weight_kg"] += float(r.fact_gross_weight_kg or 0.0)
        bucket["fact_net_weight_kg"] += fact_qty * net_weight
        bucket["fact_volume_cbm"] += float(r.fact_volume_cbm or 0.0)
        bucket["fact_amount_kzt"] += fact_amount
        bucket["fact_invoice_amount_kzt"] += fact_qty * float(product.pieces_in_master_carton or 0.0) * invoice_price
        bucket["target_quantity_in_mc"] += target_qty
        bucket["target_gross_weight_kg"] += float(r.target_gross_weight_kg or 0.0)
        bucket["target_net_weight_kg"] += target_qty * net_weight
        bucket["target_volume_cbm"] += float(r.target_volume_cbm or 0.0)
        bucket["target_amount_kzt"] += target_amount
        bucket["target_invoice_amount_kzt"] += target_qty * float(product.pieces_in_master_carton or 0.0) * invoice_price
        bucket["past_available_stock"] += stock_mc
        bucket["past_available_stock_gross_weight_kg"] += stock_mc * float(product.master_carton_gross_weight_kg or 0.0)
        bucket["past_available_stock_net_weight_kg"] += stock_mc * net_weight
        bucket["past_available_stock_amount_kzt"] += stock_mc * float(product.pieces_in_master_carton or 0.0) * dsp
        bucket["past_available_stock_invoice_amount_kzt"] += stock_mc * float(product.pieces_in_master_carton or 0.0) * invoice_price

    historical_table = _aggregate_period_totals(
        list(hist_buckets.values()),
        [
            "fact_quantity_in_mc",
            "fact_gross_weight_kg",
            "fact_net_weight_kg",
            "fact_volume_cbm",
            "fact_amount_kzt",
            "fact_invoice_amount_kzt",
            "target_quantity_in_mc",
            "target_gross_weight_kg",
            "target_net_weight_kg",
            "target_volume_cbm",
            "target_amount_kzt",
            "target_invoice_amount_kzt",
            "past_available_stock",
            "past_available_stock_gross_weight_kg",
            "past_available_stock_net_weight_kg",
            "past_available_stock_amount_kzt",
            "past_available_stock_invoice_amount_kzt",
        ],
    )
    for row in historical_table:
        period_key = str(row["period"])
        hub_stock = historical_hub_stock_by_period.get(period_key, {})
        row["past_hub_stock"] = round(float(hub_stock.get("past_hub_stock", 0.0) or 0.0), 2)
        row["past_hub_stock_gross_weight_kg"] = round(
            float(hub_stock.get("past_hub_stock_gross_weight_kg", 0.0) or 0.0), 2
        )
        row["past_hub_stock_net_weight_kg"] = round(
            float(hub_stock.get("past_hub_stock_net_weight_kg", 0.0) or 0.0), 2
        )
        row["past_hub_stock_amount_kzt"] = round(
            float(hub_stock.get("past_hub_stock_amount_kzt", 0.0) or 0.0), 2
        )
        row["past_hub_stock_invoice_amount_kzt"] = round(
            float(hub_stock.get("past_hub_stock_invoice_amount_kzt", 0.0) or 0.0), 2
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
        sku_code = str(r.sku_code or "").strip()
        product = product_map.get(sku_code)
        if not product:
            continue
        branch_id = str(r.branch_id or "").strip()
        branch_name = branch_name_by_id.get(branch_id, branch_id)
        hub_name = (
            branch_hub_by_sku_branch.get((sku_code, branch_id))
            or branch_hub_by_branch.get(branch_id)
            or "KZ-HUB"
        )
        baseline_qty = float(r.baseline_forecast_quantity_in_mc or 0.0)
        adjusted_qty = (
            float(r.adjusted_forecast_quantity_in_mc)
            if r.adjusted_forecast_quantity_in_mc is not None
            else baseline_qty
        )
        if _is_exit_sku_status(product.status):
            baseline_qty = 0.0
            adjusted_qty = 0.0
        atomic_rows.append(
            {
                "period": _month_start(r.date).isoformat(),
                "branch_name": branch_name,
                "hub_name": hub_name,
                "brand": product.brand,
                "category": product.category,
                "sub_category": product.sub_category,
                "subline": product.sub_line,
                "sku_status": product.status,
                "sku_name": product.sku_name,
                "_product_obj": product,
                "baseline_forecast_gross_weight_kg": (
                    0.0
                    if _is_exit_sku_status(product.status)
                    else float(r.baseline_forecast_gross_weight_kg or 0.0)
                ),
                "baseline_forecast_net_weight_kg": (
                    baseline_qty
                    * float(product.master_carton_net_weight_kg or 0.0)
                ),
                "baseline_forecast_volume_cbm": (
                    0.0
                    if _is_exit_sku_status(product.status)
                    else float(r.baseline_forecast_volume_cbm or 0.0)
                ),
                "baseline_forecast_amount_kzt": (
                    0.0
                    if _is_exit_sku_status(product.status)
                    else float(r.baseline_forecast_amount_kzt or 0.0)
                ),
                "baseline_forecast_invoice_amount_kzt": (
                    baseline_qty
                    * float(product.pieces_in_master_carton or 0.0)
                    * _closest_invoice_price(sku_code, _month_start(r.date))
                ),
                "baseline_forecast_quantity_in_mc": baseline_qty,
                "adjusted_forecast_quantity_in_mc": adjusted_qty,
                "adjusted_forecast_gross_weight_kg": (
                    0.0
                    if _is_exit_sku_status(product.status)
                    else float(r.adjusted_forecast_gross_weight_kg) if r.adjusted_forecast_gross_weight_kg is not None else float(r.baseline_forecast_gross_weight_kg or 0.0)
                ),
                "adjusted_forecast_net_weight_kg": (
                    adjusted_qty
                    * float(product.master_carton_net_weight_kg or 0.0)
                ),
                "adjusted_forecast_volume_cbm": (
                    0.0
                    if _is_exit_sku_status(product.status)
                    else float(r.adjusted_forecast_volume_cbm) if r.adjusted_forecast_volume_cbm is not None else float(r.baseline_forecast_volume_cbm or 0.0)
                ),
                "adjusted_forecast_amount_kzt": (
                    0.0
                    if _is_exit_sku_status(product.status)
                    else float(r.adjusted_forecast_amount_kzt) if r.adjusted_forecast_amount_kzt is not None else float(r.baseline_forecast_amount_kzt or 0.0)
                ),
                "adjusted_forecast_invoice_amount_kzt": (
                    adjusted_qty
                    * float(product.pieces_in_master_carton or 0.0)
                    * _closest_invoice_price(sku_code, _month_start(r.date))
                ),
                "future_available_stock": float(r.future_available_stock or 0.0),
                "future_available_stock_gross_weight_kg": (
                    float(r.future_available_stock or 0.0)
                    * float(product.master_carton_gross_weight_kg or 0.0)
                ),
                "future_available_stock_net_weight_kg": (
                    float(r.future_available_stock or 0.0)
                    * float(product.master_carton_net_weight_kg or 0.0)
                ),
                "future_available_stock_amount_kzt": (
                    float(r.future_available_stock or 0.0)
                    * float(product.pieces_in_master_carton or 0.0)
                    * _closest_dsp(sku_code, _month_start(r.date))
                ),
                "future_available_stock_invoice_amount_kzt": (
                    float(r.future_available_stock or 0.0)
                    * float(product.pieces_in_master_carton or 0.0)
                    * _closest_invoice_price(sku_code, _month_start(r.date))
                ),
                "_applied_override_metrics": set(),
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
                    r["_applied_override_metrics"].add(adjusted_metric)
            else:
                even_share = float(ov.value) / len(matched)
                for r in matched:
                    r[adjusted_metric] = even_share
                    r["_applied_override_metrics"].add(adjusted_metric)

    for r in atomic_rows:
        applied_metrics: set[str] = r.get("_applied_override_metrics", set())
        if "adjusted_forecast_quantity_in_mc" not in applied_metrics:
            continue

        # Cases override must propagate to DSP/weight/volume unless those metrics
        # were explicitly overridden by their own patch calls.
        product = r["_product_obj"]
        period_date = date.fromisoformat(str(r["period"]))
        adjusted_qty = float(r.get("adjusted_forecast_quantity_in_mc") or 0.0)
        pieces = float(product.pieces_in_master_carton or 0.0)
        dsp = _closest_dsp(str(product.sku_code or "").strip(), _month_start(period_date))

        if "adjusted_forecast_amount_kzt" not in applied_metrics:
            r["adjusted_forecast_amount_kzt"] = adjusted_qty * pieces * dsp
        if "adjusted_forecast_invoice_amount_kzt" not in applied_metrics:
            invoice_price = _closest_invoice_price(str(product.sku_code or "").strip(), _month_start(period_date))
            r["adjusted_forecast_invoice_amount_kzt"] = adjusted_qty * pieces * invoice_price
        if "adjusted_forecast_gross_weight_kg" not in applied_metrics:
            r["adjusted_forecast_gross_weight_kg"] = adjusted_qty * float(
                product.master_carton_gross_weight_kg or 0.0
            )
        if "adjusted_forecast_net_weight_kg" not in applied_metrics:
            r["adjusted_forecast_net_weight_kg"] = adjusted_qty * float(
                product.master_carton_net_weight_kg or 0.0
            )
        if "adjusted_forecast_volume_cbm" not in applied_metrics:
            r["adjusted_forecast_volume_cbm"] = adjusted_qty * float(
                product.master_carton_volume_cbm or 0.0
            )

    for r in atomic_rows:
        if not _is_exit_sku_status(r["_product_obj"].status):
            continue
        for metric in [
            "baseline_forecast_quantity_in_mc",
            "baseline_forecast_gross_weight_kg",
            "baseline_forecast_net_weight_kg",
            "baseline_forecast_volume_cbm",
            "baseline_forecast_amount_kzt",
            "baseline_forecast_invoice_amount_kzt",
            "adjusted_forecast_quantity_in_mc",
            "adjusted_forecast_gross_weight_kg",
            "adjusted_forecast_net_weight_kg",
            "adjusted_forecast_volume_cbm",
            "adjusted_forecast_amount_kzt",
            "adjusted_forecast_invoice_amount_kzt",
        ]:
            r[metric] = 0.0

    # Apply transient/saved filters only after override distribution so filtered views
    # receive their proportional adjusted share instead of full override totals.
    filtered_atomic_rows = [
        r
        for r in atomic_rows
        if _matches_filters(
            r["_product_obj"],
            str(r["branch_name"]),
            ctx.product_filter,
            ctx.branch_filter,
            str(r.get("hub_name") or ""),
            ctx.hub_filter,
        )
    ]

    latest_hub_stock_by_hub_sku: dict[tuple[str, str], tuple[date, float]] = {}
    for row in all_hist_rows:
        if str(row.branch_id or "").strip():
            continue
        hub_name = str(row.hub_name or "").strip()
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        if not hub_name or not sku_code:
            continue
        product = product_map.get(sku_code)
        if product is None:
            continue
        if ctx.hub_filter and hub_name not in set(ctx.hub_filter):
            continue
        if allowed_hubs_for_branch_filter is not None and hub_name not in allowed_hubs_for_branch_filter:
            continue
        if not _matches_filters(product, "", ctx.product_filter, []):
            continue
        key = (hub_name, sku_code)
        if key not in latest_hub_stock_by_hub_sku or latest_hub_stock_by_hub_sku[key][0] <= row.date:
            latest_hub_stock_by_hub_sku[key] = (row.date, float(row.past_available_stock or 0.0))

    future_hub_stock_by_period: dict[str, dict[str, float]] = {}
    forecast_periods = sorted({str(row["period"]) for row in filtered_atomic_rows})
    for period_key in forecast_periods:
        period_date = date.fromisoformat(period_key)
        bucket = future_hub_stock_by_period.setdefault(
            period_key,
            {
                "future_hub_stock": 0.0,
                "future_hub_stock_gross_weight_kg": 0.0,
                "future_hub_stock_net_weight_kg": 0.0,
                "future_hub_stock_amount_kzt": 0.0,
                "future_hub_stock_invoice_amount_kzt": 0.0,
            },
        )
        for (hub_name, sku_code), (_stock_date, stock_mc) in latest_hub_stock_by_hub_sku.items():
            product = product_map.get(sku_code)
            if product is None:
                continue
            dsp = _closest_dsp(sku_code, _month_start(period_date))
            invoice_price = _closest_invoice_price(sku_code, _month_start(period_date))
            bucket["future_hub_stock"] += stock_mc
            bucket["future_hub_stock_gross_weight_kg"] += stock_mc * float(product.master_carton_gross_weight_kg or 0.0)
            bucket["future_hub_stock_net_weight_kg"] += stock_mc * float(product.master_carton_net_weight_kg or 0.0)
            bucket["future_hub_stock_amount_kzt"] += stock_mc * float(product.pieces_in_master_carton or 0.0) * dsp
            bucket["future_hub_stock_invoice_amount_kzt"] += stock_mc * float(product.pieces_in_master_carton or 0.0) * invoice_price

    forecast_buckets: dict[tuple, dict] = {}
    for r in filtered_atomic_rows:
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
                "baseline_forecast_net_weight_kg": 0.0,
                "baseline_forecast_volume_cbm": 0.0,
                "baseline_forecast_amount_kzt": 0.0,
                "baseline_forecast_invoice_amount_kzt": 0.0,
                "adjusted_forecast_quantity_in_mc": 0.0,
                "adjusted_forecast_gross_weight_kg": 0.0,
                "adjusted_forecast_net_weight_kg": 0.0,
                "adjusted_forecast_volume_cbm": 0.0,
                "adjusted_forecast_amount_kzt": 0.0,
                "adjusted_forecast_invoice_amount_kzt": 0.0,
                "future_available_stock": 0.0,
                "future_available_stock_gross_weight_kg": 0.0,
                "future_available_stock_net_weight_kg": 0.0,
                "future_available_stock_amount_kzt": 0.0,
                "future_available_stock_invoice_amount_kzt": 0.0,
            }
        b = forecast_buckets[key]
        for metric in [
            "baseline_forecast_quantity_in_mc",
            "baseline_forecast_gross_weight_kg",
            "baseline_forecast_net_weight_kg",
            "baseline_forecast_volume_cbm",
            "baseline_forecast_amount_kzt",
            "baseline_forecast_invoice_amount_kzt",
            "adjusted_forecast_quantity_in_mc",
            "adjusted_forecast_gross_weight_kg",
            "adjusted_forecast_net_weight_kg",
            "adjusted_forecast_volume_cbm",
            "adjusted_forecast_amount_kzt",
            "adjusted_forecast_invoice_amount_kzt",
            "future_available_stock",
            "future_available_stock_gross_weight_kg",
            "future_available_stock_net_weight_kg",
            "future_available_stock_amount_kzt",
            "future_available_stock_invoice_amount_kzt",
        ]:
            b[metric] += float(r[metric] or 0.0)

    forecast_table = _aggregate_period_totals(
        list(forecast_buckets.values()),
        [
            "baseline_forecast_quantity_in_mc",
            "baseline_forecast_gross_weight_kg",
            "baseline_forecast_net_weight_kg",
            "baseline_forecast_volume_cbm",
            "baseline_forecast_amount_kzt",
            "baseline_forecast_invoice_amount_kzt",
            "adjusted_forecast_quantity_in_mc",
            "adjusted_forecast_gross_weight_kg",
            "adjusted_forecast_net_weight_kg",
            "adjusted_forecast_volume_cbm",
            "adjusted_forecast_amount_kzt",
            "adjusted_forecast_invoice_amount_kzt",
            "future_available_stock",
            "future_available_stock_gross_weight_kg",
            "future_available_stock_net_weight_kg",
            "future_available_stock_amount_kzt",
            "future_available_stock_invoice_amount_kzt",
        ],
    )
    for row in forecast_table:
        period_key = str(row["period"])
        hub_stock = future_hub_stock_by_period.get(period_key, {})
        row["future_hub_stock"] = round(float(hub_stock.get("future_hub_stock", 0.0) or 0.0), 2)
        row["future_hub_stock_gross_weight_kg"] = round(
            float(hub_stock.get("future_hub_stock_gross_weight_kg", 0.0) or 0.0), 2
        )
        row["future_hub_stock_net_weight_kg"] = round(
            float(hub_stock.get("future_hub_stock_net_weight_kg", 0.0) or 0.0), 2
        )
        row["future_hub_stock_amount_kzt"] = round(
            float(hub_stock.get("future_hub_stock_amount_kzt", 0.0) or 0.0), 2
        )
        row["future_hub_stock_invoice_amount_kzt"] = round(
            float(hub_stock.get("future_hub_stock_invoice_amount_kzt", 0.0) or 0.0), 2
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
    hub_filter: object | None = None,
) -> ReportingContext:
    normalized_view = validate_view_type(view_type)
    if normalized_view == "dsp":
        price_exists = (
            await db.execute(
                select(PriceList.id).where(PriceList.owner_user_id == owner_user_id).limit(1)
            )
        ).first()
        if price_exists is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Режим DSP недоступен, пока не загружен файл price-list. "
                    "Сначала загрузите price-list."
                ),
            )
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
        hub_filter=parse_hub_filter(hub_filter),
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
        metric_input = str(ov.get("metric_type", "")).strip()
        metric = normalize_override_metric(metric_input)
        if metric is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Неподдерживаемый metric_type. Допустимые значения: DSP, Cases, Gross Weight, Net Weight"
                ),
            )
        period_value = ov.get("period")
        if not isinstance(period_value, date):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Период override должен быть корректной датой",
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


def _override_scope_filters(model, override: dict, metric: str, period_value: date) -> list:
    scope_fields = ["branch_name", "brand", "category", "sub_category", "subline", "sku_name"]
    filters = [
        model.report_id == int(override["report_id"]),
        model.owner_user_id == int(override["owner_user_id"]),
        model.period == _month_start(period_value),
        model.metric_type == metric,
    ]
    for field_name in scope_fields:
        field = getattr(model, field_name)
        value = override.get(field_name)
        filters.append(field.is_(None) if value is None else field == value)
    return filters


async def upsert_report_overrides(
    db: AsyncSession,
    report_id: int,
    owner_user_id: int,
    overrides: list[dict],
) -> None:
    inserts: list[DPReportForecastOverride] = []
    for ov in overrides:
        metric_input = str(ov.get("metric_type", "")).strip()
        metric = normalize_override_metric(metric_input)
        if metric is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Неподдерживаемый metric_type. Допустимые значения: DSP, Cases, Gross Weight, Net Weight"
                ),
            )
        period_value = ov.get("period")
        if not isinstance(period_value, date):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Период override должен быть корректной датой",
            )
        scoped_override = {
            **ov,
            "report_id": report_id,
            "owner_user_id": owner_user_id,
        }
        await db.execute(
            delete(DPReportForecastOverride).where(
                *_override_scope_filters(DPReportForecastOverride, scoped_override, metric, period_value)
            )
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
