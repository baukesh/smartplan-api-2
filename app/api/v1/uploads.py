from io import BytesIO
from datetime import date, datetime
import logging

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import (
    Branch,
    Product,
    ProductBranch,
    HistoricalSalesMonthly,
    PlacedOrder,
    PriceList,
)
from app.services.dp_report_pipeline import refresh_all_materialized
from app.services.orders_aggregation import refresh_orders_aggregated

router = APIRouter(prefix="/uploads", tags=["uploads"])
logger = logging.getLogger(__name__)


class UploadSpec:
    def __init__(self, required_columns: list[str]):
        self.required_columns = required_columns


ASSORTMENT_SPEC = UploadSpec(
    [
        "sku_id",
        "sku_code",
        "mother_sku",
        "barcode",
        "sku_name",
        "pieces_in_master_carton",
        "master_carton_volume_cbm",
        "master_carton_gross_weight_kg",
        "master_carton_net_weight_kg",
        "lead_time",
        "source",
        "general_stock_norm_days",
        "status",
        "brand",
        "category",
        "sub_category",
        "sub_line",
    ]
)

BRANCH_STOCK_NORM_SPEC = UploadSpec(
    [
        "sku_id",
        "branch_name",
        "current_stock",
        "stock_norm",
    ]
)

PRICE_LIST_SPEC = UploadSpec(
    [
        "sku_id",
        "date",
        "invoice_price",
        "dsp",
    ]
)

HISTORICAL_SALES_MONTHLY_SPEC = UploadSpec(
    [
        "sku_id",
        "date",
        "branch_name",
        "fact_quantity_in_mc",
        "target_quantity_in_mc",
        "past_available_stock",
    ]
)

PLACED_ORDERS_SPEC = UploadSpec(
    [
        "order_id",
        "sku_id",
        "order_name",
        "creation_date",
        "receival_date",
        "quantity_in_mc",
        "author",
        "status",
    ]
)


def _load_excel(file: UploadFile) -> pd.DataFrame:
    try:
        content = file.file.read()
        return pd.read_excel(BytesIO(content))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse Excel file: {exc}",
        ) from exc


def _validate_columns(df: pd.DataFrame, spec: UploadSpec) -> list[dict]:
    errors: list[dict] = []
    missing = [c for c in spec.required_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "One or more required columns are missing",
                "columns": missing,
            }
        )
    return errors


async def _save_records(db: AsyncSession, df: pd.DataFrame, model: type) -> int:
    records = df.to_dict(orient="records")
    objs = [model(**r) for r in records]
    db.add_all(objs)
    await db.commit()
    return len(objs)


def _to_python_date(value: object) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if pd.isna(value):
        raise ValueError("Date value is empty")
    if isinstance(value, (int, float)):
        return pd.to_datetime(value, unit="D", origin="1899-12-30").date()
    return pd.to_datetime(value).date()


async def _replace_records(
    db: AsyncSession,
    model: type,
    records: list[dict],
    owner_user_id: int,
) -> int:
    await db.execute(delete(model).where(model.owner_user_id == owner_user_id))
    db.add_all([model(**r) for r in records])
    await db.commit()
    return len(records)


async def _refresh_materialized_safe(db: AsyncSession, owner_user_id: int) -> str | None:
    try:
        await refresh_all_materialized(db, owner_user_id=owner_user_id)
        return None
    except Exception as exc:
        # Keep upload successful because base rows are already committed.
        logger.exception(
            "Materialized refresh failed after upload for owner_user_id=%s",
            owner_user_id,
        )
        await db.rollback()
        return str(exc)


async def _resolve_branch_ids(
    db: AsyncSession,
    owner_user_id: int,
    branch_names: list[str],
) -> dict[str, str]:
    cleaned = sorted({name.strip() for name in branch_names if name and name.strip()})
    if not cleaned:
        return {}

    existing_rows = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    by_name = {r.branch_name: r.branch_id for r in existing_rows}
    used_ids = {r.branch_id for r in existing_rows}

    next_idx = 100001
    while str(next_idx) in used_ids:
        next_idx += 1

    new_rows: list[Branch] = []
    for name in cleaned:
        if name in by_name:
            continue
        while str(next_idx) in used_ids:
            next_idx += 1
        branch_id = str(next_idx)
        next_idx += 1
        used_ids.add(branch_id)
        by_name[name] = branch_id
        new_rows.append(
            Branch(
                owner_user_id=owner_user_id,
                branch_id=branch_id,
                branch_name=name,
            )
        )

    if new_rows:
        db.add_all(new_rows)
        await db.commit()

    return by_name


