from io import BytesIO
from datetime import UTC, date, datetime
import logging
import asyncio

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy import delete, inspect as sqla_inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.core.branch_localization import localize_branch_name, normalize_branch_lookup
from app.core.database import AsyncSessionLocal
from app.core.order_status import normalize_order_status
from app.core.product_status import normalize_product_status
from app.core.source_normalization import normalize_source_value
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
UPLOAD_WRITE_LOCK = asyncio.Lock()
REFRESH_DEBOUNCE_SECONDS = 2
_pending_refresh_tasks: dict[int, asyncio.Task] = {}
_pending_refresh_changed_keys: dict[int, set[tuple[str, str]]] = {}
_refresh_status_by_owner: dict[int, dict] = {}


class UploadSpec:
    def __init__(self, required_columns: list[str]):
        self.required_columns = required_columns


def _row_error(row: int, field: str, message: str, error_type: str = "validation_error") -> dict:
    return {
        "type": error_type,
        "row": int(row),
        "field": field,
        "message": message,
    }


def _owner_user_id(user: CurrentUser) -> int:
    try:
        return int(user.id)
    except Exception:
        identity = sqla_inspect(user).identity
        if identity and identity[0] is not None:
            return int(identity[0])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to resolve user id for upload operation",
        )


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


def _parse_upload_date(value: object, *, allow_month_only: bool = True) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if pd.isna(value):
        raise ValueError("Date value is empty")

    if isinstance(value, (int, float)):
        # Excel serial date format (origin 1899-12-30)
        return pd.to_datetime(float(value), unit="D", origin="1899-12-30").date()

    raw = str(value).strip()
    if not raw:
        raise ValueError("Date value is empty")

    # ISO full date.
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass

    # ISO month format YYYY-MM
    if allow_month_only:
        try:
            yyyy, mm = raw.split("-")
            if len(yyyy) == 4 and len(mm) == 2:
                return date(int(yyyy), int(mm), 1)
        except Exception:
            pass

    # DD/MM/YYYY
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        pass

    # MM/YYYY -> month start
    if allow_month_only:
        try:
            mm, yyyy = raw.split("/")
            if len(mm) == 2 and len(yyyy) == 4:
                return date(int(yyyy), int(mm), 1)
        except Exception:
            pass

    # Excel serial passed as string.
    try:
        serial = float(raw)
        return pd.to_datetime(serial, unit="D", origin="1899-12-30").date()
    except Exception:
        pass

    raise ValueError(
        "Unsupported date format. Use Excel date, YYYY-MM-DD, YYYY-MM, DD/MM/YYYY, or MM/YYYY"
    )


def _to_python_date(value: object) -> date:
    return _parse_upload_date(value)


def _looks_like_month_year_encoded_as_january_days(values: list[date]) -> bool:
    # Some Excel files store month/year inputs like "2/2024" as 2024-01-02.
    # For historical monthly uploads, detect this pattern and remap day->month.
    if len(values) < 12:
        return False
    if not all(v.month == 1 and 1 <= v.day <= 12 for v in values):
        return False
    unique_days = {v.day for v in values}
    return len(unique_days) >= 6


def _normalize_historical_monthly_date(parsed: date, *, day_as_month_mode: bool) -> date:
    if day_as_month_mode and parsed.month == 1 and 1 <= parsed.day <= 12:
        return date(parsed.year, parsed.day, 1)
    # Canonical month bucket for historical monthly mart.
    return parsed.replace(day=1)


async def _replace_records(
    db: AsyncSession,
    model: type,
    records: list[dict],
    owner_user_id: int,
) -> int:
    async with UPLOAD_WRITE_LOCK:
        max_attempts = 5
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await db.execute(delete(model).where(model.owner_user_id == owner_user_id))
                db.add_all([model(**r) for r in records])
                await db.commit()
                return len(records)
            except OperationalError as exc:
                await db.rollback()
                last_exc = exc
                if "database is locked" in str(exc).lower() and attempt < max_attempts:
                    await asyncio.sleep(0.3 * attempt)
                    continue
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Database is temporarily busy. Please retry upload.",
                ) from exc
        if last_exc is not None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is temporarily busy. Please retry upload.",
            ) from last_exc
    return 0


