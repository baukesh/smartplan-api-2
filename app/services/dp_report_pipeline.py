from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import logging
import time
from typing import Callable

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.order_status import normalize_order_status
from app.models.data_uploads import (
    HistoricalSalesMonthly,
    PlacedOrder,
    PriceList,
    Product,
    ProductBranch,
)
from app.models.derived import (
    BranchDistribution,
    DPReportMart,
    ForecastInferenceCache,
    ForecastOrders,
    ForecastSalesMonthly,
    InventoryHealth,
)
from app.services.gpt_forecasting import (
    forecast_baseline_quantities_in_mc,
    forecast_fast_baseline_quantities_in_mc,
)

logger = logging.getLogger(__name__)


def _closest_price_on_or_before(
    sku_prices: list[PriceList],
    target_date: date,
) -> PriceList | None:
    best: PriceList | None = None
    for p in sku_prices:
        p_date = p.date.date() if isinstance(p.date, datetime) else p.date
        best_date = (
            best.date.date() if (best is not None and isinstance(best.date, datetime)) else (best.date if best is not None else None)
        )
        if p_date <= target_date:
            if best is None or p_date > best_date:
                best = p
    return best


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _prev_month(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def _month_start(d: date | datetime) -> date:
    value = d.date() if isinstance(d, datetime) else d
    return date(value.year, value.month, 1)


def _round2(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v), 2)


def _round_qty(v: float | None) -> int:
    if v is None:
        return 0
    return int(round(float(v)))


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _avg_last_n(values: list[float], n: int = 6) -> float:
    if not values:
        return 0.0
    tail = values[-n:] if len(values) >= n else values
    return sum(tail) / len(tail) if tail else 0.0


def _aggregate_order_arrivals_by_sku_month(
    placed_orders: list[PlacedOrder],
) -> dict[tuple[str, date], float]:
    arrivals: dict[tuple[str, date], float] = defaultdict(float)
    for order in placed_orders:
        if normalize_order_status(order.status) != "в пути":
            continue
        sku_code = str(order.sku_code or order.sku_id or "").strip()
        if not sku_code:
            continue
        arrivals[(sku_code, _month_start(order.receival_date))] += float(
            order.quantity_in_mc or 0.0
        )
    return arrivals


def _allocate_order_arrivals_by_branch(
    *,
    job_payloads: list[dict],
    baseline_series_by_key: dict[tuple[str, str], list[float]],
    arrivals_by_sku_month: dict[tuple[str, date], float],
) -> dict[tuple[str, str, date], float]:
    demand_rows_by_sku_month: dict[tuple[str, date], list[tuple[str, float]]] = defaultdict(list)

    for job in job_payloads:
        sku_code = str(job["sku_code"]).strip()
        branch_id = str(job["branch_id"]).strip()
        baseline_series = baseline_series_by_key.get((sku_code, branch_id), [])
        for idx, forecast_date in enumerate(job["forecast_months"]):
            month = _month_start(forecast_date)
            if arrivals_by_sku_month.get((sku_code, month), 0.0) <= 0:
                continue
            demand = float(baseline_series[idx]) if idx < len(baseline_series) else 0.0
            demand_rows_by_sku_month[(sku_code, month)].append((branch_id, max(demand, 0.0)))

    allocations: dict[tuple[str, str, date], float] = defaultdict(float)
    for (sku_code, month), branch_demands in demand_rows_by_sku_month.items():
        incoming_qty = float(arrivals_by_sku_month.get((sku_code, month), 0.0) or 0.0)
        if incoming_qty <= 0 or not branch_demands:
            continue
        total_demand = sum(demand for _branch_id, demand in branch_demands)
        if total_demand > 0:
            for branch_id, demand in branch_demands:
                allocations[(sku_code, branch_id, month)] += incoming_qty * demand / total_demand
        else:
            even_share = incoming_qty / len(branch_demands)
            for branch_id, _demand in branch_demands:
                allocations[(sku_code, branch_id, month)] += even_share
    return allocations


def _normalize_changed_keys(
    changed_keys: list[tuple[str, str]] | None,
) -> list[tuple[str, str]] | None:
    if not changed_keys:
        return None
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sku_code, branch_id in changed_keys:
        key = (str(sku_code).strip(), str(branch_id).strip())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized or None


def _normalize_changed_skus(
    changed_skus: list[str] | None,
) -> list[str] | None:
    if not changed_skus:
        return None
    normalized = sorted({str(s).strip() for s in changed_skus if str(s).strip()})
    return normalized or None


