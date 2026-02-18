from io import BytesIO
from datetime import date, datetime

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.models.data_uploads import (
    Product,
    ProductBranch,
    HistoricalSalesMonthly,
    PlacedOrder,
    PriceList,
)
from app.services.dp_report_pipeline import refresh_all_materialized

router = APIRouter(prefix="/uploads", tags=["uploads"])


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
        "branch_id",
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
        "branch_id",
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


async def _replace_records(db: AsyncSession, model: type, records: list[dict]) -> int:
    await db.execute(delete(model))
    db.add_all([model(**r) for r in records])
    await db.commit()
    return len(records)


@router.post("/assortment")
async def upload_assortment(
    db: DBSession,
    _user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, ASSORTMENT_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    count = await _replace_records(
        db, Product, df[ASSORTMENT_SPEC.required_columns].to_dict(orient="records")
    )
    await refresh_all_materialized(db)
    return {"rows_inserted": count}


@router.post("/branch-stock-norm")
async def upload_branch_stock_norm(
    db: DBSession,
    _user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, BRANCH_STOCK_NORM_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    count = await _replace_records(
        db,
        ProductBranch,
        df[BRANCH_STOCK_NORM_SPEC.required_columns].to_dict(orient="records"),
    )
    await refresh_all_materialized(db)
    return {"rows_inserted": count}


@router.post("/price-list")
async def upload_price_list(
    db: DBSession,
    _user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, PRICE_LIST_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df[PRICE_LIST_SPEC.required_columns].copy()
    rows["date"] = rows["date"].apply(_to_python_date)
    count = await _replace_records(db, PriceList, rows.to_dict(orient="records"))
    await refresh_all_materialized(db)
    return {"rows_inserted": count}


@router.post("/historical-sales-monthly")
async def upload_historical_sales_monthly(
    db: DBSession,
    _user: CurrentUser,
    file: UploadFile = File(...),
):
    df = _load_excel(file)
    errors = _validate_columns(df, HISTORICAL_SALES_MONTHLY_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df[HISTORICAL_SALES_MONTHLY_SPEC.required_columns].copy()
    rows["date"] = rows["date"].apply(_to_python_date)

    products = {
        p.sku_id: p for p in (await db.execute(select(Product))).scalars().all()
    }
    prices = (await db.execute(select(PriceList))).scalars().all()
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
                "branch_id": str(row["branch_id"]),
                "fact_quantity_in_mc": fact_qty,
                "fact_gross_weight_kg": fact_qty * product.master_carton_gross_weight_kg,
                "fact_volume_cbm": fact_qty * product.master_carton_volume_cbm,
                "fact_amount_kzt": fact_amount,
                "target_quantity_in_mc": target_qty,
                "target_gross_weight_kg": target_qty * product.master_carton_gross_weight_kg,
                "target_volume_cbm": target_qty * product.master_carton_volume_cbm,
                "target_amount_kzt": target_amount,
                "past_available_stock": float(row["past_available_stock"]),
            }
        )

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=row_errors,
        )

    count = await _replace_records(db, HistoricalSalesMonthly, prepared)
    await refresh_all_materialized(db)
    return {"rows_inserted": count}


@router.post("/placed-orders")
async def upload_placed_orders(
    db: DBSession,
    _user: CurrentUser,
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
        p.sku_id: p for p in (await db.execute(select(Product))).scalars().all()
    }
    prices = (await db.execute(select(PriceList))).scalars().all()
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
        closest_price = None
        for p in prices_by_sku.get(sku_id, []):
            if _to_python_date(p.date) <= c_date:
                closest_price = p
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
                    qty * product.pieces_in_master_carton * closest_price.invoice_price
                    if closest_price is not None
                    else None
                ),
                "status": str(row["status"]),
            }
        )

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=row_errors,
        )

    count = await _replace_records(db, PlacedOrder, prepared)
    return {"rows_inserted": count}

