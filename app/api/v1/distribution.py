from datetime import date
from io import BytesIO

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.core.branch_localization import normalize_branch_lookup
from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import Branch, HistoricalSalesMonthly, PriceList, Product, ProductBranch
from app.models.derived import DistributionBranchAdjustment, DistributionSkuAdjustment, ForecastSalesMonthly

router = APIRouter(prefix="/distribution", tags=["distribution"])

PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


class DistributionAggregateRow(BaseModel):
    branch_name: str
    available_quantity_in_mc_per_branch: float
    available_volume_cbm_per_branch: float
    available_gross_weight_kg_per_branch: float
    available_amount_kzt_per_branch: float
    recommended_quantity_in_mc_per_branch: float
    recommended_volume_cbm_per_branch: float
    recommended_gross_weight_kg_per_branch: float
    recommended_amount_kzt_per_branch: float
    adjusted_quantity_in_mc_per_branch: float
    branch_health_index: int


class DistributionAggregateResponse(BaseModel):
    planning_date: str
    items: list[DistributionAggregateRow]
    total_items: int
    total_pages: int


class DistributionSummaryResponse(BaseModel):
    planning_date: str
    total_recommended_quantity: float
    total_recommended_volume_cbm: float


class DistributionDetailRow(BaseModel):
    sku_code: str
    sku_name: str
    total_available_quantity_in_mc: float
    available_quantity_in_mc: float
    average_l3m_quantity_in_mc: float
    average_f3m_quantity_in_mc: float
    recommended_quantity_in_mc: float
    adjusted_quantity_in_mc: float | None = None


class DistributionDetailsResponse(BaseModel):
    planning_date: str
    branch_name: str
    items: list[DistributionDetailRow]
    total_items: int
    total_pages: int


class DistributionBranchAdjustRow(BaseModel):
    branch_name: str
    adjusted_quantity_in_mc_per_branch: float


class DistributionBranchAdjustRequest(BaseModel):
    updates: list[DistributionBranchAdjustRow]


class DistributionSkuAdjustRow(BaseModel):
    sku_code: str
    adjusted_quantity_in_mc: float | None = None
    adjusted_quantity_in_mc_per_branch: float | None = None


class DistributionSkuAdjustRequest(BaseModel):
    updates: list[DistributionSkuAdjustRow]


class _SkuBranchCalc(BaseModel):
    owner_user_id: int
    branch_id: str
    branch_name: str
    sku_id: str
    sku_code: str
    sku_name: str
    current_stock: float
    stock_norm: float
    pieces_in_master_carton: float
    master_carton_volume_cbm: float
    master_carton_gross_weight_kg: float
    dsp: float
    avg_l3m: float
    avg_f3m: float
    recommended_quantity: float
    sales_share: float
    future_health_index: float
    adjusted_detail_quantity: float | None = None


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be one of: 10, 50, 100, all",
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


def _branch_name_matches(query_value: str, branch_name: str, branch_id: str) -> bool:
    return normalize_branch_lookup(query_value) == normalize_branch_lookup(branch_name) or str(query_value).strip() == str(branch_id).strip()


async def _resolve_planning_date(db: DBSession, user: CurrentUser) -> date:
    stmt = select(func.max(HistoricalSalesMonthly.date))
    if not is_admin(user):
        stmt = stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
    max_hist = (await db.execute(stmt)).scalar_one_or_none()
    if max_hist is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot derive planning_date without historical_sales_monthly data",
        )
    return _add_months(_month_start(max_hist), 1)


def _pick_dsp(prices: list[PriceList], planning_date: date) -> float:
    if not prices:
        return 0.0
    sorted_prices = sorted(prices, key=lambda p: p.date)
    selected = None
    for p in sorted_prices:
        if p.date <= planning_date:
            selected = p
    if selected is None:
        selected = sorted_prices[-1]
    return float(selected.dsp)


