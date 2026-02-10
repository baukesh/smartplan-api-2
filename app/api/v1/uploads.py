from io import BytesIO

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DBSession, CurrentUser
from app.models.data_uploads import (
    Assortment,
    BranchStockNorm,
    HistoricalSalesMonthly,
    PlacedOrder,
    PriceList,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadSpec:
    def __init__(self, required_columns: list[str]):
        self.required_columns = required_columns


ASSORTMENT_SPEC = UploadSpec(
    [
        "sku_id",
        "sku_code",
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
    ]
)

BRANCH_STOCK_NORM_SPEC = UploadSpec(
    [
        "branch_id",
        "sku_id",
        "current_stock",
        "stock_norm_days",
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
        "branch_id",
        "date",
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
        "gross_weight_kg",
        "volume_cbm",
        "amount_kzt",
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
    count = await _save_records(db, df[ASSORTMENT_SPEC.required_columns], Assortment)
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
    count = await _save_records(
        db, df[BRANCH_STOCK_NORM_SPEC.required_columns], BranchStockNorm
    )
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
    count = await _save_records(db, df[PRICE_LIST_SPEC.required_columns], PriceList)
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
    count = await _save_records(
        db,
        df[HISTORICAL_SALES_MONTHLY_SPEC.required_columns],
        HistoricalSalesMonthly,
    )
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
    count = await _save_records(db, df[PLACED_ORDERS_SPEC.required_columns], PlacedOrder)
    return {"rows_inserted": count}

