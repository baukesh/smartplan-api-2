from __future__ import annotations

import calendar
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branch_localization import normalize_branch_lookup
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product
from app.models.derived import ForecastSalesMonthly
from app.models.reporting import PromoActivity
from app.services.reporting_service import validate_view_type


PROMO_DATE_FORMAT = "%d.%m.%Y"


@dataclass(frozen=True)
class PromoFilters:
    date_from: date | None = None
    date_to: date | None = None
    sku_code: tuple[str, ...] = ()
    sku_name: tuple[str, ...] = ()
    brand: tuple[str, ...] = ()
    category: tuple[str, ...] = ()
    sub_category: tuple[str, ...] = ()
    subline: tuple[str, ...] = ()
    branch_name: tuple[str, ...] = ()
    promo_channel: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromoComputedValues:
    fact_value: float
    baseline_forecast_value: float
    promo_effect: float
    promo_plan_value: float


def parse_promo_date(value: str, *, field_name: str) -> date:
    try:
        return datetime.strptime(str(value or "").strip(), PROMO_DATE_FORMAT).date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Поле {field_name} должно быть в формате DD.MM.YYYY",
        ) from exc


def format_promo_date(value: date) -> str:
    return value.strftime(PROMO_DATE_FORMAT)


def parse_promo_list(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def serialize_promo_list(values: list[str]) -> str:
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    return json.dumps(cleaned, ensure_ascii=False)


def normalize_promo_filters(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    sku_code: list[str] | None = None,
    sku_name: list[str] | None = None,
    brand: list[str] | None = None,
    category: list[str] | None = None,
    sub_category: list[str] | None = None,
    subline: list[str] | None = None,
    branch_name: list[str] | None = None,
    promo_channel: list[str] | None = None,
) -> PromoFilters:
    def clean(values: list[str] | None) -> tuple[str, ...]:
        return tuple(str(v).strip() for v in values or [] if str(v).strip())

    return PromoFilters(
        date_from=date_from,
        date_to=date_to,
        sku_code=clean(sku_code),
        sku_name=clean(sku_name),
        brand=clean(brand),
        category=clean(category),
        sub_category=clean(sub_category),
        subline=clean(subline),
        branch_name=clean(branch_name),
        promo_channel=clean(promo_channel),
    )


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def _promo_month_weights(start: date, end: date) -> dict[date, float]:
    weights: dict[date, float] = {}
    cursor = _month_start(start)
    while cursor <= _month_start(end):
        overlap_start = max(start, cursor)
        overlap_end = min(end, _month_end(cursor))
        if overlap_start <= overlap_end:
            month_days = calendar.monthrange(cursor.year, cursor.month)[1]
            overlap_days = (overlap_end - overlap_start).days + 1
            weights[cursor] = overlap_days / month_days
        cursor = _add_months(cursor, 1)
    return weights


def _overlap_dates(left_start: date, left_end: date, right_start: date, right_end: date) -> tuple[date, date] | None:
    start = max(left_start, right_start)
    end = min(left_end, right_end)
    if start > end:
        return None
    return start, end


def _round_value(value: float) -> float:
    return round(float(value or 0.0), 2)


def _round_view_value(value: float, view_type: str) -> float:
    if (view_type or "").strip().lower() == "cases":
        return float(round(value))
    return _round_value(value)


def _matches_product_filters(product: Product, filters: PromoFilters) -> bool:
    if filters.sku_code and product.sku_code not in set(filters.sku_code):
        return False
    if filters.sku_name and product.sku_name not in set(filters.sku_name):
        return False
    if filters.brand and product.brand not in set(filters.brand):
        return False
    if filters.category and product.category not in set(filters.category):
        return False
    if filters.sub_category and product.sub_category not in set(filters.sub_category):
        return False
    if filters.subline and product.sub_line not in set(filters.subline):
        return False
    return True


def _filter_branch_names(branches: list[str], filters: PromoFilters) -> list[str]:
    if not filters.branch_name:
        return branches
    requested = {normalize_branch_lookup(v) for v in filters.branch_name}
    return [branch for branch in branches if normalize_branch_lookup(branch) in requested]


def _filter_products(products: list[Product], promo_sku_codes: list[str], filters: PromoFilters) -> list[Product]:
    promo_codes = {str(v).strip() for v in promo_sku_codes if str(v).strip()}
    return [
        product
        for product in products
        if (not promo_codes or str(product.sku_code).strip() in promo_codes)
        and _matches_product_filters(product, filters)
    ]


def _closest_price(prices_by_sku: dict[str, list[PriceList]], sku_code: str, target_month: date) -> PriceList | None:
    prices = prices_by_sku.get(str(sku_code or "").strip(), [])
    best: PriceList | None = None
    for price in prices:
        price_month = _month_start(price.date)
        if price_month <= target_month and (best is None or _month_start(best.date) < price_month):
            best = price
    return best


def _case_conversion_factor(
    *,
    view_type: str,
    product: Product | None,
    price: PriceList | None,
) -> float:
    normalized = validate_view_type(view_type)
    if normalized == "dsp":
        return float(product.pieces_in_master_carton or 0.0) * float(price.dsp or 0.0) if product and price else 0.0
    if normalized == "invoice price":
        return (
            float(product.pieces_in_master_carton or 0.0) * float(price.invoice_price or 0.0)
            if product and price
            else 0.0
        )
    if normalized == "gross weight":
        return float(product.master_carton_gross_weight_kg or 0.0) if product else 0.0
    if normalized == "net weight":
        return float(product.master_carton_net_weight_kg or 0.0) if product else 0.0
    return 1.0


def _historical_value(
    row: HistoricalSalesMonthly,
    *,
    view_type: str,
    product: Product | None,
    price: PriceList | None,
) -> float:
    normalized = validate_view_type(view_type)
    cases = float(row.fact_quantity_in_mc or 0.0)
    if normalized == "dsp":
        return float(row.fact_amount_kzt or 0.0)
    if normalized == "invoice price":
        return cases * _case_conversion_factor(view_type=normalized, product=product, price=price)
    if normalized == "gross weight":
        return float(row.fact_gross_weight_kg or 0.0)
    if normalized == "net weight":
        return cases * float(product.master_carton_net_weight_kg or 0.0) if product else 0.0
    return cases


def _forecast_baseline_value(
    row: ForecastSalesMonthly,
    *,
    view_type: str,
    product: Product | None,
    price: PriceList | None,
) -> float:
    normalized = validate_view_type(view_type)
    cases = float(row.baseline_forecast_quantity_in_mc or 0.0)
    if normalized == "dsp":
        return float(row.baseline_forecast_amount_kzt or 0.0)
    if normalized == "invoice price":
        return cases * _case_conversion_factor(view_type=normalized, product=product, price=price)
    if normalized == "gross weight":
        return float(row.baseline_forecast_gross_weight_kg or 0.0)
    if normalized == "net weight":
        return cases * float(product.master_carton_net_weight_kg or 0.0) if product else 0.0
    return cases


def _scope_intersects(left_values: list[str], right_values: list[str], *, branch: bool = False) -> bool:
    if not left_values or not right_values:
        return True
    if branch:
        left = {normalize_branch_lookup(v) for v in left_values}
        right = {normalize_branch_lookup(v) for v in right_values}
    else:
        left = {str(v).strip() for v in left_values if str(v).strip()}
        right = {str(v).strip() for v in right_values if str(v).strip()}
    return bool(left & right)


def _branch_ids_for_names(branch_name_by_id: dict[str, str], branch_names: list[str]) -> set[str]:
    if not branch_names:
        return set(branch_name_by_id.keys())
    requested = {normalize_branch_lookup(v) for v in branch_names}
    return {
        branch_id
        for branch_id, branch_name in branch_name_by_id.items()
        if normalize_branch_lookup(branch_name) in requested
    }


def _share_weights(
    hist_rows: list[HistoricalSalesMonthly],
    *,
    products_by_code: dict[str, Product],
    branch_name_by_id: dict[str, str],
    branches: list[str],
    sku_codes: list[str],
    start: date,
) -> dict[tuple[str, str], float]:
    branch_ids = _branch_ids_for_names(branch_name_by_id, branches)
    allowed_skus = {str(v).strip() for v in sku_codes if str(v).strip()}
    from_date = _add_months(_month_start(start), -6)
    to_date = _month_start(start) - timedelta(days=1)
    buckets: dict[tuple[str, str], float] = defaultdict(float)
    for row in hist_rows:
        if row.date < from_date or row.date > to_date:
            continue
        branch_id = str(row.branch_id or "").strip()
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        if branch_id not in branch_ids or sku_code not in allowed_skus:
            continue
        if sku_code not in products_by_code:
            continue
        branch_name = branch_name_by_id.get(branch_id, branch_id)
        buckets[(branch_name, sku_code)] += float(row.fact_quantity_in_mc or 0.0)
    total = sum(value for value in buckets.values() if value > 0)
    if total > 0:
        return {key: value / total for key, value in buckets.items() if value > 0}

    pairs = [
        (branch_name_by_id.get(branch_id, branch_id), sku_code)
        for branch_id in sorted(branch_ids)
        for sku_code in sorted(allowed_skus)
        if sku_code in products_by_code
    ]
    if not pairs:
        return {}
    equal_share = 1.0 / len(pairs)
    return {pair: equal_share for pair in pairs}


async def load_owner_promos(db: AsyncSession, owner_user_id: int) -> list[PromoActivity]:
    rows = (
        await db.execute(
            select(PromoActivity)
            .where(PromoActivity.owner_user_id == owner_user_id)
            .order_by(PromoActivity.promo_start_date, PromoActivity.id)
        )
    ).scalars().all()
    return list(rows)


async def load_promo_dropdowns(db: AsyncSession, owner_user_id: int) -> tuple[list[str], list[str]]:
    branches = (
        await db.execute(
            select(Branch.branch_name)
            .where(Branch.owner_user_id == owner_user_id)
            .order_by(Branch.branch_name)
        )
    ).scalars().all()
    sku_codes = (
        await db.execute(
            select(Product.sku_code)
            .where(Product.owner_user_id == owner_user_id)
            .order_by(Product.sku_code)
        )
    ).scalars().all()
    return (
        sorted({str(value).strip() for value in branches if str(value).strip()}),
        sorted({str(value).strip() for value in sku_codes if str(value).strip()}),
    )


async def compute_promo_values(
    db: AsyncSession,
    *,
    owner_user_id: int,
    promo: PromoActivity,
    view_type: str,
    filters: PromoFilters,
    all_promos: list[PromoActivity] | None = None,
    include_overlaps: bool = True,
) -> PromoComputedValues | None:
    normalized_view_type = validate_view_type(view_type or "cases")
    if filters.promo_channel:
        channels = {v.strip().lower() for v in filters.promo_channel if v.strip()}
        if str(promo.promo_channel or "").strip().lower() not in channels:
            return None

    promo_start = max(promo.promo_start_date, filters.date_from) if filters.date_from else promo.promo_start_date
    promo_end = min(promo.promo_end_date, filters.date_to) if filters.date_to else promo.promo_end_date
    if promo_start > promo_end:
        return None

    products = (
        await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
    ).scalars().all()
    branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    prices = (
        await db.execute(
            select(PriceList)
            .where(PriceList.owner_user_id == owner_user_id)
            .order_by(PriceList.sku_code, PriceList.date)
        )
    ).scalars().all()
    branch_name_by_id = {str(row.branch_id).strip(): str(row.branch_name).strip() for row in branches}
    products_by_code = {str(row.sku_code).strip(): row for row in products if str(row.sku_code).strip()}
    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for row in prices:
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        if sku_code:
            prices_by_sku[sku_code].append(row)

    scoped_branches = _filter_branch_names(parse_promo_list(promo.promo_branches), filters)
    scoped_products = _filter_products(list(products), parse_promo_list(promo.promo_sku_codes), filters)
    scoped_sku_codes = [str(product.sku_code).strip() for product in scoped_products if str(product.sku_code).strip()]
    if not scoped_branches or not scoped_sku_codes:
        return None

    month_weights = _promo_month_weights(promo_start, promo_end)
    hist_from = _month_start(promo_start)
    hist_to = _month_start(promo_end)
    share_from = _add_months(_month_start(promo.promo_start_date), -6)
    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id,
                HistoricalSalesMonthly.date >= share_from,
                HistoricalSalesMonthly.date <= hist_to,
            )
        )
    ).scalars().all()
    forecast_rows = (
        await db.execute(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.owner_user_id == owner_user_id,
                ForecastSalesMonthly.date >= hist_from,
                ForecastSalesMonthly.date <= hist_to,
            )
        )
    ).scalars().all()

    branch_ids = _branch_ids_for_names(branch_name_by_id, scoped_branches)
    sku_set = set(scoped_sku_codes)
    fact_value = 0.0
    for row in hist_rows:
        month = _month_start(row.date)
        weight = month_weights.get(month)
        if weight is None:
            continue
        branch_id = str(row.branch_id or "").strip()
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        if branch_id not in branch_ids or sku_code not in sku_set:
            continue
        product = products_by_code.get(sku_code)
        price = _closest_price(prices_by_sku, sku_code, month)
        fact_value += _historical_value(row, view_type=normalized_view_type, product=product, price=price) * weight

    baseline_value = 0.0
    for row in forecast_rows:
        month = _month_start(row.date)
        weight = month_weights.get(month)
        if weight is None:
            continue
        branch_id = str(row.branch_id or "").strip()
        sku_code = str(row.sku_code or row.sku_id or "").strip()
        if branch_id not in branch_ids or sku_code not in sku_set:
            continue
        product = products_by_code.get(sku_code)
        price = _closest_price(prices_by_sku, sku_code, month)
        baseline_value += _forecast_baseline_value(row, view_type=normalized_view_type, product=product, price=price) * weight

    promos_for_effect = [promo]
    if include_overlaps:
        if all_promos is None:
            all_promos = await load_owner_promos(db, owner_user_id)
        for candidate in all_promos:
            if candidate.id == promo.id:
                continue
            if _overlap_dates(promo_start, promo_end, candidate.promo_start_date, candidate.promo_end_date) is None:
                continue
            if not _scope_intersects(scoped_branches, parse_promo_list(candidate.promo_branches), branch=True):
                continue
            if not _scope_intersects(scoped_sku_codes, parse_promo_list(candidate.promo_sku_codes)):
                continue
            promos_for_effect.append(candidate)

    promo_effect = 0.0
    min_share_from = min(_add_months(_month_start(effect_promo.promo_start_date), -6) for effect_promo in promos_for_effect)
    if min_share_from < share_from:
        hist_rows = (
            await db.execute(
                select(HistoricalSalesMonthly).where(
                    HistoricalSalesMonthly.owner_user_id == owner_user_id,
                    HistoricalSalesMonthly.date >= min_share_from,
                    HistoricalSalesMonthly.date <= hist_to,
                )
            )
        ).scalars().all()
    for effect_promo in promos_for_effect:
        overlap = _overlap_dates(promo_start, promo_end, effect_promo.promo_start_date, effect_promo.promo_end_date)
        if overlap is None:
            continue
        effect_branches = _filter_branch_names(parse_promo_list(effect_promo.promo_branches), filters)
        effect_branches = _filter_branch_names(effect_branches, normalize_promo_filters(branch_name=scoped_branches))
        effect_products = _filter_products(
            list(products),
            parse_promo_list(effect_promo.promo_sku_codes),
            filters,
        )
        effect_skus = [
            str(product.sku_code).strip()
            for product in effect_products
            if str(product.sku_code).strip() in sku_set
        ]
        if not effect_branches or not effect_skus:
            continue
        shares = _share_weights(
            list(hist_rows),
            products_by_code=products_by_code,
            branch_name_by_id=branch_name_by_id,
            branches=effect_branches,
            sku_codes=effect_skus,
            start=effect_promo.promo_start_date,
        )
        if not shares:
            continue
        full_weights = _promo_month_weights(effect_promo.promo_start_date, effect_promo.promo_end_date)
        overlap_weights = _promo_month_weights(overlap[0], overlap[1])
        full_weight_total = sum(full_weights.values()) or 1.0
        for (branch_name, sku_code), share in shares.items():
            if normalize_branch_lookup(branch_name) not in {normalize_branch_lookup(v) for v in effect_branches}:
                continue
            product = products_by_code.get(sku_code)
            if product is None:
                continue
            for month, month_weight in overlap_weights.items():
                effect_cases = (
                    float(effect_promo.promo_effect_cases or 0.0)
                    * share
                    * (month_weight / full_weight_total)
                )
                price = _closest_price(prices_by_sku, sku_code, month)
                promo_effect += effect_cases * _case_conversion_factor(
                    view_type=normalized_view_type,
                    product=product,
                    price=price,
                )

    fact_out = _round_view_value(fact_value, normalized_view_type)
    baseline_out = _round_view_value(baseline_value, normalized_view_type)
    effect_out = _round_view_value(promo_effect, normalized_view_type)
    return PromoComputedValues(
        fact_value=fact_out,
        baseline_forecast_value=baseline_out,
        promo_effect=effect_out,
        promo_plan_value=_round_view_value(baseline_out + effect_out, normalized_view_type),
    )
