from datetime import date
from io import BytesIO
import math

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.core.branch_localization import normalize_branch_lookup
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product, ProductBranch
from app.models.derived import (
    DistributionBranchAmountAdjustment,
    DistributionSkuAdjustment,
    ForecastSalesMonthly,
)

router = APIRouter(prefix="/distribution", tags=["distribution"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


class DistributionAggregateRow(BaseModel):
    hub_name: str
    branch_name: str
    target_amount_dsp_per_branch: float
    available_amount_kzt_per_branch: float
    recommended_amount_kzt_per_branch: float
    adjusted_amount_kzt_per_branch: float
    readiness_for_target_per_branch: int


class DistributionAggregateResponse(BaseModel):
    planning_date: str
    items: list[DistributionAggregateRow]
    total_items: int
    total_pages: int
    filter_options: "DistributionAggregateFilterOptions"


class DistributionAggregateFilterOptions(BaseModel):
    branch_name: list[str]
    readiness_for_target_per_branch: list[int]


class DistributionSummaryResponse(BaseModel):
    planning_date: str
    total_target_amount_dsp: float
    total_fact_amount_dsp: float


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
    readiness_for_target_per_sku: list[int]


class DistributionDetailsSummaryResponse(BaseModel):
    planning_date: str
    hub_name: str
    branch_name: str
    total_adjusted_volume_cbm_per_branch: float
    total_adjusted_gross_weight_kg_per_branch: float


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


def _pick_dsp_for_sales_date(prices: list[PriceList], sales_date: date) -> float:
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
    return float(selected.dsp or 0.0)


def _safe_readiness(numerator: float, denominator: float) -> int:
    if denominator <= 0:
        return 0
    value = int(round((numerator / denominator) * 100.0))
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


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


async def _build_distribution_calc(
    db: DBSession,
    user: CurrentUser,
) -> tuple[date, list[_BranchSkuCalc], dict[tuple[int, str], str], dict[tuple[int, str], str]]:
    planning_date = await _resolve_planning_date(db, user)

    hs_stmt = select(HistoricalSalesMonthly)
    p_stmt = select(Product)
    b_stmt = select(Branch)
    pr_stmt = select(PriceList)
    fs_stmt = select(ForecastSalesMonthly)
    if not is_admin(user):
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
        b_stmt = b_stmt.where(Branch.owner_user_id == user.id)
        pr_stmt = pr_stmt.where(PriceList.owner_user_id == user.id)
        fs_stmt = fs_stmt.where(ForecastSalesMonthly.owner_user_id == user.id)

    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    product_rows = (await db.execute(p_stmt)).scalars().all()
    branch_rows = (await db.execute(b_stmt)).scalars().all()
    price_rows = (await db.execute(pr_stmt)).scalars().all()
    forecast_rows = (await db.execute(fs_stmt)).scalars().all()

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
    forecast_qty_by_key: dict[tuple[int, str, str, date], float] = {}
    for r in forecast_rows:
        m = _month_start(r.date)
        qty = (
            float(r.adjusted_forecast_quantity_in_mc)
            if r.adjusted_forecast_quantity_in_mc is not None
            else float(r.baseline_forecast_quantity_in_mc or 0.0)
        )
        forecast_key = (r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip(), m)
        forecast_qty_by_key[forecast_key] = forecast_qty_by_key.get(forecast_key, 0.0) + qty
        if m in f3_months:
            f3_by_key.setdefault((r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()), []).append(qty)

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

    # Branch-level stock norm from branch_stock_norm upload table.
    branch_norm_stmt = select(ProductBranch)
    if not is_admin(user):
        branch_norm_stmt = branch_norm_stmt.where(ProductBranch.owner_user_id == user.id)
    branch_norm_rows = (await db.execute(branch_norm_stmt)).scalars().all()
    branch_norm_days_by_key = {
        (r.owner_user_id, str(r.branch_id).strip(), str(r.sku_code or "").strip()): float(r.stock_norm or 0.0)
        for r in branch_norm_rows
    }

    # Unconstrained need per branch+sku and proportional allocation from hub pool.
    need_by_hub_sku: dict[tuple[int, str, str], dict[str, int]] = {}
    stock_norm_target_by_branch_sku: dict[tuple[int, str, str], float] = {}
    for (owner_id, branch_id, sku_code), vals in branch_sku_rows.items():
        stock_norm_days = branch_norm_days_by_key.get(
            (owner_id, branch_id, sku_code),
            float(default_stock_norm_days_by_key.get((owner_id, sku_code), 0.0)),
        )
        stock_norm_target_qty = _forecast_target_for_stock_norm(
            forecast_qty_by_key=forecast_qty_by_key,
            owner_user_id=owner_id,
            branch_id=branch_id,
            sku_code=sku_code,
            planning_date=planning_date,
            stock_norm_days=stock_norm_days,
        )
        stock_norm_target_by_branch_sku[(owner_id, branch_id, sku_code)] = stock_norm_target_qty
        need_qty = max(int(math.ceil(stock_norm_target_qty - float(vals["available_qty"]))), 0)
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
        dsp = _pick_dsp_for_sales_date(prices_by_key.get((owner_id, sku_code), []), row_date)
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
    branch_name: list[str] | None = Query(default=None),
    readiness_for_target_per_branch: list[int] | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> DistributionAggregateResponse:
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

    buckets: dict[tuple[int, str, str, str], dict[str, float]] = {}
    for r in calc_rows:
        key = (r.owner_user_id, r.branch_id, r.branch_name, r.hub_name)
        if key not in buckets:
            buckets[key] = {
                "available_amount": 0.0,
                "recommended_amount": 0.0,
            }
        b = buckets[key]
        b["available_amount"] += float(r.available_qty) * r.pieces_in_master_carton * r.dsp
        b["recommended_amount"] += float(r.recommended_qty) * r.pieces_in_master_carton * r.dsp

    rows: list[DistributionAggregateRow] = []
    for (owner_id, branch_id, branch_name_value, hub_name), vals in buckets.items():
        target_amount = float(vals["available_amount"]) + float(vals["recommended_amount"])
        explicit_adjustment_key = (owner_id, branch_id)
        readiness_adjusted_amount = float(branch_adj_map.get(explicit_adjustment_key, 0.0))
        display_adjusted_amount = (
            readiness_adjusted_amount
            if explicit_adjustment_key in branch_adj_map
            else float(vals["recommended_amount"])
        )
        readiness = _safe_readiness(
            numerator=float(vals["available_amount"]) + readiness_adjusted_amount,
            denominator=target_amount,
        )
        rows.append(
            DistributionAggregateRow(
                hub_name=hub_name,
                branch_name=branch_name_value,
                target_amount_dsp_per_branch=round(target_amount, 2),
                available_amount_kzt_per_branch=round(float(vals["available_amount"]), 2),
                recommended_amount_kzt_per_branch=round(float(vals["recommended_amount"]), 2),
                adjusted_amount_kzt_per_branch=round(display_adjusted_amount, 2),
                readiness_for_target_per_branch=int(readiness),
            )
        )
    filtered_rows = rows
    if branch_name:
        branch_name_values = {
            normalize_branch_lookup(v)
            for v in branch_name
            if str(v).strip()
        }
        filtered_rows = [
            r
            for r in filtered_rows
            if normalize_branch_lookup(r.branch_name) in branch_name_values
        ]
    if readiness_for_target_per_branch:
        readiness_values = {int(v) for v in readiness_for_target_per_branch}
        filtered_rows = [
            r
            for r in filtered_rows
            if int(r.readiness_for_target_per_branch) in readiness_values
        ]

    filter_options = DistributionAggregateFilterOptions(
        branch_name=sorted({r.branch_name for r in filtered_rows}),
        readiness_for_target_per_branch=sorted(
            {int(r.readiness_for_target_per_branch) for r in filtered_rows}
        ),
    )
    filtered_rows.sort(key=lambda x: (x.hub_name, x.branch_name))
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
    total_target = sum(float(r.target_qty) * r.pieces_in_master_carton * r.dsp for r in calc_rows)
    total_fact = sum(float(r.fact_qty) * r.pieces_in_master_carton * r.dsp for r in calc_rows)
    return DistributionSummaryResponse(
        planning_date=planning_date.isoformat(),
        total_target_amount_dsp=round(total_target, 2),
        total_fact_amount_dsp=round(total_fact, 2),
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

    detail_rows: list[DistributionDetailRow] = []
    for r in selected:
        explicit_adjustment_key = (r.owner_user_id, r.branch_id, r.sku_code)
        readiness_adjusted = int(detail_adj_map.get(explicit_adjustment_key, 0))
        recommended_for_target = int(r.recommended_qty)
        display_adjusted = (
            readiness_adjusted
            if explicit_adjustment_key in detail_adj_map
            else recommended_for_target
        )
        readiness = _safe_readiness(
            numerator=float(r.available_qty) + float(readiness_adjusted),
            denominator=float(r.stock_norm_target_qty),
        )
        detail_rows.append(
            DistributionDetailRow(
                sku_code=r.sku_code,
                sku_name=r.sku_name,
                total_available_quantity_in_mc=int(math.ceil(float(r.total_hub_available_qty))),
                available_quantity_in_mc=int(math.ceil(float(r.available_qty))),
                average_l3m_quantity_in_mc=int(r.avg_l3m),
                average_f3m_quantity_in_mc=int(r.avg_f3m),
                recommended_quantity_in_mc=int(recommended_for_target),
                adjusted_quantity_in_mc=int(display_adjusted),
                readiness_for_target_per_sku=int(readiness),
            )
        )

    filtered_rows = detail_rows
    if sku_code_filter:
        sku_code_values = {str(v).strip() for v in sku_code_filter if str(v).strip()}
        filtered_rows = [r for r in filtered_rows if r.sku_code in sku_code_values]
    if sku_name_filter:
        sku_name_values = {str(v).strip() for v in sku_name_filter if str(v).strip()}
        filtered_rows = [r for r in filtered_rows if r.sku_name in sku_name_values]
    if readiness_for_target_per_sku_filter:
        readiness_values = {int(v) for v in readiness_for_target_per_sku_filter}
        filtered_rows = [r for r in filtered_rows if int(r.readiness_for_target_per_sku) in readiness_values]

    filter_options = DistributionDetailsFilterOptions(
        sku_code=sorted({r.sku_code for r in filtered_rows}),
        sku_name=sorted({r.sku_name for r in filtered_rows}),
        readiness_for_target_per_sku=sorted(
            {int(r.readiness_for_target_per_sku) for r in filtered_rows}
        ),
    )

    filtered_rows.sort(key=lambda x: x.sku_code)
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

    total_volume = 0.0
    total_gross_weight = 0.0
    for r in selected_rows:
        adjustment_key = (r.owner_user_id, r.branch_id, r.sku_code)
        adjusted = float(
            detail_adj_map[adjustment_key]
            if adjustment_key in detail_adj_map
            else r.recommended_qty
        )
        total_volume += adjusted * r.master_carton_volume_cbm
        total_gross_weight += adjusted * r.master_carton_gross_weight_kg

    first = selected_rows[0]
    return DistributionDetailsSummaryResponse(
        planning_date=planning_date.isoformat(),
        hub_name=first.hub_name,
        branch_name=first.branch_name,
        total_adjusted_volume_cbm_per_branch=round(total_volume, 2),
        total_adjusted_gross_weight_kg_per_branch=round(total_gross_weight, 2),
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
    return {"rows_updated": updated}


@router.get("/download/", include_in_schema=False)
@router.get("/download")
async def download_distribution(
    db: DBSession,
    user: CurrentUser,
    branch_name: str | None = Query(None),
):
    response = await get_distribution_aggregated(
        db=db,
        user=user,
        branch_name=None,
        readiness_for_target_per_branch=None,
        page=1,
        page_size="all",
    )
    rows = response.items
    if branch_name:
        branch_norm = normalize_branch_lookup(branch_name)
        rows = [r for r in rows if normalize_branch_lookup(r.branch_name) == branch_norm]
    export_rows = [r.model_dump() for r in rows]
    output = BytesIO()
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="distribution")
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
):
    response = await get_distribution_details(
        db=db,
        user=user,
        branch_name=branch_name,
        sku_code_filter=None,
        sku_name_filter=None,
        readiness_for_target_per_sku_filter=None,
        page=1,
        page_size="all",
    )
    rows = response.items
    if sku_code:
        sku_norm = sku_code.strip()
        rows = [r for r in rows if r.sku_code == sku_norm]
    export_rows = [r.model_dump() for r in rows]
    output = BytesIO()
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="distribution_details")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="distribution_details.xlsx"'},
    )