async def _build_distribution_calc(
    db: DBSession,
    user: CurrentUser,
) -> tuple[date, list[_SkuBranchCalc], dict[str, str], dict[tuple[int, str], float]]:
    planning_date = await _resolve_planning_date(db, user)

    pb_stmt = select(ProductBranch)
    p_stmt = select(Product)
    br_stmt = select(Branch)
    hs_stmt = select(HistoricalSalesMonthly)
    fs_stmt = select(ForecastSalesMonthly)
    pr_stmt = select(PriceList)
    da_stmt = select(DistributionSkuAdjustment)
    if not is_admin(user):
        pb_stmt = pb_stmt.where(ProductBranch.owner_user_id == user.id)
        p_stmt = p_stmt.where(Product.owner_user_id == user.id)
        br_stmt = br_stmt.where(Branch.owner_user_id == user.id)
        hs_stmt = hs_stmt.where(HistoricalSalesMonthly.owner_user_id == user.id)
        fs_stmt = fs_stmt.where(ForecastSalesMonthly.owner_user_id == user.id)
        pr_stmt = pr_stmt.where(PriceList.owner_user_id == user.id)
        da_stmt = da_stmt.where(DistributionSkuAdjustment.owner_user_id == user.id)

    product_branch_rows = (await db.execute(pb_stmt)).scalars().all()
    product_rows = (await db.execute(p_stmt)).scalars().all()
    branch_rows = (await db.execute(br_stmt)).scalars().all()
    hist_rows = (await db.execute(hs_stmt)).scalars().all()
    forecast_rows = (await db.execute(fs_stmt)).scalars().all()
    price_rows = (await db.execute(pr_stmt)).scalars().all()
    detail_adjustments = (await db.execute(da_stmt.where(DistributionSkuAdjustment.planning_date == planning_date))).scalars().all()

    branch_name_map = {(b.owner_user_id, b.branch_id): b.branch_name for b in branch_rows}
    branch_id_by_name: dict[tuple[int, str], str] = {}
    for b in branch_rows:
        branch_id_by_name[(b.owner_user_id, normalize_branch_lookup(b.branch_name))] = b.branch_id

    product_map = {(p.owner_user_id, p.sku_id): p for p in product_rows}
    prices_by_key: dict[tuple[int, str], list[PriceList]] = {}
    for pr in price_rows:
        prices_by_key.setdefault((pr.owner_user_id, pr.sku_id), []).append(pr)

    l3_months = {_add_months(planning_date, -1), _add_months(planning_date, -2), _add_months(planning_date, -3)}
    f3_months = {_add_months(planning_date, 0), _add_months(planning_date, 1), _add_months(planning_date, 2)}

    l3_by_key: dict[tuple[int, str, str], list[float]] = {}
    sales_qty_by_sku: dict[tuple[int, str], float] = {}
    for r in hist_rows:
        m = _month_start(r.date)
        if m in l3_months:
            l3_by_key.setdefault((r.owner_user_id, r.sku_id, r.branch_id), []).append(float(r.fact_quantity_in_mc or 0.0))
            sales_qty_by_sku[(r.owner_user_id, r.sku_id)] = sales_qty_by_sku.get((r.owner_user_id, r.sku_id), 0.0) + float(r.fact_quantity_in_mc or 0.0)

    f3_by_key: dict[tuple[int, str, str], list[float]] = {}
    for r in forecast_rows:
        m = _month_start(r.date)
        if m in f3_months:
            qty = (
                float(r.adjusted_forecast_quantity_in_mc)
                if r.adjusted_forecast_quantity_in_mc is not None
                else float(r.baseline_forecast_quantity_in_mc or 0.0)
            )
            f3_by_key.setdefault((r.owner_user_id, r.sku_id, r.branch_id), []).append(qty)

    total_sales_qty = sum(sales_qty_by_sku.values())
    sales_share_by_sku: dict[tuple[int, str], float] = {
        k: (v / total_sales_qty if total_sales_qty > 0 else 0.0)
        for k, v in sales_qty_by_sku.items()
    }

    detail_adjustment_map = {
        (a.owner_user_id, a.branch_id, a.sku_id): float(a.adjusted_quantity_in_mc)
        for a in detail_adjustments
    }

    total_available_by_sku: dict[tuple[int, str], float] = {}
    calc_rows: list[_SkuBranchCalc] = []
    for pb in product_branch_rows:
        product = product_map.get((pb.owner_user_id, pb.sku_id))
        if not product:
            continue
        branch_name = branch_name_map.get((pb.owner_user_id, pb.branch_id), pb.branch_id)
        prices = prices_by_key.get((pb.owner_user_id, pb.sku_id), [])
        dsp = _pick_dsp(prices, planning_date)
        avg_l3m_vals = l3_by_key.get((pb.owner_user_id, pb.sku_id, pb.branch_id), [])
        avg_f3m_vals = f3_by_key.get((pb.owner_user_id, pb.sku_id, pb.branch_id), [])
        avg_l3m = (sum(avg_l3m_vals) / len(avg_l3m_vals)) if avg_l3m_vals else 0.0
        avg_f3m = (sum(avg_f3m_vals) / len(avg_f3m_vals)) if avg_f3m_vals else 0.0

        current_stock = float(pb.current_stock or 0.0)
        stock_norm = float(pb.stock_norm or 0.0)
        target_stock = stock_norm * avg_f3m / 30.0 if stock_norm > 0 else 0.0
        recommended = max(target_stock - current_stock, 0.0)
        if target_stock > 0:
            future_health_index = ((current_stock - target_stock) / target_stock) + 1.0
        else:
            future_health_index = 1.0
        sales_share = sales_share_by_sku.get((pb.owner_user_id, pb.sku_id), 0.0)

        adjusted_detail = detail_adjustment_map.get((pb.owner_user_id, pb.branch_id, pb.sku_id))

        calc_rows.append(
            _SkuBranchCalc(
                owner_user_id=pb.owner_user_id,
                branch_id=pb.branch_id,
                branch_name=branch_name,
                sku_id=pb.sku_id,
                sku_code=product.sku_code,
                sku_name=product.sku_name,
                current_stock=current_stock,
                stock_norm=stock_norm,
                pieces_in_master_carton=float(product.pieces_in_master_carton),
                master_carton_volume_cbm=float(product.master_carton_volume_cbm),
                master_carton_gross_weight_kg=float(product.master_carton_gross_weight_kg),
                dsp=dsp,
                avg_l3m=avg_l3m,
                avg_f3m=avg_f3m,
                recommended_quantity=recommended,
                sales_share=sales_share,
                future_health_index=future_health_index,
                adjusted_detail_quantity=adjusted_detail,
            )
        )
        total_available_by_sku[(pb.owner_user_id, pb.sku_id)] = (
            total_available_by_sku.get((pb.owner_user_id, pb.sku_id), 0.0) + current_stock
        )

    return planning_date, calc_rows, branch_id_by_name, total_available_by_sku