@router.post("/assortment")
async def upload_assortment(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, ASSORTMENT_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    records = df[ASSORTMENT_SPEC.required_columns].to_dict(orient="records")
    for r in records:
        r["owner_user_id"] = user.id
    count = await _replace_records(db, Product, records, owner_user_id=user.id)
    refresh_error = await _refresh_materialized_safe(db, owner_user_id=user.id)
    response = {"rows_inserted": count}
    if refresh_error:
        response["warning"] = (
            "Upload succeeded, but post-upload materialization refresh failed. "
            "Data is saved; please retry refresh."
        )
        response["refresh_error"] = refresh_error
    return response


@router.post("/branch-stock-norm")
async def upload_branch_stock_norm(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, BRANCH_STOCK_NORM_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    branch_map = await _resolve_branch_ids(
        db,
        owner_user_id=user.id,
        branch_names=df["branch_name"].astype(str).tolist(),
    )
    records = df[BRANCH_STOCK_NORM_SPEC.required_columns].to_dict(orient="records")
    for r in records:
        branch_name = str(r.pop("branch_name")).strip()
        r["branch_id"] = branch_map[branch_name]
        r["owner_user_id"] = user.id
    count = await _replace_records(
        db,
        ProductBranch,
        records,
        owner_user_id=user.id,
    )
    refresh_error = await _refresh_materialized_safe(db, owner_user_id=user.id)
    response = {"rows_inserted": count}
    if refresh_error:
        response["warning"] = (
            "Upload succeeded, but post-upload materialization refresh failed. "
            "Data is saved; please retry refresh."
        )
        response["refresh_error"] = refresh_error
    return response


@router.post("/price-list")
async def upload_price_list(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, PRICE_LIST_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df[PRICE_LIST_SPEC.required_columns].copy()
    rows["date"] = rows["date"].apply(_to_python_date)
    records = rows.to_dict(orient="records")
    for r in records:
        r["owner_user_id"] = user.id
    count = await _replace_records(
        db, PriceList, records, owner_user_id=user.id
    )
    refresh_error = await _refresh_materialized_safe(db, owner_user_id=user.id)
    response = {"rows_inserted": count}
    if refresh_error:
        response["warning"] = (
            "Upload succeeded, but post-upload materialization refresh failed. "
            "Data is saved; please retry refresh."
        )
        response["refresh_error"] = refresh_error
    return response


@router.post("/historical-sales-monthly")
async def upload_historical_sales_monthly(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, HISTORICAL_SALES_MONTHLY_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df[HISTORICAL_SALES_MONTHLY_SPEC.required_columns].copy()
    rows["date"] = rows["date"].apply(_to_python_date)
    branch_map = await _resolve_branch_ids(
        db,
        owner_user_id=user.id,
        branch_names=rows["branch_name"].astype(str).tolist(),
    )

    products = {
        p.sku_id: p
        for p in (
            await db.execute(
                select(Product).where(Product.owner_user_id == user.id)
            )
        ).scalars().all()
    }
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == user.id))
    ).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for p in prices:
        prices_by_sku.setdefault(p.sku_id, []).append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    prepared: list[dict] = []
    row_errors: list[dict] = []
    for idx, row in rows.iterrows():
        sku_id = str(row["sku_id"])
        product = products.get(sku_id)
        if not product:
            row_errors.append(
                {
                    "type": "foreign_key_missing",
                    "row": int(idx),
                    "field": "sku_id",
                    "message": f"Product '{sku_id}' not found in product table",
                }
            )
            continue
        r_date: date = _to_python_date(row["date"])
        closest_price = None
        for p in prices_by_sku.get(sku_id, []):
            if _to_python_date(p.date) <= r_date:
                closest_price = p
        fact_qty = float(row["fact_quantity_in_mc"])
        target_qty = float(row["target_quantity_in_mc"])
        fact_amount = (
            fact_qty * product.pieces_in_master_carton * closest_price.dsp
            if closest_price is not None
            else None
        )
        target_amount = (
            target_qty * product.pieces_in_master_carton * closest_price.dsp
            if closest_price is not None
            else None
        )
        prepared.append(
            {
                "sku_id": sku_id,
                "date": r_date,
                "branch_id": branch_map[str(row["branch_name"]).strip()],
                "fact_quantity_in_mc": fact_qty,
                "fact_gross_weight_kg": fact_qty * product.master_carton_gross_weight_kg,
                "fact_volume_cbm": fact_qty * product.master_carton_volume_cbm,
                "fact_amount_kzt": fact_amount,
                "target_quantity_in_mc": target_qty,
                "target_gross_weight_kg": target_qty * product.master_carton_gross_weight_kg,
                "target_volume_cbm": target_qty * product.master_carton_volume_cbm,
                "target_amount_kzt": target_amount,
                "past_available_stock": float(row["past_available_stock"]),
                "owner_user_id": user.id,
            }
        )

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=row_errors,
        )

    count = await _replace_records(
        db, HistoricalSalesMonthly, prepared, owner_user_id=user.id
    )
    refresh_error = await _refresh_materialized_safe(db, owner_user_id=user.id)
    response = {"rows_inserted": count}
    if refresh_error:
        response["warning"] = (
            "Upload succeeded, but post-upload materialization refresh failed. "
            "Data is saved; please retry refresh."
        )
        response["refresh_error"] = refresh_error
    return response


