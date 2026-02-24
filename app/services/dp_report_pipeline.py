from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime
import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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
    ForecastOrders,
    ForecastSalesMonthly,
    InventoryHealth,
)
from app.services.gpt_forecasting import forecast_baseline_quantities_in_mc


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


def _round2(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v), 2)


def _avg_last_n(values: list[float], n: int = 6) -> float:
    if not values:
        return 0.0
    tail = values[-n:] if len(values) >= n else values
    return sum(tail) / len(tail) if tail else 0.0


async def refresh_forecast_sales_monthly(
    db: AsyncSession, owner_user_id: int | None = None
) -> None:
    products = {
        p.sku_id: p
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
    hist_rows = (
        await db.execute(
            select(HistoricalSalesMonthly).where(
                HistoricalSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    placed_orders = (
        await db.execute(
            select(PlacedOrder).where(PlacedOrder.owner_user_id == owner_user_id)
        )
    ).scalars().all()

    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for p in prices:
        prices_by_sku[p.sku_id].append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    hist_by_key: dict[tuple[str, str], list[HistoricalSalesMonthly]] = defaultdict(list)
    for row in hist_rows:
        hist_by_key[(row.sku_id, row.branch_id)].append(row)
    for key in hist_by_key:
        hist_by_key[key].sort(key=lambda x: x.date)

    branch_stock = {(b.sku_id, b.branch_id): b.current_stock for b in branch_rows}
    stock_norm_by_key = {(b.sku_id, b.branch_id): b.stock_norm for b in branch_rows}

    placed_orders_by_sku: dict[str, list[PlacedOrder]] = defaultdict(list)
    for po in placed_orders:
        placed_orders_by_sku[po.sku_id].append(po)
    for sku in placed_orders_by_sku:
        placed_orders_by_sku[sku].sort(key=lambda x: x.creation_date)

    await db.execute(
        delete(ForecastSalesMonthly).where(
            ForecastSalesMonthly.owner_user_id == owner_user_id
        )
    )

    to_insert: list[ForecastSalesMonthly] = []

    job_payloads: list[dict] = []
    for key, rows in hist_by_key.items():
        sku_id, branch_id = key
        product = products.get(sku_id)
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
                "target_quantity_in_mc": float(r.target_quantity_in_mc or 0.0),
                "past_available_stock": float(r.past_available_stock or 0.0),
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
            for po in placed_orders_by_sku.get(sku_id, [])
        ]
        job_payloads.append(
            {
                "sku_id": sku_id,
                "branch_id": branch_id,
                "product": product,
                "rows": rows,
                "forecast_months": forecast_months,
                "history_context": history_context,
                "orders_context": orders_context,
                "current_stock": float(branch_stock.get((sku_id, branch_id), 0.0)),
                "stock_norm_days": float(
                    stock_norm_by_key.get((sku_id, branch_id), product.general_stock_norm_days)
                    or 0.0
                ),
            }
        )

    max_concurrency = max(int(settings.OPENAI_FORECAST_MAX_CONCURRENCY or 1), 1)
    timeout_seconds = float(settings.OPENAI_FORECAST_TIMEOUT_SECONDS or 15.0)
    semaphore = asyncio.Semaphore(max_concurrency)
    memo: dict[str, list[float]] = {}

    async def _get_baseline_series(job: dict) -> list[float]:
        history_values = [
            float(x.get("fact_quantity_in_mc") or 0.0)
            for x in job["history_context"]
        ]
        avg_baseline = _avg_last_n(history_values, n=6)
        fallback_series = [avg_baseline for _ in job["forecast_months"]]

        # Skip model call when history is too short; use deterministic fallback.
        if len(history_values) < 6:
            return fallback_series

        cache_payload = {
            "sku_id": job["sku_id"],
            "branch_id": job["branch_id"],
            "forecast_months": [d.isoformat() for d in job["forecast_months"]],
            "history_context": job["history_context"][-24:],
            "orders_context": job["orders_context"][-24:],
            "current_stock": round(float(job["current_stock"]), 6),
            "stock_norm_days": round(float(job["stock_norm_days"]), 6),
            "model": settings.OPENAI_FORECAST_MODEL,
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if cache_key in memo:
            return memo[cache_key]

        async with semaphore:
            series = await forecast_baseline_quantities_in_mc(
                sku_id=job["sku_id"],
                branch_id=job["branch_id"],
                forecast_months=job["forecast_months"],
                history=job["history_context"],
                current_stock=job["current_stock"],
                stock_norm_days=job["stock_norm_days"],
                placed_orders_history=job["orders_context"],
                timeout_seconds=timeout_seconds,
            )
        memo[cache_key] = series
        return series

    baseline_series_by_key: dict[tuple[str, str], list[float]] = {}
    if job_payloads:
        series_results = await asyncio.gather(
            *[_get_baseline_series(job) for job in job_payloads]
        )
        for idx, job in enumerate(job_payloads):
            baseline_series_by_key[(job["sku_id"], job["branch_id"])] = series_results[idx]

    for job in job_payloads:
        sku_id = job["sku_id"]
        branch_id = job["branch_id"]
        product = job["product"]
        prev_stock = float(job["current_stock"])
        forecast_months = job["forecast_months"]
        baseline_series = baseline_series_by_key.get((sku_id, branch_id), [])

        for idx, forecast_date in enumerate(forecast_months):
            baseline_qty = float(baseline_series[idx]) if idx < len(baseline_series) else 0.0
            closest_dsp = _closest_price_on_or_before(
                prices_by_sku.get(sku_id, []), forecast_date
            )
            baseline_amount = None
            if closest_dsp is not None:
                baseline_amount = (
                    baseline_qty * product.pieces_in_master_carton * closest_dsp.dsp
                )
            future_stock = max(prev_stock - baseline_qty, 0.0)
            to_insert.append(
                ForecastSalesMonthly(
                    sku_id=sku_id,
                    branch_id=branch_id,
                    date=forecast_date,
                    baseline_forecast_quantity_in_mc=_round2(baseline_qty) or 0.0,
                    baseline_forecast_gross_weight_kg=(
                        _round2(baseline_qty * product.master_carton_gross_weight_kg)
                    ),
                    baseline_forecast_volume_cbm=(
                        _round2(baseline_qty * product.master_carton_volume_cbm)
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


async def refresh_dp_report_mart(
    db: AsyncSession, owner_user_id: int | None = None
) -> None:
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

    await db.execute(delete(DPReportMart).where(DPReportMart.owner_user_id == owner_user_id))
    to_insert: list[DPReportMart] = []

    for r in hist_rows:
        to_insert.append(
            DPReportMart(
                sku_id=r.sku_id,
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
    db: AsyncSession, owner_user_id: int | None = None
) -> None:
    fc_rows = (
        await db.execute(
            select(ForecastSalesMonthly).where(
                ForecastSalesMonthly.owner_user_id == owner_user_id
            )
        )
    ).scalars().all()
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

    pb_norm = {(r.sku_id, r.branch_id): float(r.stock_norm) for r in pb_rows}

    by_sku_date_branch: dict[str, dict[date, dict[str, ForecastSalesMonthly]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for row in fc_rows:
        by_sku_date_branch[row.sku_id][row.date][row.branch_id] = row
    hist_qty_total_by_sku_date: dict[tuple[str, date], float] = defaultdict(float)
    for row in hist_rows:
        hist_qty_total_by_sku_date[(row.sku_id, row.date)] += float(
            row.fact_quantity_in_mc or 0.0
        )
    fc_qty_total_by_sku_date: dict[tuple[str, date], float] = defaultdict(float)
    for row in fc_rows:
        fc_qty_total_by_sku_date[(row.sku_id, row.date)] += float(
            row.adjusted_forecast_quantity_in_mc
            if row.adjusted_forecast_quantity_in_mc is not None
            else row.baseline_forecast_quantity_in_mc
        )

    await db.execute(
        delete(ForecastOrders).where(ForecastOrders.owner_user_id == owner_user_id)
    )
    inserts: list[ForecastOrders] = []

    for sku_id, date_map in by_sku_date_branch.items():
        dates = sorted(date_map.keys())
        for idx, d in enumerate(dates):
            prev_d = _prev_month(d)
            month_prior_stock = 0.0
            if prev_d in date_map:
                month_prior_stock = sum(
                    r.future_available_stock for r in date_map[prev_d].values()
                )

            f3_slice = dates[idx : idx + 3]

            l3_vals: list[float] = []
            # Hybrid rolling window for L3M: current planning month + previous two months.
            # Per month, prefer historical fact; otherwise fallback to forecast.
            l3_months = [d, _prev_month(d), _prev_month(_prev_month(d))]
            for dd in l3_months:
                hist_total = hist_qty_total_by_sku_date.get((sku_id, dd))
                if hist_total is not None:
                    l3_vals.append(float(hist_total))
                    continue
                fc_total = fc_qty_total_by_sku_date.get((sku_id, dd))
                if fc_total is not None:
                    l3_vals.append(float(fc_total))
                    continue
                l3_vals.append(0.0)
            f3_vals: list[float] = []
            for dd in f3_slice:
                f3_vals.extend(
                    [r.baseline_forecast_quantity_in_mc for r in date_map[dd].values()]
                )
            avg_l3 = sum(l3_vals) / len(l3_vals) if l3_vals else 0.0
            avg_f3 = sum(f3_vals) / len(f3_vals) if f3_vals else 0.0

            rec_total = 0.0
            prev_branch_rows = date_map.get(prev_d, {})
            for branch_id in date_map[d].keys():
                prior_stock_b = (
                    prev_branch_rows[branch_id].future_available_stock
                    if branch_id in prev_branch_rows
                    else 0.0
                )
                stock_norm = pb_norm.get((sku_id, branch_id), 0.0)
                needed = stock_norm * (avg_f3 / 30.0)
                rec_total += max(needed - prior_stock_b, 0.0)

            inserts.append(
                ForecastOrders(
                    sku_id=sku_id,
                    date=d,
                    month_prior_available_stock=_round2(month_prior_stock) or 0.0,
                    average_l3m_quantity_in_mc=_round2(avg_l3) or 0.0,
                    average_f3m_quantity_in_mc=_round2(avg_f3) or 0.0,
                    recommended_quantity_in_mc=_round2(rec_total) or 0.0,
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
        p.sku_id: p
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

    pb_map = {(r.sku_id, r.branch_id): r for r in pb_rows}
    hist_map = {(r.sku_id, r.branch_id, r.date): r for r in hist_rows}
    fc_map = {(r.sku_id, r.branch_id, r.date): r for r in fc_rows}

    prices_by_sku: dict[str, list[PriceList]] = defaultdict(list)
    for p in prices:
        prices_by_sku[p.sku_id].append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    key_dates: dict[tuple[str, str], set[date]] = defaultdict(set)
    for r in hist_rows:
        key_dates[(r.sku_id, r.branch_id)].add(r.date)
    for r in fc_rows:
        key_dates[(r.sku_id, r.branch_id)].add(r.date)

    total_sales_by_date: dict[date, float] = defaultdict(float)
    for r in hist_rows:
        total_sales_by_date[r.date] += r.fact_quantity_in_mc

    await db.execute(
        delete(InventoryHealth).where(InventoryHealth.owner_user_id == owner_user_id)
    )
    inserts: list[InventoryHealth] = []
    temp_rows: list[dict] = []

    for (sku_id, branch_id), dates in key_dates.items():
        product = products.get(sku_id)
        if not product:
            continue
        pb = pb_map.get((sku_id, branch_id))
        fc_for_key = sorted(
            [r for r in fc_rows if r.sku_id == sku_id and r.branch_id == branch_id],
            key=lambda x: x.date,
        )
        fc_dates = [r.date for r in fc_for_key]
        for d in sorted(dates):
            hist = hist_map.get((sku_id, branch_id, d))
            fc = fc_map.get((sku_id, branch_id, d))

            sales_qty = float(hist.fact_quantity_in_mc) if hist else 0.0
            closest_price = _closest_price_on_or_before(prices_by_sku.get(sku_id, []), d)
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
                    "sku_id": sku_id,
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
        sku_totals[r["sku_id"]] += r["sales_qty"]
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
                category=categories.get(r["sku_id"], "C"),
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
        p.sku_id: p
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
        latest_dsp_by_sku[p.sku_id] = p.dsp

    latest_fo_by_sku: dict[str, ForecastOrders] = {}
    for row in sorted(fo_rows, key=lambda x: x.date):
        latest_fo_by_sku[row.sku_id] = row

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
        pb_by_sku[pb.sku_id].append(pb)
        product = products.get(pb.sku_id)
        if not product:
            continue
        dsp = latest_dsp_by_sku.get(pb.sku_id, 0.0)
        b = branch_buckets[pb.branch_id]
        b["available_quantity_in_mc"] += pb.current_stock
        b["available_volume_cbm"] += pb.current_stock * product.master_carton_volume_cbm
        b["available_gross_weight_kg"] += pb.current_stock * product.master_carton_gross_weight_kg
        b["available_amount_kzt"] += pb.current_stock * product.pieces_in_master_carton * dsp

    for sku_id, fo in latest_fo_by_sku.items():
        branches = pb_by_sku.get(sku_id, [])
        if not branches:
            continue
        product = products.get(sku_id)
        if not product:
            continue
        total_norm = sum(b.stock_norm for b in branches)
        if total_norm <= 0:
            total_norm = float(len(branches))
        qty_total = fo.adjusted_quantity_in_mc or fo.recommended_quantity_in_mc
        dsp = latest_dsp_by_sku.get(sku_id, 0.0)
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


async def refresh_all_materialized(
    db: AsyncSession, owner_user_id: int | None = None
) -> None:
    await refresh_forecast_sales_monthly(db, owner_user_id=owner_user_id)
    await refresh_dp_report_mart(db, owner_user_id=owner_user_id)
    await refresh_forecast_orders(db, owner_user_id=owner_user_id)
    await refresh_inventory_health(db, owner_user_id=owner_user_id)
    await refresh_branch_distribution(db, owner_user_id=owner_user_id)