@router.get("", response_model=DistributionAggregateResponse, include_in_schema=False)
@router.get("/", response_model=DistributionAggregateResponse)
async def get_distribution_aggregated(
    db: DBSession,
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> DistributionAggregateResponse:
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)

    branch_adjust_stmt = select(DistributionBranchAdjustment).where(
        DistributionBranchAdjustment.planning_date == planning_date
    )
    if not is_admin(user):
        branch_adjust_stmt = branch_adjust_stmt.where(DistributionBranchAdjustment.owner_user_id == user.id)
    branch_adjust_rows = (await db.execute(branch_adjust_stmt)).scalars().all()
    branch_adjust_map = {
        (r.owner_user_id, r.branch_id): float(r.adjusted_quantity_in_mc) for r in branch_adjust_rows
    }

    buckets: dict[tuple[int, str, str], dict[str, float]] = {}
    for r in calc_rows:
        key = (r.owner_user_id, r.branch_id, r.branch_name)
        if key not in buckets:
            buckets[key] = {
                "available_quantity_in_mc_per_branch": 0.0,
                "available_volume_cbm_per_branch": 0.0,
                "available_gross_weight_kg_per_branch": 0.0,
                "available_amount_kzt_per_branch": 0.0,
                "recommended_quantity_in_mc_per_branch": 0.0,
                "recommended_volume_cbm_per_branch": 0.0,
                "recommended_gross_weight_kg_per_branch": 0.0,
                "recommended_amount_kzt_per_branch": 0.0,
                "sales_weighted_hi": 0.0,
            }
        b = buckets[key]
        b["available_quantity_in_mc_per_branch"] += r.current_stock
        b["available_volume_cbm_per_branch"] += r.current_stock * r.master_carton_volume_cbm
        b["available_gross_weight_kg_per_branch"] += r.current_stock * r.master_carton_gross_weight_kg
        b["available_amount_kzt_per_branch"] += r.current_stock * r.pieces_in_master_carton * r.dsp
        b["recommended_quantity_in_mc_per_branch"] += r.recommended_quantity
        b["recommended_volume_cbm_per_branch"] += r.recommended_quantity * r.master_carton_volume_cbm
        b["recommended_gross_weight_kg_per_branch"] += r.recommended_quantity * r.master_carton_gross_weight_kg
        b["recommended_amount_kzt_per_branch"] += r.recommended_quantity * r.pieces_in_master_carton * r.dsp
        b["sales_weighted_hi"] += r.future_health_index * r.sales_share

    rows: list[DistributionAggregateRow] = []
    for (owner_user_id, branch_id, branch_name), vals in buckets.items():
        adjusted_branch = branch_adjust_map.get((owner_user_id, branch_id), 0.0)
        rows.append(
            DistributionAggregateRow(
                branch_name=branch_name,
                available_quantity_in_mc_per_branch=round(vals["available_quantity_in_mc_per_branch"], 2),
                available_volume_cbm_per_branch=round(vals["available_volume_cbm_per_branch"], 2),
                available_gross_weight_kg_per_branch=round(vals["available_gross_weight_kg_per_branch"], 2),
                available_amount_kzt_per_branch=round(vals["available_amount_kzt_per_branch"], 2),
                recommended_quantity_in_mc_per_branch=round(vals["recommended_quantity_in_mc_per_branch"], 2),
                recommended_volume_cbm_per_branch=round(vals["recommended_volume_cbm_per_branch"], 2),
                recommended_gross_weight_kg_per_branch=round(vals["recommended_gross_weight_kg_per_branch"], 2),
                recommended_amount_kzt_per_branch=round(vals["recommended_amount_kzt_per_branch"], 2),
                adjusted_quantity_in_mc_per_branch=round(adjusted_branch, 2),
                branch_health_index=int(round(vals["sales_weighted_hi"] * 100)),
            )
        )
    rows.sort(key=lambda x: x.branch_name)
    paged, total_items, total_pages = _paginate(rows, page=page, page_size=page_size)
    return DistributionAggregateResponse(
        planning_date=planning_date.isoformat(),
        items=paged,
        total_items=total_items,
        total_pages=total_pages,
    )