@router.post("/placed-orders")
async def upload_placed_orders(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, PLACED_ORDERS_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df[PLACED_ORDERS_SPEC.required_columns].copy()
    rows["creation_date"] = rows["creation_date"].apply(_to_python_date)
    rows["receival_date"] = rows["receival_date"].apply(_to_python_date)

    products = {
        p.sku_id: p
        for p in (
            await db.execute(
                select(Product).where(Product.owner_user_id == user.id)
            )
        ).scalars().all()
    }
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == user.id))
    ).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for p in prices:
        prices_by_sku.setdefault(p.sku_id, []).append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    prepared: list[dict] = []
    row_errors: list[dict] = []
    for idx, row in rows.iterrows():
        sku_id = str(row["sku_id"])
        product = products.get(sku_id)
        if not product:
            row_errors.append(
                {
                    "type": "foreign_key_missing",
                    "row": int(idx),
                    "field": "sku_id",
                    "message": f"Product '{sku_id}' not found in product table",
                }
            )
            continue
        c_date: date = _to_python_date(row["creation_date"])
        sorted_prices = prices_by_sku.get(sku_id, [])
        closest_price = None
        earliest_price = sorted_prices[0] if sorted_prices else None
        for p in sorted_prices:
            if _to_python_date(p.date) <= c_date:
                closest_price = p
        selected_price = closest_price if closest_price is not None else earliest_price
        qty = float(row["quantity_in_mc"])
        prepared.append(
            {
                "order_id": str(row["order_id"]),
                "sku_id": sku_id,
                "order_name": str(row["order_name"]),
                "creation_date": c_date,
                "receival_date": _to_python_date(row["receival_date"]),
                "quantity_in_mc": qty,
                "gross_weight_kg": qty * product.master_carton_gross_weight_kg,
                "volume_cbm": qty * product.master_carton_volume_cbm,
                "amount_kzt": (
                    qty * product.pieces_in_master_carton * selected_price.invoice_price
                    if selected_price is not None
                    else None
                ),
                "author": str(row["author"]) if not pd.isna(row["author"]) else None,
                "status": str(row["status"]),
                "owner_user_id": user.id,
            }
        )

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=row_errors,
        )

    count = await _replace_records(db, PlacedOrder, prepared, owner_user_id=user.id)
    await refresh_orders_aggregated(db, owner_user_id=user.id)
    return {"rows_inserted": count}