async def refresh_forecast_sales_monthly(
    db: AsyncSession,
    owner_user_id: int | None = None,
    changed_keys: list[tuple[str, str]] | None = None,
    forecast_source: str = "gpt",
) -> dict[str, int]:
    changed_keys = _normalize_changed_keys(changed_keys)
    if forecast_source not in {"fast", "gpt"}:
        raise ValueError("forecast_source must be either 'fast' or 'gpt'")
    products = {
        str(p.sku_code).strip(): p
        for p in (
            await db.execute(
                select(Product).where(Product.owner_user_id == owner_user_id)
            )
        ).scalars().all()
    }
    branch_rows = (
        await db.execute(
            select(ProductBranch).where(ProductBranch.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    hs_stmt = select(HistoricalSalesMonthly).where(
        HistoricalSalesMonthly.owner_user_id == owner_user_id
    )
    if changed_keys:
        hs_stmt = hs_stmt.where(
            tuple_(HistoricalSalesMonthly.sku_code, HistoricalSalesMonthly.branch_id).in_(changed_keys)
        )
    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    placed_orders = (
        await db.execute(
            select(PlacedOrder).where(PlacedOrder.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    in_transit_orders = [
        order for order in placed_orders if normalize_order_status(order.status) == "в пути"
    ]

    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for p in prices:
        prices_by_sku[str(p.sku_code or "").strip()].append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    hist_by_key: dict[tuple[str, str], list[HistoricalSalesMonthly]] = defaultdict(list)
    for row in hist_rows:
        hist_by_key[(str(row.sku_code or "").strip(), row.branch_id)].append(row)
    for key in hist_by_key:
        hist_by_key[key].sort(key=lambda x: x.date)

    branch_stock = {
        (str(b.sku_code or "").strip(), b.branch_id): b.current_stock for b in branch_rows
    }
    stock_norm_by_key = {
        (str(b.sku_code or "").strip(), b.branch_id): b.stock_norm for b in branch_rows
    }

    placed_orders_by_sku: dict[str, list[PlacedOrder]] = defaultdict(list)
    for po in in_transit_orders:
        placed_orders_by_sku[str(po.sku_code or "").strip()].append(po)
    for sku in placed_orders_by_sku:
        placed_orders_by_sku[sku].sort(key=lambda x: x.creation_date)

    if changed_keys:
        await db.execute(
            delete(ForecastSalesMonthly).where(
                ForecastSalesMonthly.owner_user_id == owner_user_id,
                tuple_(ForecastSalesMonthly.sku_code, ForecastSalesMonthly.branch_id).in_(changed_keys),
            )
        )
    else:
        await db.execute(
            delete(ForecastSalesMonthly).where(
                ForecastSalesMonthly.owner_user_id == owner_user_id
            )
        )

    to_insert: list[ForecastSalesMonthly] = []

    job_payloads: list[dict] = []
    for key, rows in hist_by_key.items():
        sku_code, branch_id = key
        product = products.get(sku_code)
        if not product:
            continue
        if not rows:
            continue

        max_hist_date = rows[-1].date
        forecast_months: list[date] = []
        forecast_date = _next_month(max_hist_date)
        for _ in range(12):
            forecast_months.append(forecast_date)
            forecast_date = _next_month(forecast_date)

        history_context = [
            {
                "date": r.date.isoformat(),
                "fact_quantity_in_mc": float(r.fact_quantity_in_mc or 0.0),
            }
            for r in rows
        ]
        orders_context = [
            {
                "order_id": po.order_id,
                "creation_date": po.creation_date.isoformat(),
                "receival_date": po.receival_date.isoformat(),
                "quantity_in_mc": float(po.quantity_in_mc or 0.0),
                "status": po.status,
            }
            for po in placed_orders_by_sku.get(sku_code, [])
        ]
        cache_payload = {
            "sku_code": sku_code,
            "branch_id": branch_id,
            "forecast_months": [d.isoformat() for d in forecast_months],
            "history_context": history_context[-24:],
            "forecast_prompt_version": "historical_sales_only_v3",
            "model": settings.OPENAI_FORECAST_MODEL,
            "schema_version": settings.FORECAST_CACHE_SCHEMA_VERSION,
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        job_payloads.append(
            {
                "sku_code": sku_code,
                "branch_id": branch_id,
                "product": product,
                "rows": rows,
                "forecast_months": forecast_months,
                "history_context": history_context,
                "orders_context": orders_context,
                "current_stock": float(branch_stock.get((sku_code, branch_id), 0.0)),
                "stock_norm_days": float(
                    stock_norm_by_key.get((sku_code, branch_id), product.general_stock_norm_days)
                    or 0.0
                ),
                "cache_key": cache_key,
            }
        )

    max_concurrency = max(int(settings.OPENAI_FORECAST_MAX_CONCURRENCY or 1), 1)
    timeout_seconds = float(settings.OPENAI_FORECAST_TIMEOUT_SECONDS or 15.0)
    semaphore = asyncio.Semaphore(max_concurrency)
    memo: dict[str, list[float]] = {}
    now_utc = datetime.now(UTC)
    gpt_attempted = 0
    gpt_fallbacks = 0
    cache_hits = 0
    cache_writes = 0
    cache_ttl = timedelta(hours=max(int(settings.FORECAST_CACHE_TTL_HOURS or 1), 1))

    cache_rows_by_hash: dict[str, ForecastInferenceCache] = {}
    if forecast_source == "gpt" and settings.PERSISTENT_FORECAST_CACHE_ENABLED and job_payloads:
        cache_keys = [str(job["cache_key"]) for job in job_payloads]
        cache_rows = (
            await db.execute(
                select(ForecastInferenceCache).where(
                    ForecastInferenceCache.owner_user_id == (owner_user_id or 0),
                    ForecastInferenceCache.model_name == settings.OPENAI_FORECAST_MODEL,
                    ForecastInferenceCache.schema_version == settings.FORECAST_CACHE_SCHEMA_VERSION,
                    ForecastInferenceCache.payload_hash.in_(cache_keys),
                )
            )
        ).scalars().all()
        cache_rows_by_hash = {row.payload_hash: row for row in cache_rows}

    cache_upserts: dict[str, tuple[str, str, str, list[float]]] = {}

    async def _get_baseline_series(job: dict) -> list[float]:
        nonlocal gpt_attempted, gpt_fallbacks, cache_hits
        if forecast_source == "fast":
            gpt_fallbacks += 1
            return forecast_fast_baseline_quantities_in_mc(
                sku_code=job["sku_code"],
                branch_id=job["branch_id"],
                forecast_months=job["forecast_months"],
                history=job["history_context"],
            )

        history_values = [
            float(x.get("fact_quantity_in_mc") or 0.0)
            for x in job["history_context"]
        ]
        avg_baseline = _avg_last_n(history_values, n=6)
        fallback_series = [avg_baseline for _ in job["forecast_months"]]

        # Skip model call when history is too short; use deterministic fallback.
        if len(history_values) < 6:
            gpt_fallbacks += 1
            return fallback_series

        cache_key = str(job["cache_key"])
        if cache_key in memo:
            return memo[cache_key]

        if settings.PERSISTENT_FORECAST_CACHE_ENABLED:
            cache_row = cache_rows_by_hash.get(cache_key)
            if cache_row and _as_aware_utc(cache_row.expires_at) >= now_utc:
                try:
                    persisted = json.loads(cache_row.forecast_values_json)
                    if isinstance(persisted, list):
                        cache_hits += 1
                        memo[cache_key] = [float(v) for v in persisted]
                        return memo[cache_key]
                except Exception:
                    pass

        async with semaphore:
            gpt_attempted += 1
            series = await forecast_baseline_quantities_in_mc(
                sku_code=job["sku_code"],
                branch_id=job["branch_id"],
                forecast_months=job["forecast_months"],
                history=job["history_context"],
                current_stock=job["current_stock"],
                stock_norm_days=job["stock_norm_days"],
                placed_orders_history=job["orders_context"],
                timeout_seconds=timeout_seconds,
            )
        memo[cache_key] = series
        if settings.PERSISTENT_FORECAST_CACHE_ENABLED:
            cache_upserts[cache_key] = (
                str(job["product"].sku_id),
                str(job["sku_code"]),
                job["branch_id"],
                series,
            )
        return series

    baseline_series_by_key: dict[tuple[str, str], list[float]] = {}
    if job_payloads:
        series_results = await asyncio.gather(
            *[_get_baseline_series(job) for job in job_payloads]
        )
        for idx, job in enumerate(job_payloads):
            baseline_series_by_key[(job["sku_code"], job["branch_id"])] = series_results[idx]

    if forecast_source == "gpt" and settings.PERSISTENT_FORECAST_CACHE_ENABLED and cache_upserts:
        for payload_hash, (sku_id, sku_code, branch_id, series) in cache_upserts.items():
            cache_row = cache_rows_by_hash.get(payload_hash)
            if cache_row is None:
                db.add(
                    ForecastInferenceCache(
                        owner_user_id=owner_user_id or 0,
                        sku_id=sku_id,
                        sku_code=sku_code,
                        branch_id=branch_id,
                        model_name=settings.OPENAI_FORECAST_MODEL,
                        schema_version=settings.FORECAST_CACHE_SCHEMA_VERSION,
                        payload_hash=payload_hash,
                        forecast_values_json=json.dumps(series),
                        expires_at=now_utc + cache_ttl,
                    )
                )
            else:
                cache_row.forecast_values_json = json.dumps(series)
                cache_row.expires_at = now_utc + cache_ttl
        cache_writes = len(cache_upserts)

    for job in job_payloads:
        sku_code = job["sku_code"]
        branch_id = job["branch_id"]
        product = job["product"]
        prev_stock = float(job["current_stock"])
        forecast_months = job["forecast_months"]
        baseline_series = baseline_series_by_key.get((sku_code, branch_id), [])

        for idx, forecast_date in enumerate(forecast_months):
            baseline_qty = float(baseline_series[idx]) if idx < len(baseline_series) else 0.0
            forecast_case_qty = _round2(baseline_qty) or 0.0
            closest_dsp = _closest_price_on_or_before(
                prices_by_sku.get(sku_code, []), forecast_date
            )
            baseline_amount = None
            if closest_dsp is not None:
                baseline_amount = (
                    forecast_case_qty * product.pieces_in_master_carton * closest_dsp.dsp
                )
            future_stock = max(prev_stock - forecast_case_qty, 0.0)
            to_insert.append(
                ForecastSalesMonthly(
                    sku_id=product.sku_id,
                    sku_code=sku_code,
                    branch_id=branch_id,
                    date=forecast_date,
                    baseline_forecast_quantity_in_mc=forecast_case_qty,
                    baseline_forecast_gross_weight_kg=(
                        _round2(forecast_case_qty * product.master_carton_gross_weight_kg)
                    ),
                    baseline_forecast_volume_cbm=(
                        _round2(forecast_case_qty * product.master_carton_volume_cbm)
                    ),
                    baseline_forecast_amount_kzt=_round2(baseline_amount),
                    adjusted_forecast_quantity_in_mc=None,
                    adjusted_forecast_gross_weight_kg=None,
                    adjusted_forecast_volume_cbm=None,
                    adjusted_forecast_amount_kzt=None,
                    future_available_stock=_round2(future_stock) or 0.0,
                    owner_user_id=owner_user_id or 0,
                )
            )
            prev_stock = future_stock

    db.add_all(to_insert)
    await db.commit()
    logger.info(
        "refresh_forecast_sales_monthly done owner=%s source=%s series=%s gpt_attempted=%s fallback=%s cache_hits=%s cache_writes=%s incremental=%s",
        owner_user_id,
        forecast_source,
        len(job_payloads),
        gpt_attempted,
        gpt_fallbacks,
        cache_hits,
        cache_writes,
        bool(changed_keys),
    )
    return {
        "series_processed": len(job_payloads),
        "gpt_attempted": gpt_attempted,
        "gpt_fallbacks": gpt_fallbacks,
        "cache_hits": cache_hits,
        "cache_writes": cache_writes,
        "forecast_source": forecast_source,
    }


async def refresh_dp_report_mart(
    db: AsyncSession,
    owner_user_id: int | None = None,
    changed_skus: list[str] | None = None,
) -> None:
    changed_skus = _normalize_changed_skus(changed_skus)
    hs_stmt = select(HistoricalSalesMonthly).where(
        HistoricalSalesMonthly.owner_user_id == owner_user_id
    )
    fc_stmt = select(ForecastSalesMonthly).where(
        ForecastSalesMonthly.owner_user_id == owner_user_id
    )
    if changed_skus:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.sku_code.in_(changed_skus))
        fc_stmt = fc_stmt.where(ForecastSalesMonthly.sku_code.in_(changed_skus))
        await db.execute(
            delete(DPReportMart).where(
                DPReportMart.owner_user_id == owner_user_id,
                DPReportMart.sku_code.in_(changed_skus),
            )
        )
    else:
        await db.execute(delete(DPReportMart).where(DPReportMart.owner_user_id == owner_user_id))
    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    fc_rows = (await db.execute(fc_stmt)).scalars().all()

    to_insert: list[DPReportMart] = []

    for r in hist_rows:
        to_insert.append(
            DPReportMart(
                sku_id=r.sku_id,
                sku_code=r.sku_code,
                date=r.date,
                branch_id=r.branch_id,
                fact_quantity_in_mc=r.fact_quantity_in_mc,
                fact_gross_weight_kg=r.fact_gross_weight_kg,
                fact_volume_cbm=r.fact_volume_cbm,
                fact_amount_kzt=r.fact_amount_kzt,
                target_quantity_in_mc=r.target_quantity_in_mc,
                target_gross_weight_kg=r.target_gross_weight_kg,
                target_volume_cbm=r.target_volume_cbm,
                target_amount_kzt=r.target_amount_kzt,
                past_available_stock=r.past_available_stock,
                baseline_forecast_quantity_in_mc=None,
                baseline_forecast_gross_weight_kg=None,
                baseline_forecast_volume_cbm=None,
                baseline_forecast_amount_kzt=None,
                adjusted_forecast_quantity_in_mc=None,
                adjusted_forecast_gross_weight_kg=None,
                adjusted_forecast_volume_cbm=None,
                adjusted_forecast_amount_kzt=None,
                future_available_stock=None,
                    owner_user_id=owner_user_id or 0,
            )
        )

    for r in fc_rows:
        to_insert.append(
            DPReportMart(
                sku_id=r.sku_id,
                sku_code=r.sku_code,
                date=r.date,
                branch_id=r.branch_id,
                fact_quantity_in_mc=None,
                fact_gross_weight_kg=None,
                fact_volume_cbm=None,
                fact_amount_kzt=None,
                target_quantity_in_mc=None,
                target_gross_weight_kg=None,
                target_volume_cbm=None,
                target_amount_kzt=None,
                past_available_stock=None,
                baseline_forecast_quantity_in_mc=r.baseline_forecast_quantity_in_mc,
                baseline_forecast_gross_weight_kg=r.baseline_forecast_gross_weight_kg,
                baseline_forecast_volume_cbm=r.baseline_forecast_volume_cbm,
                baseline_forecast_amount_kzt=r.baseline_forecast_amount_kzt,
                adjusted_forecast_quantity_in_mc=r.adjusted_forecast_quantity_in_mc,
                adjusted_forecast_gross_weight_kg=r.adjusted_forecast_gross_weight_kg,
                adjusted_forecast_volume_cbm=r.adjusted_forecast_volume_cbm,
                adjusted_forecast_amount_kzt=r.adjusted_forecast_amount_kzt,
                future_available_stock=r.future_available_stock,
                    owner_user_id=owner_user_id or 0,
            )
        )

    db.add_all(to_insert)
    await db.commit()


async def refresh_forecast_orders(
    db: AsyncSession,
    owner_user_id: int | None = None,
    changed_skus: list[str] | None = None,
) -> None:
    changed_skus = _normalize_changed_skus(changed_skus)
    fc_stmt = select(ForecastSalesMonthly).where(
        ForecastSalesMonthly.owner_user_id == owner_user_id
    )
    if changed_skus:
        fc_stmt = fc_stmt.where(ForecastSalesMonthly.sku_code.in_(changed_skus))
    fc_rows = (await db.execute(fc_stmt)).scalars().all()
    pb_rows = (
        await db.execute(
            select(ProductBranch).where(ProductBranch.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    hs_stmt = select(HistoricalSalesMonthly).where(
        HistoricalSalesMonthly.owner_user_id == owner_user_id
    )
    if changed_skus:
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.sku_code.in_(changed_skus))
    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    po_stmt = select(PlacedOrder).where(PlacedOrder.owner_user_id == owner_user_id)
    if changed_skus:
        po_stmt = po_stmt.where(PlacedOrder.sku_code.in_(changed_skus))
    placed_orders = (await db.execute(po_stmt)).scalars().all()

    pb_norm = {(str(r.sku_code or "").strip(), r.branch_id): float(r.stock_norm) for r in pb_rows}
    arrivals_by_sku_month = _aggregate_order_arrivals_by_sku_month(placed_orders)

    by_sku_date_branch: dict[str, dict[date, dict[str, ForecastSalesMonthly]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for row in fc_rows:
        by_sku_date_branch[str(row.sku_code or "").strip()][row.date][row.branch_id] = row
    hist_qty_total_by_sku_date: dict[tuple[str, date], float] = defaultdict(float)
    for row in hist_rows:
        hist_qty_total_by_sku_date[(str(row.sku_code or "").strip(), row.date)] += float(
            row.fact_quantity_in_mc or 0.0
        )
    fc_qty_total_by_sku_date: dict[tuple[str, date], float] = defaultdict(float)
    for row in fc_rows:
        fc_qty_total_by_sku_date[(str(row.sku_code or "").strip(), row.date)] += float(
            row.adjusted_forecast_quantity_in_mc
            if row.adjusted_forecast_quantity_in_mc is not None
            else row.baseline_forecast_quantity_in_mc
        )

    if changed_skus:
        await db.execute(
            delete(ForecastOrders).where(
                ForecastOrders.owner_user_id == owner_user_id,
                ForecastOrders.sku_code.in_(changed_skus),
            )
        )
    else:
        await db.execute(
            delete(ForecastOrders).where(ForecastOrders.owner_user_id == owner_user_id)
        )
    inserts: list[ForecastOrders] = []

    for sku_code, date_map in by_sku_date_branch.items():
        dates = sorted(date_map.keys())
        for idx, d in enumerate(dates):
            prev_d = _prev_month(d)
            month_prior_stock = 0.0
            if prev_d in date_map:
                month_prior_stock = sum(
                    r.future_available_stock for r in date_map[prev_d].values()
                )
            current_month_arrival = float(arrivals_by_sku_month.get((sku_code, d), 0.0) or 0.0)
            month_prior_stock += current_month_arrival

            f3_slice = dates[idx : idx + 3]

            l3_vals: list[float] = []
            # Hybrid rolling window for L3M: current planning month + previous two months.
            # Per month, prefer historical fact; otherwise fallback to forecast.
            l3_months = [d, _prev_month(d), _prev_month(_prev_month(d))]
            for dd in l3_months:
                hist_total = hist_qty_total_by_sku_date.get((sku_code, dd))
                if hist_total is not None:
                    l3_vals.append(float(hist_total))
                    continue
                fc_total = fc_qty_total_by_sku_date.get((sku_code, dd))
                if fc_total is not None:
                    l3_vals.append(float(fc_total))
                    continue
                l3_vals.append(0.0)
            f3_month_totals: list[float] = []
            branch_f3_vals: dict[str, list[float]] = defaultdict(list)
            for dd in f3_slice:
                month_total = 0.0
                for branch_id, row in date_map[dd].items():
                    qty = float(
                        row.adjusted_forecast_quantity_in_mc
                        if row.adjusted_forecast_quantity_in_mc is not None
                        else row.baseline_forecast_quantity_in_mc
                        or 0.0
                    )
                    month_total += qty
                    branch_f3_vals[branch_id].append(qty)
                f3_month_totals.append(month_total)
            avg_l3 = sum(l3_vals) / len(l3_vals) if l3_vals else 0.0
            avg_f3 = sum(f3_month_totals) / len(f3_month_totals) if f3_month_totals else 0.0

            rec_total = 0.0
            prev_branch_rows = date_map.get(prev_d, {})
            current_branch_rows = date_map.get(d, {})
            current_month_arrival_by_branch: dict[str, float] = {}
            if current_month_arrival > 0 and current_branch_rows:
                branch_demands = {
                    branch_id: float(
                        row.adjusted_forecast_quantity_in_mc
                        if row.adjusted_forecast_quantity_in_mc is not None
                        else row.baseline_forecast_quantity_in_mc
                        or 0.0
                    )
                    for branch_id, row in current_branch_rows.items()
                }
                total_branch_demand = sum(max(v, 0.0) for v in branch_demands.values())
                if total_branch_demand > 0:
                    current_month_arrival_by_branch = {
                        branch_id: current_month_arrival * max(demand, 0.0) / total_branch_demand
                        for branch_id, demand in branch_demands.items()
                    }
                else:
                    even_share = current_month_arrival / len(current_branch_rows)
                    current_month_arrival_by_branch = {
                        branch_id: even_share for branch_id in current_branch_rows
                    }
            for branch_id in date_map[d].keys():
                prior_stock_b = (
                    prev_branch_rows[branch_id].future_available_stock
                    if branch_id in prev_branch_rows
                    else 0.0
                )
                prior_stock_b += current_month_arrival_by_branch.get(branch_id, 0.0)
                stock_norm = pb_norm.get((sku_code, branch_id), 0.0)
                branch_vals = branch_f3_vals.get(branch_id, [])
                branch_avg_f3 = (
                    sum(branch_vals) / len(branch_vals)
                    if branch_vals
                    else 0.0
                )
                needed = stock_norm * (branch_avg_f3 / 30.0)
                rec_total += max(needed - prior_stock_b, 0.0)

            sku_id = next(iter(date_map[d].values())).sku_id if date_map[d] else ""
            inserts.append(
                ForecastOrders(
                    sku_id=sku_id,
                    sku_code=sku_code,
                    date=d,
                    month_prior_available_stock=_round2(month_prior_stock) or 0.0,
                    average_l3m_quantity_in_mc=float(_round_qty(avg_l3)),
                    average_f3m_quantity_in_mc=float(_round_qty(avg_f3)),
                    recommended_quantity_in_mc=float(_round_qty(rec_total)),
                    adjusted_quantity_in_mc=None,
                    owner_user_id=owner_user_id or 0,
                )
            )

    db.add_all(inserts)
    await db.commit()


async def refresh_inventory_health(
    db: AsyncSession, owner_user_id: int | None = None
) -> None:
    products = {
        str(p.sku_code).strip(): p
        for p in (
            await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
        ).scalars().all()
    }
    pb_rows = (
        await db.execute(
            select(ProductBranch).where(ProductBranch.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
    fc_rows = (
        await db.execute(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()

    pb_map = {(str(r.sku_code or "").strip(), r.branch_id): r for r in pb_rows}
    hist_map = {(str(r.sku_code or "").strip(), r.branch_id, r.date): r for r in hist_rows}
    fc_map = {(str(r.sku_code or "").strip(), r.branch_id, r.date): r for r in fc_rows}

    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for p in prices:
        prices_by_sku[str(p.sku_code or "").strip()].append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    key_dates: dict[tuple[str, str], set[date]] = defaultdict(set)
    for r in hist_rows:
        key_dates[(str(r.sku_code or "").strip(), r.branch_id)].add(r.date)
    for r in fc_rows:
        key_dates[(str(r.sku_code or "").strip(), r.branch_id)].add(r.date)

    total_sales_by_date: dict[date, float] = defaultdict(float)
    for r in hist_rows:
        total_sales_by_date[r.date] += r.fact_quantity_in_mc

    await db.execute(
        delete(InventoryHealth).where(InventoryHealth.owner_user_id == owner_user_id)
    )
    inserts: list[InventoryHealth] = []
    temp_rows: list[dict] = []

    for (sku_code, branch_id), dates in key_dates.items():
        product = products.get(sku_code)
        if not product:
            continue
        pb = pb_map.get((sku_code, branch_id))
        fc_for_key = sorted(
            [r for r in fc_rows if str(r.sku_code or "").strip() == sku_code and r.branch_id == branch_id],
            key=lambda x: x.date,
        )
        fc_dates = [r.date for r in fc_for_key]
        for d in sorted(dates):
            hist = hist_map.get((sku_code, branch_id, d))
            fc = fc_map.get((sku_code, branch_id, d))

            sales_qty = float(hist.fact_quantity_in_mc) if hist else 0.0
            closest_price = _closest_price_on_or_before(prices_by_sku.get(sku_code, []), d)
            dsp = float(closest_price.dsp) if closest_price else 0.0

            if hist:
                available_stock = float(hist.past_available_stock)
            elif fc:
                available_stock = float(fc.future_available_stock)
            else:
                available_stock = float(pb.current_stock) if pb else 0.0

            f3_vals: list[float] = []
            if fc_dates:
                # take current month and next two forecast points
                future_dates = [dd for dd in fc_dates if dd >= d][:3]
                by_date = {r.date: r for r in fc_for_key}
                for fd in future_dates:
                    f3_vals.append(float(by_date[fd].baseline_forecast_quantity_in_mc))
            avg_f3 = sum(f3_vals) / len(f3_vals) if f3_vals else 0.0
            stock_norm = float(pb.stock_norm) if pb else float(product.general_stock_norm_days)
            available_stock_days = (available_stock / (avg_f3 / 90.0)) if avg_f3 > 0 else 0.0
            overstock = (
                max((available_stock_days - stock_norm) / stock_norm * 100.0, 0.0)
                if stock_norm > 0
                else 0.0
            )
            understock = (
                max((stock_norm - available_stock_days) / stock_norm * 100.0, 0.0)
                if stock_norm > 0
                else 0.0
            )
            stock_out = 1.0 if available_stock <= 0 else 0.0

            total_sales_share = (
                sales_qty / total_sales_by_date[d] if total_sales_by_date[d] > 0 else 0.0
            )
            temp_rows.append(
                {
                    "sku_id": product.sku_id,
                    "sku_code": sku_code,
                    "branch_id": branch_id,
                    "date": d,
                    "sales_qty": sales_qty,
                    "sales_gross": sales_qty * product.master_carton_gross_weight_kg,
                    "sales_volume": sales_qty * product.master_carton_volume_cbm,
                    "sales_amount": sales_qty * product.pieces_in_master_carton * dsp,
                    "total_sales_share": total_sales_share,
                    "available_stock": available_stock,
                    "avg_f3": avg_f3,
                    "dsp": dsp,
                    "available_stock_days": available_stock_days,
                    "stock_norm_days": stock_norm,
                    "overstock": overstock,
                    "understock": understock,
                    "stock_out": stock_out,
                }
            )

    sku_totals: dict[str, float] = defaultdict(float)
    for r in temp_rows:
        sku_totals[r["sku_code"]] += r["sales_qty"]
    total_stock_by_date: dict[date, float] = defaultdict(float)
    for r in temp_rows:
        total_stock_by_date[r["date"]] += r["available_stock"]
    grand_total = sum(sku_totals.values())
    sorted_skus = sorted(sku_totals.items(), key=lambda x: x[1], reverse=True)
    categories: dict[str, str] = {}
    cum = 0.0
    for sku, qty in sorted_skus:
        frac = qty / grand_total if grand_total > 0 else 0.0
        cum += frac
        if cum <= 0.80:
            categories[sku] = "A"
        elif cum <= 0.95:
            categories[sku] = "B"
        else:
            categories[sku] = "C"

    for r in temp_rows:
        share_business = r["total_sales_share"]
        stock_total_for_date = total_stock_by_date.get(r["date"], 0.0)
        share_stock = (
            (r["available_stock"] / stock_total_for_date) if stock_total_for_date > 0 else 0.0
        )
        health_index = (share_stock / share_business * 100.0) if share_business > 0 else 0.0
        inserts.append(
            InventoryHealth(
                sku_id=r["sku_id"],
                sku_code=r["sku_code"],
                branch_id=r["branch_id"],
                date=r["date"],
                sales_quantity_in_mc=_round2(r["sales_qty"]) or 0.0,
                sales_gross_weight_kg=_round2(r["sales_gross"]) or 0.0,
                sales_volume_cbm=_round2(r["sales_volume"]) or 0.0,
                sales_amount_kzt=_round2(r["sales_amount"]) or 0.0,
                total_sales_share=_round2(r["total_sales_share"]) or 0.0,
                available_stock=_round2(r["available_stock"]) or 0.0,
                average_f3m_quantity_in_mc=_round2(r["avg_f3"]) or 0.0,
                dsp=_round2(r["dsp"]) or 0.0,
                available_stock_days=_round2(r["available_stock_days"]) or 0.0,
                stock_norm_days=_round2(r["stock_norm_days"]) or 0.0,
                overstock=_round2(r["overstock"]) or 0.0,
                understock=_round2(r["understock"]) or 0.0,
                stock_out=_round2(r["stock_out"]) or 0.0,
                category=categories.get(r["sku_code"], "C"),
                health_index=_round2(health_index) or 0.0,
                owner_user_id=owner_user_id or 0,
            )
        )

    db.add_all(inserts)
    await db.commit()


async def refresh_branch_distribution(
    db: AsyncSession, owner_user_id: int | None = None
) -> None:
    products = {
        str(p.sku_code).strip(): p
        for p in (
            await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
        ).scalars().all()
    }
    pb_rows = (
        await db.execute(
            select(ProductBranch).where(ProductBranch.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    fo_rows = (
        await db.execute(
            select(ForecastOrders).where(ForecastOrders.owner_user_id == owner_user_id)
        )
    ).scalars().all()
    ih_rows = (
        await db.execute(
            select(InventoryHealth).where(InventoryHealth.owner_user_id == owner_user_id)
        )
    ).scalars().all()

    latest_dsp_by_sku: dict[str, float] = {}
    for p in sorted(prices, key=lambda x: x.date):
        latest_dsp_by_sku[str(p.sku_code or "").strip()] = p.dsp

    latest_fo_by_sku: dict[str, ForecastOrders] = {}
    for row in sorted(fo_rows, key=lambda x: x.date):
        latest_fo_by_sku[str(row.sku_code or "").strip()] = row

    branch_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "available_quantity_in_mc": 0.0,
            "available_volume_cbm": 0.0,
            "available_gross_weight_kg": 0.0,
            "available_amount_kzt": 0.0,
            "recommended_quantity_in_mc": 0.0,
            "recommended_volume_cbm": 0.0,
            "recommended_gross_weight_kg": 0.0,
            "recommended_amount_kzt": 0.0,
        }
    )

    pb_by_sku: dict[str, list[ProductBranch]] = defaultdict(list)
    for pb in pb_rows:
        sku_code = str(pb.sku_code or "").strip()
        pb_by_sku[sku_code].append(pb)
        product = products.get(sku_code)
        if not product:
            continue
        dsp = latest_dsp_by_sku.get(sku_code, 0.0)
        b = branch_buckets[pb.branch_id]
        b["available_quantity_in_mc"] += pb.current_stock
        b["available_volume_cbm"] += pb.current_stock * product.master_carton_volume_cbm
        b["available_gross_weight_kg"] += pb.current_stock * product.master_carton_gross_weight_kg
        b["available_amount_kzt"] += pb.current_stock * product.pieces_in_master_carton * dsp

    for sku_code, fo in latest_fo_by_sku.items():
        branches = pb_by_sku.get(sku_code, [])
        if not branches:
            continue
        product = products.get(sku_code)
        if not product:
            continue
        total_norm = sum(b.stock_norm for b in branches)
        if total_norm <= 0:
            total_norm = float(len(branches))
        qty_total = fo.adjusted_quantity_in_mc or fo.recommended_quantity_in_mc
        dsp = latest_dsp_by_sku.get(sku_code, 0.0)
        for b in branches:
            share = (b.stock_norm / total_norm) if total_norm else 0.0
            qty = qty_total * share
            bucket = branch_buckets[b.branch_id]
            bucket["recommended_quantity_in_mc"] += qty
            bucket["recommended_volume_cbm"] += qty * product.master_carton_volume_cbm
            bucket["recommended_gross_weight_kg"] += qty * product.master_carton_gross_weight_kg
            bucket["recommended_amount_kzt"] += qty * product.pieces_in_master_carton * dsp

    latest_ih_by_branch: dict[str, list[InventoryHealth]] = defaultdict(list)
    if ih_rows:
        max_date = max(r.date for r in ih_rows)
        for r in ih_rows:
            if r.date == max_date:
                latest_ih_by_branch[r.branch_id].append(r)

    await db.execute(
        delete(BranchDistribution).where(BranchDistribution.owner_user_id == owner_user_id)
    )
    inserts: list[BranchDistribution] = []
    for branch_id, values in branch_buckets.items():
        rows = latest_ih_by_branch.get(branch_id, [])
        avg_health_index = (
            sum(r.health_index for r in rows) / len(rows) if rows else 0.0
        )

        inserts.append(
            BranchDistribution(
                branch_id=branch_id,
                available_quantity_in_mc=_round2(values["available_quantity_in_mc"]) or 0.0,
                available_volume_cbm=_round2(values["available_volume_cbm"]) or 0.0,
                available_gross_weight_kg=_round2(values["available_gross_weight_kg"]) or 0.0,
                available_amount_kzt=_round2(values["available_amount_kzt"]) or 0.0,
                recommended_quantity_in_mc=(
                    _round2(values["recommended_quantity_in_mc"]) or 0.0
                ),
                recommended_volume_cbm=_round2(values["recommended_volume_cbm"]) or 0.0,
                recommended_gross_weight_kg=(
                    _round2(values["recommended_gross_weight_kg"]) or 0.0
                ),
                recommended_amount_kzt=_round2(values["recommended_amount_kzt"]) or 0.0,
                branch_health_index=_round2(avg_health_index) or 0.0,
                owner_user_id=owner_user_id or 0,
            )
        )

    db.add_all(inserts)
    await db.commit()


async def refresh_dp_vertical(db: AsyncSession, owner_user_id: int | None = None) -> None:
    await refresh_forecast_sales_monthly(db, owner_user_id=owner_user_id)
    await refresh_dp_report_mart(db, owner_user_id=owner_user_id)


async def _refresh_downstream_materialized(
    db: AsyncSession,
    *,
    owner_user_id: int | None,
    changed_skus: list[str] | None,
    use_incremental: bool,
    stage_group: str,
) -> None:
    stage_start = time.perf_counter()
    await refresh_dp_report_mart(
        db,
        owner_user_id=owner_user_id,
        changed_skus=changed_skus if use_incremental else None,
    )
    logger.info(
        "refresh_all_materialized stage=%s.dp_report owner=%s elapsed_ms=%s incremental=%s",
        stage_group,
        owner_user_id,
        round((time.perf_counter() - stage_start) * 1000, 1),
        use_incremental,
    )

    stage_start = time.perf_counter()
    await refresh_forecast_orders(
        db,
        owner_user_id=owner_user_id,
        changed_skus=changed_skus if use_incremental else None,
    )
    logger.info(
        "refresh_all_materialized stage=%s.forecast_orders owner=%s elapsed_ms=%s incremental=%s",
        stage_group,
        owner_user_id,
        round((time.perf_counter() - stage_start) * 1000, 1),
        use_incremental,
    )

    # Inventory/distribution stay full-rebuild for correctness guardrails.
    stage_start = time.perf_counter()
    await refresh_inventory_health(db, owner_user_id=owner_user_id)
    logger.info(
        "refresh_all_materialized stage=%s.inventory_health owner=%s elapsed_ms=%s incremental=false",
        stage_group,
        owner_user_id,
        round((time.perf_counter() - stage_start) * 1000, 1),
    )

    stage_start = time.perf_counter()
    await refresh_branch_distribution(db, owner_user_id=owner_user_id)
    logger.info(
        "refresh_all_materialized stage=%s.branch_distribution owner=%s elapsed_ms=%s incremental=false",
        stage_group,
        owner_user_id,
        round((time.perf_counter() - stage_start) * 1000, 1),
    )


async def refresh_all_materialized(
    db: AsyncSession,
    owner_user_id: int | None = None,
    changed_keys: list[tuple[str, str]] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> None:
    changed_keys = _normalize_changed_keys(changed_keys)
    changed_skus = (
        sorted({sku_code for sku_code, _branch_id in changed_keys}) if changed_keys else None
    )
    use_incremental = bool(
        settings.INCREMENTAL_REFRESH_ENABLED and changed_keys and len(changed_keys) > 0
    )

    refresh_start = time.perf_counter()

    def _set_stage(stage: str) -> None:
        if stage_callback is not None:
            stage_callback(stage)

    _set_stage("fast_baseline_forecast")
    stage_start = time.perf_counter()
    fast_forecast_stats = await refresh_forecast_sales_monthly(
        db,
        owner_user_id=owner_user_id,
        changed_keys=changed_keys if use_incremental else None,
        forecast_source="fast",
    )
    logger.info(
        "refresh_all_materialized stage=fast_forecast_sales owner=%s elapsed_ms=%s incremental=%s",
        owner_user_id,
        round((time.perf_counter() - stage_start) * 1000, 1),
        use_incremental,
    )

    await _refresh_downstream_materialized(
        db,
        owner_user_id=owner_user_id,
        changed_skus=changed_skus,
        use_incremental=use_incremental,
        stage_group="fast",
    )
    baseline_ready_ms = round((time.perf_counter() - refresh_start) * 1000, 1)
    _set_stage(
        "baseline_ready_gpt_refining"
        if settings.FORECAST_GPT_REFINEMENT_ENABLED
        else "statistical_baseline_ready"
    )
    logger.info(
        "refresh_all_materialized baseline_ready owner=%s elapsed_ms=%s forecast_series=%s",
        owner_user_id,
        baseline_ready_ms,
        fast_forecast_stats.get("series_processed", 0),
    )
    if not settings.FORECAST_GPT_REFINEMENT_ENABLED:
        logger.info(
            "refresh_all_materialized completed owner=%s incremental=%s total_elapsed_ms=%s statistical_series=%s gpt_refinement_enabled=false",
            owner_user_id,
            use_incremental,
            round((time.perf_counter() - refresh_start) * 1000, 1),
            fast_forecast_stats.get("series_processed", 0),
        )
        return

    _set_stage("gpt_refinement")
    stage_start = time.perf_counter()
    gpt_forecast_stats = await refresh_forecast_sales_monthly(
        db,
        owner_user_id=owner_user_id,
        changed_keys=changed_keys if use_incremental else None,
        forecast_source="gpt",
    )
    logger.info(
        "refresh_all_materialized stage=gpt_forecast_sales owner=%s elapsed_ms=%s incremental=%s",
        owner_user_id,
        round((time.perf_counter() - stage_start) * 1000, 1),
        use_incremental,
    )

    await _refresh_downstream_materialized(
        db,
        owner_user_id=owner_user_id,
        changed_skus=changed_skus,
        use_incremental=use_incremental,
        stage_group="gpt",
    )
    logger.info(
        "refresh_all_materialized completed owner=%s incremental=%s baseline_ready_ms=%s total_elapsed_ms=%s fast_series=%s gpt_series=%s gpt_attempted=%s gpt_fallbacks=%s cache_hits=%s cache_writes=%s",
        owner_user_id,
        use_incremental,
        baseline_ready_ms,
        round((time.perf_counter() - refresh_start) * 1000, 1),
        fast_forecast_stats.get("series_processed", 0),
        gpt_forecast_stats.get("series_processed", 0),
        gpt_forecast_stats.get("gpt_attempted", 0),
        gpt_forecast_stats.get("gpt_fallbacks", 0),
        gpt_forecast_stats.get("cache_hits", 0),
        gpt_forecast_stats.get("cache_writes", 0),
    )