async def _refresh_materialized_safe(db: AsyncSession, owner_user_id: int) -> str | None:
    try:
        changed_keys = list(_pending_refresh_changed_keys.get(owner_user_id, set()))
        await refresh_all_materialized(
            db,
            owner_user_id=owner_user_id,
            changed_keys=changed_keys,
        )
        return None
    except Exception as exc:
        # Keep upload successful because base rows are already committed.
        logger.exception(
            "Materialized refresh failed after upload for owner_user_id=%s",
            owner_user_id,
        )
        await db.rollback()
        return str(exc)


async def _refresh_materialized_background(owner_user_id: int) -> None:
    async with AsyncSessionLocal() as session:
        _refresh_status_by_owner[owner_user_id] = {
            **_refresh_status_by_owner.get(owner_user_id, {}),
            "in_progress": True,
            "stage": "materialized_refresh",
            "last_started_at": datetime.now(UTC).isoformat(),
            "last_error": None,
        }
        error = await _refresh_materialized_safe(session, owner_user_id=owner_user_id)
        if error:
            logger.warning(
                "Background materialized refresh failed for owner_user_id=%s: %s",
                owner_user_id,
                error,
            )
        else:
            _pending_refresh_changed_keys.pop(owner_user_id, None)
        _refresh_status_by_owner[owner_user_id] = {
            **_refresh_status_by_owner.get(owner_user_id, {}),
            "in_progress": False,
            "stage": "idle" if error is None else "failed",
            "last_error": error,
            "last_completed_at": datetime.now(UTC).isoformat(),
        }


async def _debounced_materialized_refresh(owner_user_id: int) -> None:
    try:
        await asyncio.sleep(REFRESH_DEBOUNCE_SECONDS)
        await _refresh_materialized_background(owner_user_id)
    finally:
        current = _pending_refresh_tasks.get(owner_user_id)
        if current is asyncio.current_task():
            _pending_refresh_tasks.pop(owner_user_id, None)


def _schedule_materialized_refresh(owner_user_id: int) -> None:
    _refresh_status_by_owner[owner_user_id] = {
        **_refresh_status_by_owner.get(owner_user_id, {}),
        "in_progress": False,
        "stage": "scheduled",
        "last_scheduled_at": datetime.now(UTC).isoformat(),
        "pending_changed_keys_count": len(
            _pending_refresh_changed_keys.get(owner_user_id, set())
        ),
    }
    existing = _pending_refresh_tasks.get(owner_user_id)
    if existing and not existing.done():
        existing.cancel()
    _pending_refresh_tasks[owner_user_id] = asyncio.create_task(
        _debounced_materialized_refresh(owner_user_id)
    )


def get_refresh_status(owner_user_id: int) -> dict:
    return {
        "in_progress": bool(
            _refresh_status_by_owner.get(owner_user_id, {}).get("in_progress", False)
        ),
        "stage": str(_refresh_status_by_owner.get(owner_user_id, {}).get("stage", "idle")),
        "last_error": _refresh_status_by_owner.get(owner_user_id, {}).get("last_error"),
        "last_started_at": _refresh_status_by_owner.get(owner_user_id, {}).get(
            "last_started_at"
        ),
        "last_completed_at": _refresh_status_by_owner.get(owner_user_id, {}).get(
            "last_completed_at"
        ),
        "last_scheduled_at": _refresh_status_by_owner.get(owner_user_id, {}).get(
            "last_scheduled_at"
        ),
        "pending_changed_keys_count": int(
            _refresh_status_by_owner.get(owner_user_id, {}).get(
                "pending_changed_keys_count", 0
            )
        ),
    }