@router.get("/summary/", response_model=DistributionSummaryResponse, include_in_schema=False)
@router.get("/summary", response_model=DistributionSummaryResponse)
async def get_distribution_summary(
    db: DBSession,
    user: CurrentUser,
) -> DistributionSummaryResponse:
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    total_recommended_quantity = sum(r.recommended_quantity for r in calc_rows)
    total_recommended_volume = sum(r.recommended_quantity * r.master_carton_volume_cbm for r in calc_rows)
    return DistributionSummaryResponse(
        planning_date=planning_date.isoformat(),
        total_recommended_quantity=round(total_recommended_quantity, 2),
        total_recommended_volume_cbm=round(total_recommended_volume, 2),
    )


@router.get("/details", response_model=DistributionDetailsResponse)
async def get_distribution_details(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> DistributionDetailsResponse:
    planning_date, calc_rows, _, total_available_by_sku = await _build_distribution_calc(db, user)
    selected = [r for r in calc_rows if _branch_name_matches(branch_name, r.branch_name, r.branch_id)]
    if not selected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    detail_rows = [
        DistributionDetailRow(
            sku_code=r.sku_code,
            sku_name=r.sku_name,
            total_available_quantity_in_mc=round(total_available_by_sku.get((r.owner_user_id, r.sku_id), 0.0), 2),
            available_quantity_in_mc=round(r.current_stock, 2),
            average_l3m_quantity_in_mc=round(r.avg_l3m, 2),
            average_f3m_quantity_in_mc=round(r.avg_f3m, 2),
            recommended_quantity_in_mc=round(r.recommended_quantity, 2),
            adjusted_quantity_in_mc=round(r.adjusted_detail_quantity, 2) if r.adjusted_detail_quantity is not None else None,
        )
        for r in selected
    ]
    detail_rows.sort(key=lambda x: x.sku_code)
    paged, total_items, total_pages = _paginate(detail_rows, page=page, page_size=page_size)
    return DistributionDetailsResponse(
        planning_date=planning_date.isoformat(),
        branch_name=selected[0].branch_name,
        items=paged,
        total_items=total_items,
        total_pages=total_pages,
    )


@router.patch("", include_in_schema=False)
@router.patch("/")
async def patch_distribution_branch_adjustments(
    db: DBSession,
    user: CurrentUser,
    payload: DistributionBranchAdjustRequest,
) -> dict:
    if not payload.updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="updates cannot be empty")
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
                delete(DistributionBranchAdjustment).where(
                    DistributionBranchAdjustment.owner_user_id == owner_id,
                    DistributionBranchAdjustment.planning_date == planning_date,
                    DistributionBranchAdjustment.branch_id == branch_id,
                )
            )
            db.add(
                DistributionBranchAdjustment(
                    owner_user_id=owner_id,
                    planning_date=planning_date,
                    branch_id=branch_id,
                    adjusted_quantity_in_mc=float(row.adjusted_quantity_in_mc_per_branch),
                )
            )
            updated += 1
        if not matched and row.branch_name:
            # allow legacy branch_id input
            if owner_user_id is not None:
                db.add(
                    DistributionBranchAdjustment(
                        owner_user_id=owner_user_id,
                        planning_date=planning_date,
                        branch_id=row.branch_name,
                        adjusted_quantity_in_mc=float(row.adjusted_quantity_in_mc_per_branch),
                    )
                )
                updated += 1
    await db.commit()
    return {"rows_updated": updated}


@router.patch("/details")
async def patch_distribution_detail_adjustments(
    db: DBSession,
    user: CurrentUser,
    branch_name: str = Query(...),
    payload: DistributionSkuAdjustRequest = ...,
) -> dict:
    if not payload.updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="updates cannot be empty")
    wrong_field_rows = [
        {
            "sku_code": item.sku_code,
            "provided_field": "adjusted_quantity_in_mc_per_branch",
            "expected_field": "adjusted_quantity_in_mc",
        }
        for item in payload.updates
        if item.adjusted_quantity_in_mc is None
        and item.adjusted_quantity_in_mc_per_branch is not None
    ]
    if wrong_field_rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Invalid payload for /distribution/details. Use adjusted_quantity_in_mc, not adjusted_quantity_in_mc_per_branch.",
                "invalid_updates": wrong_field_rows,
            },
        )
    planning_date, calc_rows, _, _ = await _build_distribution_calc(db, user)
    branch_rows = [r for r in calc_rows if _branch_name_matches(branch_name, r.branch_name, r.branch_id)]
    if not branch_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    by_owner_code: dict[tuple[int, str], _SkuBranchCalc] = {(r.owner_user_id, r.sku_code): r for r in branch_rows}
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
                    DistributionSkuAdjustment.sku_id == row.sku_id,
                )
            )
            if item.adjusted_quantity_in_mc is not None:
                db.add(
                    DistributionSkuAdjustment(
                        owner_user_id=owner_id,
                        planning_date=planning_date,
                        branch_id=row.branch_id,
                        sku_id=row.sku_id,
                        adjusted_quantity_in_mc=float(item.adjusted_quantity_in_mc),
                    )
                )
            updated += 1
    await db.commit()
    return {"rows_updated": updated}


@router.get("/download")
async def download_distribution(
    db: DBSession,
    user: CurrentUser,
    branch_name: str | None = Query(None),
):
    response = await get_distribution_aggregated(db=db, user=user, page=1, page_size="all")
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