async def _expand_changed_keys(
    db: AsyncSession,
    owner_user_id: int,
    direct_keys: list[tuple[str, str]] | None = None,
    sku_ids: list[str] | None = None,
) -> list[tuple[str, str]]:
    collected: set[tuple[str, str]] = set()
    for sku_id, branch_id in direct_keys or []:
        s = str(sku_id).strip()
        b = str(branch_id).strip()
        if s and b:
            collected.add((s, b))
    normalized_skus = sorted({str(s).strip() for s in (sku_ids or []) if str(s).strip()})
    if normalized_skus:
        rows = (
            await db.execute(
                select(ProductBranch).where(
                    ProductBranch.owner_user_id == owner_user_id,
                    ProductBranch.sku_id.in_(normalized_skus),
                )
            )
        ).scalars().all()
        for row in rows:
            collected.add((str(row.sku_id).strip(), str(row.branch_id).strip()))
    return sorted(collected)


def _accumulate_changed_keys(owner_user_id: int, changed_keys: list[tuple[str, str]]) -> None:
    bucket = _pending_refresh_changed_keys.setdefault(owner_user_id, set())
    for sku_id, branch_id in changed_keys:
        s = str(sku_id).strip()
        b = str(branch_id).strip()
        if s and b:
            bucket.add((s, b))


async def _resolve_branch_ids(
    db: AsyncSession,
    owner_user_id: int,
    branch_names: list[str],
) -> dict[str, str]:
    cleaned = sorted(
        {
            str(localize_branch_name(name) or str(name).strip())
            for name in branch_names
            if name and str(name).strip()
        }
    )
    if not cleaned:
        return {}

    existing_rows = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    by_name = {r.branch_name: r.branch_id for r in existing_rows}
    by_name_lookup = {normalize_branch_lookup(r.branch_name): r.branch_id for r in existing_rows}
    used_ids = {r.branch_id for r in existing_rows}

    next_idx = 100001
    while str(next_idx) in used_ids:
        next_idx += 1

    new_rows: list[Branch] = []
    for name in cleaned:
        if normalize_branch_lookup(name) in by_name_lookup:
            by_name[name] = by_name_lookup[normalize_branch_lookup(name)]
            continue
        while str(next_idx) in used_ids:
            next_idx += 1
        branch_id = str(next_idx)
        next_idx += 1
        used_ids.add(branch_id)
        by_name[name] = branch_id
        by_name_lookup[normalize_branch_lookup(name)] = branch_id
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


def _validate_historical_sales_columns(df: pd.DataFrame) -> list[dict]:
    errors: list[dict] = []
    required_metric_columns = [
        "date",
        "fact_quantity_in_mc",
        "target_quantity_in_mc",
        "past_available_stock",
    ]
    missing = [c for c in required_metric_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "One or more required columns are missing",
                "columns": missing,
            }
        )
    if "sku_id" not in df.columns and "sku_code" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "One of sku_id or sku_code must be provided",
                "columns": ["sku_id|sku_code"],
            }
        )
    if "branch_name" not in df.columns and "branch_id" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "One of branch_name or branch_id must be provided",
                "columns": ["branch_name|branch_id"],
            }
        )
    return errors


def _validate_placed_orders_columns(df: pd.DataFrame) -> list[dict]:
    errors: list[dict] = []
    required_columns = [
        "order_id",
        "order_name",
        "creation_date",
        "receival_date",
        "quantity_in_mc",
        "author",
        "status",
    ]
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "One or more required columns are missing",
                "columns": missing,
            }
        )
    if "sku_id" not in df.columns and "sku_code" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "One of sku_id or sku_code must be provided",
                "columns": ["sku_id|sku_code"],
            }
        )
    return errors


@router.post("/assortment")
async def upload_assortment(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    df = _load_excel(file)
    errors = _validate_columns(df, ASSORTMENT_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    records = df[ASSORTMENT_SPEC.required_columns].to_dict(orient="records")
    for r in records:
        normalized_status = normalize_product_status(str(r.get("status", "")))
        if normalized_status is None:
            normalized_status = "новый"
        r["status"] = normalized_status
        r["source"] = normalize_source_value(r.get("source"))
        r["owner_user_id"] = owner_user_id
    count = await _replace_records(db, Product, records, owner_user_id=owner_user_id)
    changed_keys = await _expand_changed_keys(
        db,
        owner_user_id=owner_user_id,
        sku_ids=[str(r.get("sku_id", "")) for r in records],
    )
    _accumulate_changed_keys(owner_user_id, changed_keys)
    return {
        "rows_inserted": count,
        "refresh_status": "deferred_until_orders_upload",
    }


@router.post("/branch-stock-norm")
async def upload_branch_stock_norm(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    df = _load_excel(file)
    errors = _validate_columns(df, BRANCH_STOCK_NORM_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    row_errors: list[dict] = []
    for idx, row in df.iterrows():
        current_stock = row.get("current_stock")
        stock_norm = row.get("stock_norm")
        if pd.isna(current_stock) or str(current_stock).strip() == "":
            row_errors.append(
                _row_error(
                    int(idx),
                    "current_stock",
                    "current_stock cannot be empty",
                    error_type="required_field",
                )
            )
        else:
            try:
                float(current_stock)
            except Exception:
                row_errors.append(
                    _row_error(
                        int(idx),
                        "current_stock",
                        "current_stock must be a numeric value",
                    )
                )
        if pd.isna(stock_norm) or str(stock_norm).strip() == "":
            row_errors.append(
                _row_error(
                    int(idx),
                    "stock_norm",
                    "stock_norm cannot be empty",
                    error_type="required_field",
                )
            )
        else:
            try:
                float(stock_norm)
            except Exception:
                row_errors.append(
                    _row_error(
                        int(idx),
                        "stock_norm",
                        "stock_norm must be a numeric value",
                    )
                )
    if row_errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=row_errors)
    branch_map = await _resolve_branch_ids(
        db,
        owner_user_id=owner_user_id,
        branch_names=df["branch_name"].astype(str).tolist(),
    )
    records = df[BRANCH_STOCK_NORM_SPEC.required_columns].to_dict(orient="records")
    for r in records:
        branch_name = str(localize_branch_name(str(r.pop("branch_name")).strip()) or "").strip()
        r["branch_id"] = branch_map[branch_name]
        r["current_stock"] = float(r["current_stock"])
        r["stock_norm"] = float(r["stock_norm"])
        r["owner_user_id"] = owner_user_id
    changed_keys = [
        (str(r.get("sku_id", "")).strip(), str(r.get("branch_id", "")).strip())
        for r in records
    ]
    _accumulate_changed_keys(owner_user_id, changed_keys)
    count = await _replace_records(
        db,
        ProductBranch,
        records,
        owner_user_id=owner_user_id,
    )
    return {
        "rows_inserted": count,
        "refresh_status": "deferred_until_orders_upload",
    }


@router.post("/price-list")
async def upload_price_list(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    df = _load_excel(file)
    errors = _validate_columns(df, PRICE_LIST_SPEC)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df[PRICE_LIST_SPEC.required_columns].copy()
    products = {
        p.sku_id: p
        for p in (
            await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
        ).scalars().all()
    }
    products_by_code = {str(p.sku_code).strip(): p for p in products.values()}
    row_errors: list[dict] = []
    records: list[dict] = []
    for idx, row in rows.iterrows():
        try:
            parsed_date = _parse_upload_date(row["date"])
        except Exception as exc:
            row_errors.append(_row_error(int(idx), "date", str(exc)))
            continue
        raw_sku_value = str(row["sku_id"]).strip()
        if not raw_sku_value:
            row_errors.append(
                _row_error(int(idx), "sku_id", "sku_id cannot be empty", error_type="required_field")
            )
            continue
        product = products.get(raw_sku_value) or products_by_code.get(raw_sku_value)
        if product is None:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id",
                    (
                        f"Product not found for sku_id='{raw_sku_value}'. "
                        "Provide a valid sku_id or sku_code in the sku_id column."
                    ),
                    error_type="foreign_key_missing",
                )
            )
            continue
        sku_id = str(product.sku_id).strip()
        try:
            invoice_price = float(row["invoice_price"])
        except Exception:
            row_errors.append(_row_error(int(idx), "invoice_price", "Must be numeric"))
            continue
        try:
            dsp = float(row["dsp"])
        except Exception:
            row_errors.append(_row_error(int(idx), "dsp", "Must be numeric"))
            continue
        records.append(
            {
                "sku_id": sku_id,
                "date": parsed_date,
                "invoice_price": invoice_price,
                "dsp": dsp,
                "owner_user_id": owner_user_id,
            }
        )
    if row_errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=row_errors)
    count = await _replace_records(
        db, PriceList, records, owner_user_id=owner_user_id
    )
    changed_keys = await _expand_changed_keys(
        db,
        owner_user_id=owner_user_id,
        sku_ids=[str(r.get("sku_id", "")) for r in records],
    )
    _accumulate_changed_keys(owner_user_id, changed_keys)
    return {
        "rows_inserted": count,
        "refresh_status": "deferred_until_orders_upload",
    }


@router.post("/historical-sales-monthly")
async def upload_historical_sales_monthly(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    df = _load_excel(file)
    errors = _validate_historical_sales_columns(df)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df.copy()

    products = {
        p.sku_id: p
        for p in (
            await db.execute(
                select(Product).where(Product.owner_user_id == owner_user_id)
            )
        ).scalars().all()
    }
    products_by_code = {str(p.sku_code).strip(): p for p in products.values()}

    existing_branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    branch_id_to_name = {
        str(b.branch_id).strip(): str(b.branch_name).strip() for b in existing_branches
    }

    branch_names_to_resolve: list[str] = []
    if "branch_name" in rows.columns:
        for _, row in rows.iterrows():
            raw_branch_name = row.get("branch_name")
            if pd.isna(raw_branch_name) or str(raw_branch_name).strip() == "":
                continue
            branch_names_to_resolve.append(
                str(localize_branch_name(str(raw_branch_name).strip()) or "").strip()
            )
    branch_map = await _resolve_branch_ids(
        db,
        owner_user_id=owner_user_id,
        branch_names=branch_names_to_resolve,
    )
    existing_branches = (
        await db.execute(select(Branch).where(Branch.owner_user_id == owner_user_id))
    ).scalars().all()
    branch_id_to_name = {
        str(b.branch_id).strip(): str(b.branch_name).strip() for b in existing_branches
    }
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for p in prices:
        prices_by_sku.setdefault(p.sku_id, []).append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    prepared: list[dict] = []
    row_errors: list[dict] = []
    parsed_dates_by_idx: dict[int, date] = {}
    parsed_dates: list[date] = []
    for idx, row in rows.iterrows():
        try:
            parsed = _parse_upload_date(row["date"])
            parsed_dates_by_idx[int(idx)] = parsed
            parsed_dates.append(parsed)
        except Exception as exc:
            row_errors.append(_row_error(int(idx), "date", str(exc)))
    day_as_month_mode = _looks_like_month_year_encoded_as_january_days(parsed_dates)

    for idx, row in rows.iterrows():
        raw_sku_id = row.get("sku_id") if "sku_id" in rows.columns else None
        raw_sku_code = row.get("sku_code") if "sku_code" in rows.columns else None
        sku_id_value = None if pd.isna(raw_sku_id) else str(raw_sku_id).strip()
        sku_code_value = None if pd.isna(raw_sku_code) else str(raw_sku_code).strip()

        resolved_from_id = products.get(sku_id_value) if sku_id_value else None
        resolved_from_code = products_by_code.get(sku_code_value) if sku_code_value else None

        if not sku_id_value and not sku_code_value:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    "Either sku_id or sku_code must be provided",
                    error_type="required_field",
                )
            )
            continue

        if sku_id_value and sku_code_value:
            if (
                resolved_from_id is None
                or resolved_from_code is None
                or resolved_from_id.sku_id != resolved_from_code.sku_id
            ):
                row_errors.append(
                    _row_error(
                        int(idx),
                        "sku_id|sku_code",
                        "Conflicting sku_id and sku_code values in the same row",
                        error_type="conflict",
                    )
                )
                continue

        product = resolved_from_id or resolved_from_code
        if not product:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    (
                        f"Product not found for sku_id='{sku_id_value}' and "
                        f"sku_code='{sku_code_value}'"
                    ),
                    error_type="foreign_key_missing",
                )
            )
            continue
        sku_id = product.sku_id

        raw_branch_name = row.get("branch_name") if "branch_name" in rows.columns else None
        raw_branch_id = row.get("branch_id") if "branch_id" in rows.columns else None
        branch_name_value = None if pd.isna(raw_branch_name) else str(raw_branch_name).strip()
        branch_id_value = None if pd.isna(raw_branch_id) else str(raw_branch_id).strip()

        resolved_branch_id_from_name = None
        if branch_name_value:
            localized_name = str(
                localize_branch_name(branch_name_value) or branch_name_value
            ).strip()
            resolved_branch_id_from_name = branch_map.get(localized_name)
        resolved_branch_id_from_id = (
            branch_id_value if branch_id_value and branch_id_value in branch_id_to_name else None
        )

        if not branch_name_value and not branch_id_value:
            row_errors.append(
                _row_error(
                    int(idx),
                    "branch_name|branch_id",
                    "Either branch_name or branch_id must be provided",
                    error_type="required_field",
                )
            )
            continue

        if branch_name_value and branch_id_value:
            if (
                resolved_branch_id_from_name is not None
                and resolved_branch_id_from_id is not None
                and resolved_branch_id_from_name != resolved_branch_id_from_id
            ):
                row_errors.append(
                    _row_error(
                        int(idx),
                        "branch_name|branch_id",
                        "Conflicting branch_name and branch_id values in the same row",
                        error_type="conflict",
                    )
                )
                continue

        branch_id = resolved_branch_id_from_name or resolved_branch_id_from_id
        if branch_id is None:
            row_errors.append(
                _row_error(
                    int(idx),
                    "branch_name|branch_id",
                    "Branch not found for provided branch_name/branch_id",
                    error_type="foreign_key_missing",
                )
            )
            continue

        parsed_date = parsed_dates_by_idx.get(int(idx))
        if parsed_date is None:
            continue
        r_date = _normalize_historical_monthly_date(
            parsed_date,
            day_as_month_mode=day_as_month_mode,
        )

        closest_price = None
        for p in prices_by_sku.get(sku_id, []):
            if _to_python_date(p.date) <= r_date:
                closest_price = p
        try:
            fact_qty = float(row["fact_quantity_in_mc"])
        except Exception:
            row_errors.append(_row_error(int(idx), "fact_quantity_in_mc", "Must be numeric"))
            continue
        try:
            target_qty = float(row["target_quantity_in_mc"])
        except Exception:
            row_errors.append(_row_error(int(idx), "target_quantity_in_mc", "Must be numeric"))
            continue
        try:
            past_available_stock = float(row["past_available_stock"])
        except Exception:
            row_errors.append(_row_error(int(idx), "past_available_stock", "Must be numeric"))
            continue

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
                "branch_id": branch_id,
                "fact_quantity_in_mc": fact_qty,
                "fact_gross_weight_kg": fact_qty * product.master_carton_gross_weight_kg,
                "fact_volume_cbm": fact_qty * product.master_carton_volume_cbm,
                "fact_amount_kzt": fact_amount,
                "target_quantity_in_mc": target_qty,
                "target_gross_weight_kg": target_qty * product.master_carton_gross_weight_kg,
                "target_volume_cbm": target_qty * product.master_carton_volume_cbm,
                "target_amount_kzt": target_amount,
                "past_available_stock": past_available_stock,
                "owner_user_id": owner_user_id,
            }
        )

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=row_errors,
        )

    count = await _replace_records(
        db, HistoricalSalesMonthly, prepared, owner_user_id=owner_user_id
    )
    changed_keys = [
        (str(r.get("sku_id", "")).strip(), str(r.get("branch_id", "")).strip())
        for r in prepared
    ]
    _accumulate_changed_keys(owner_user_id, changed_keys)
    return {
        "rows_inserted": count,
        "refresh_status": "deferred_until_orders_upload",
    }


@router.post("/placed-orders")
async def upload_placed_orders(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    df = _load_excel(file)
    errors = _validate_placed_orders_columns(df)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df.copy()

    products = {
        p.sku_id: p
        for p in (
            await db.execute(
                select(Product).where(Product.owner_user_id == owner_user_id)
            )
        ).scalars().all()
    }
    products_by_code = {str(p.sku_code).strip(): p for p in products.values()}
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for p in prices:
        prices_by_sku.setdefault(p.sku_id, []).append(p)
    for sku in prices_by_sku:
        prices_by_sku[sku].sort(key=lambda x: x.date)

    prepared: list[dict] = []
    row_errors: list[dict] = []
    for idx, row in rows.iterrows():
        raw_sku_id = row.get("sku_id") if "sku_id" in rows.columns else None
        raw_sku_code = row.get("sku_code") if "sku_code" in rows.columns else None
        sku_id_value = None if pd.isna(raw_sku_id) else str(raw_sku_id).strip()
        sku_code_value = None if pd.isna(raw_sku_code) else str(raw_sku_code).strip()

        resolved_from_id = products.get(sku_id_value) if sku_id_value else None
        resolved_from_code = products_by_code.get(sku_code_value) if sku_code_value else None

        if not sku_id_value and not sku_code_value:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    "Either sku_id or sku_code must be provided",
                    error_type="required_field",
                )
            )
            continue
        if sku_id_value and sku_code_value:
            if (
                resolved_from_id is None
                or resolved_from_code is None
                or resolved_from_id.sku_id != resolved_from_code.sku_id
            ):
                row_errors.append(
                    _row_error(
                        int(idx),
                        "sku_id|sku_code",
                        "Conflicting sku_id and sku_code values in the same row",
                        error_type="conflict",
                    )
                )
                continue

        product = resolved_from_id or resolved_from_code
        if not product:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    (
                        f"Product not found for sku_id='{sku_id_value}' and "
                        f"sku_code='{sku_code_value}'"
                    ),
                    error_type="foreign_key_missing",
                )
            )
            continue
        sku_id = product.sku_id
        try:
            c_date: date = _parse_upload_date(row["creation_date"])
        except Exception as exc:
            row_errors.append(_row_error(int(idx), "creation_date", str(exc)))
            continue
        try:
            receival_date: date = _parse_upload_date(row["receival_date"])
        except Exception as exc:
            row_errors.append(_row_error(int(idx), "receival_date", str(exc)))
            continue
        sorted_prices = prices_by_sku.get(sku_id, [])
        closest_price = None
        earliest_price = sorted_prices[0] if sorted_prices else None
        for p in sorted_prices:
            if _to_python_date(p.date) <= c_date:
                closest_price = p
        selected_price = closest_price if closest_price is not None else earliest_price
        try:
            qty = float(row["quantity_in_mc"])
        except Exception:
            row_errors.append(_row_error(int(idx), "quantity_in_mc", "Must be numeric"))
            continue
        prepared.append(
            {
                "order_id": str(row["order_id"]),
                "sku_id": sku_id,
                "order_name": str(row["order_name"]),
                "creation_date": c_date,
                "receival_date": receival_date,
                "quantity_in_mc": qty,
                "gross_weight_kg": qty * product.master_carton_gross_weight_kg,
                "volume_cbm": qty * product.master_carton_volume_cbm,
                "amount_kzt": (
                    qty * product.pieces_in_master_carton * selected_price.invoice_price
                    if selected_price is not None
                    else None
                ),
                "author": str(row["author"]) if not pd.isna(row["author"]) else None,
                "status": normalize_order_status(str(row["status"])) or "создан",
                "owner_user_id": owner_user_id,
            }
        )

    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=row_errors,
        )

    count = await _replace_records(db, PlacedOrder, prepared, owner_user_id=owner_user_id)
    changed_keys = await _expand_changed_keys(
        db,
        owner_user_id=owner_user_id,
        sku_ids=[str(r.get("sku_id", "")) for r in prepared],
    )
    _accumulate_changed_keys(owner_user_id, changed_keys)
    await refresh_orders_aggregated(db, owner_user_id=owner_user_id)
    _schedule_materialized_refresh(owner_user_id)
    return {
        "rows_inserted": count,
        "refresh_status": "scheduled",
    }

