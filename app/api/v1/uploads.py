from io import BytesIO
from datetime import UTC, date, datetime
import logging
import asyncio
from pathlib import Path
import zipfile

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, inspect as sqla_inspect, select, tuple_
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
REFRESH_CANCEL_TIMEOUT_SECONDS = 10
_pending_refresh_tasks: dict[int, asyncio.Task] = {}
_pending_refresh_changed_keys: dict[int, set[tuple[str, str]]] = {}
_refresh_status_by_owner: dict[int, dict] = {}
TEMPLATE_FILENAMES = [
    "assortment_template.xlsx",
    "branch_stock_norm_template.xlsx",
    "orders_template.xlsx",
    "price_list_template.xlsx",
    "sales_template.xlsx",
]


async def _clear_latency_caches() -> None:
    from app.api.v1.dashboard import clear_dashboard_cache
    from app.api.v1.distribution import clear_distribution_cache
    from app.api.v1.inventory_health import clear_inventory_health_cache
    from app.api.v1.reports import clear_report_cache
    from app.core.response_cache import clear_response_cache

    await asyncio.gather(
        clear_dashboard_cache(),
        clear_distribution_cache(),
        clear_inventory_health_cache(),
        clear_report_cache(),
        clear_response_cache(),
    )


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


def _summarize_row_errors(row_errors: list[dict], max_sample_rows: int = 20) -> list[dict]:
    """
    Collapse identical row-level validation errors so the UI does not show the
    same message hundreds of times for bulk uploads.
    """
    grouped: dict[tuple[str, str, str], dict] = {}
    for error in row_errors:
        error_type = str(error.get("type", "validation_error"))
        field = str(error.get("field", ""))
        message = str(error.get("message", ""))
        key = (error_type, field, message)
        grouped_error = grouped.setdefault(
            key,
            {
                "type": error_type,
                "field": field,
                "message": message,
                "row": error.get("row"),
                "rows": [],
                "row_count": 0,
            },
        )
        grouped_error["row_count"] = int(grouped_error["row_count"]) + 1
        row = error.get("row")
        if row is not None and len(grouped_error["rows"]) < max_sample_rows:
            grouped_error["rows"].append(int(row))

    summarized: list[dict] = []
    for error in grouped.values():
        if int(error["row_count"]) == 1:
            error.pop("rows", None)
            error.pop("row_count", None)
        summarized.append(error)
    return summarized


def _owner_user_id(user: CurrentUser) -> int:
    try:
        return int(user.id)
    except Exception:
        identity = sqla_inspect(user).identity
        if identity and identity[0] is not None:
            return int(identity[0])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось определить идентификатор пользователя для операции загрузки",
        )


ASSORTMENT_SPEC = UploadSpec(
    [
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
            detail=f"Не удалось прочитать Excel-файл: {exc}",
        ) from exc


def _validate_columns(df: pd.DataFrame, spec: UploadSpec) -> list[dict]:
    errors: list[dict] = []
    missing = [c for c in spec.required_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Отсутствует один или несколько обязательных столбцов",
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
        raise ValueError("Значение даты пустое")

    if isinstance(value, (int, float)):
        # Excel serial date format (origin 1899-12-30)
        return pd.to_datetime(float(value), unit="D", origin="1899-12-30").date()

    raw = str(value).strip()
    if not raw:
        raise ValueError("Значение даты пустое")

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
        "Неподдерживаемый формат даты. Используйте Excel-дату, YYYY-MM-DD, YYYY-MM, DD/MM/YYYY или MM/YYYY"
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
                    detail="База данных временно занята. Повторите попытку загрузки.",
                ) from exc
        if last_exc is not None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="База данных временно занята. Повторите попытку загрузки.",
            ) from last_exc
    return 0


def _deduplicate_records_by_key(records: list[dict], key_fields: list[str]) -> list[dict]:
    deduped: dict[tuple, dict] = {}
    for record in records:
        key = tuple(record.get(field) for field in key_fields)
        deduped[key] = record
    return list(deduped.values())


async def _upsert_records_by_key(
    db: AsyncSession,
    model: type,
    records: list[dict],
    owner_user_id: int,
    key_fields: list[str],
) -> int:
    deduped_records = _deduplicate_records_by_key(records, key_fields)
    if not deduped_records:
        return 0

    async with UPLOAD_WRITE_LOCK:
        max_attempts = 5
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                key_values = [tuple(record.get(field) for field in key_fields) for record in deduped_records]
                delete_stmt = delete(model).where(model.owner_user_id == owner_user_id)
                if len(key_fields) == 1:
                    field = getattr(model, key_fields[0])
                    delete_stmt = delete_stmt.where(field.in_([key[0] for key in key_values]))
                else:
                    fields = [getattr(model, field_name) for field_name in key_fields]
                    delete_stmt = delete_stmt.where(tuple_(*fields).in_(key_values))
                await db.execute(delete_stmt)
                db.add_all([model(**record) for record in deduped_records])
                await db.commit()
                return len(deduped_records)
            except OperationalError as exc:
                await db.rollback()
                last_exc = exc
                if "database is locked" in str(exc).lower() and attempt < max_attempts:
                    await asyncio.sleep(0.3 * attempt)
                    continue
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="База данных временно занята. Повторите попытку загрузки.",
                ) from exc
        if last_exc is not None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="База данных временно занята. Повторите попытку загрузки.",
            ) from last_exc
    return 0


async def _refresh_materialized_safe(db: AsyncSession, owner_user_id: int) -> str | None:
    try:
        changed_keys = list(_pending_refresh_changed_keys.get(owner_user_id, set()))
        await refresh_all_materialized(
            db,
            owner_user_id=owner_user_id,
            changed_keys=changed_keys,
            stage_callback=lambda stage: _set_refresh_stage(owner_user_id, stage),
        )
        await _clear_latency_caches()
        return None
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        # Keep upload successful because base rows are already committed.
        logger.exception(
            "Materialized refresh failed after upload for owner_user_id=%s",
            owner_user_id,
        )
        await db.rollback()
        return str(exc)


def _set_refresh_stage(owner_user_id: int, stage: str) -> None:
    _refresh_status_by_owner[owner_user_id] = {
        **_refresh_status_by_owner.get(owner_user_id, {}),
        "in_progress": True,
        "stage": stage,
        "last_stage_updated_at": datetime.now(UTC).isoformat(),
    }


async def _refresh_materialized_background(owner_user_id: int) -> None:
    async with AsyncSessionLocal() as session:
        _refresh_status_by_owner[owner_user_id] = {
            **_refresh_status_by_owner.get(owner_user_id, {}),
            "in_progress": True,
            "stage": "fast_baseline_forecast",
            "last_started_at": datetime.now(UTC).isoformat(),
            "last_error": None,
        }
        try:
            error = await _refresh_materialized_safe(session, owner_user_id=owner_user_id)
        except asyncio.CancelledError:
            _refresh_status_by_owner[owner_user_id] = {
                **_refresh_status_by_owner.get(owner_user_id, {}),
                "in_progress": False,
                "stage": "cancelled_for_upload",
                "last_error": None,
                "last_completed_at": datetime.now(UTC).isoformat(),
                "pending_changed_keys_count": len(
                    _pending_refresh_changed_keys.get(owner_user_id, set())
                ),
            }
            raise
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
            "pending_changed_keys_count": 0 if error is None else len(
                _pending_refresh_changed_keys.get(owner_user_id, set())
            ),
        }


async def _debounced_materialized_refresh(owner_user_id: int) -> None:
    try:
        await asyncio.sleep(REFRESH_DEBOUNCE_SECONDS)
        await _refresh_materialized_background(owner_user_id)
    finally:
        current = _pending_refresh_tasks.get(owner_user_id)
        if current is asyncio.current_task():
            _pending_refresh_tasks.pop(owner_user_id, None)


async def _cancel_pending_refresh_for_upload(owner_user_id: int) -> None:
    existing = _pending_refresh_tasks.get(owner_user_id)
    if not existing or existing.done():
        return
    existing.cancel()
    done, _pending = await asyncio.wait({existing}, timeout=REFRESH_CANCEL_TIMEOUT_SECONDS)
    if existing in done:
        try:
            await existing
        except asyncio.CancelledError:
            pass
    else:
        _refresh_status_by_owner[owner_user_id] = {
            **_refresh_status_by_owner.get(owner_user_id, {}),
            "in_progress": True,
            "stage": "cancel_timeout_for_upload",
            "last_error": "Предыдущий пересчет данных все еще завершается. Повторите загрузку через несколько секунд.",
            "last_completed_at": datetime.now(UTC).isoformat(),
        }
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Предыдущий пересчет данных все еще завершается. Повторите загрузку через несколько секунд.",
        )
    _refresh_status_by_owner[owner_user_id] = {
        **_refresh_status_by_owner.get(owner_user_id, {}),
        "in_progress": False,
        "stage": "cancelled_for_upload",
        "last_error": None,
        "last_completed_at": datetime.now(UTC).isoformat(),
    }


def _schedule_materialized_refresh(owner_user_id: int) -> None:
    asyncio.create_task(_clear_latency_caches())
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
    bucket_count = len(_pending_refresh_changed_keys.get(owner_user_id, set()))
    stored_pending = int(
        _refresh_status_by_owner.get(owner_user_id, {}).get("pending_changed_keys_count", 0)
    )
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
        "pending_changed_keys_count": max(bucket_count, stored_pending),
    }


async def _expand_changed_keys(
    db: AsyncSession,
    owner_user_id: int,
    direct_keys: list[tuple[str, str]] | None = None,
    sku_codes: list[str] | None = None,
) -> list[tuple[str, str]]:
    collected: set[tuple[str, str]] = set()
    for sku_id, branch_id in direct_keys or []:
        s = str(sku_id).strip()
        b = str(branch_id).strip()
        if s and b:
            collected.add((s, b))
    normalized_sku_codes = sorted({str(s).strip() for s in (sku_codes or []) if str(s).strip()})
    if normalized_sku_codes:
        rows = (
            await db.execute(
                select(ProductBranch).where(
                    ProductBranch.owner_user_id == owner_user_id,
                    ProductBranch.sku_code.in_(normalized_sku_codes),
                )
            )
        ).scalars().all()
        for row in rows:
            collected.add((str(row.sku_code or "").strip(), str(row.branch_id).strip()))
    return sorted(collected)


def _accumulate_changed_keys(owner_user_id: int, changed_keys: list[tuple[str, str]]) -> None:
    bucket = _pending_refresh_changed_keys.setdefault(owner_user_id, set())
    for sku_code, branch_id in changed_keys:
        s = str(sku_code).strip()
        b = str(branch_id).strip()
        if s and b:
            bucket.add((s, b))


def _defer_materialized_refresh_until_placed_orders(owner_user_id: int) -> None:
    """Record pending keys but defer heavy pipelines until `/uploads/placed-orders`."""
    asyncio.create_task(_clear_latency_caches())
    _refresh_status_by_owner[owner_user_id] = {
        **_refresh_status_by_owner.get(owner_user_id, {}),
        "in_progress": False,
        "stage": "pending_placed_orders_upload",
        "pending_changed_keys_count": len(
            _pending_refresh_changed_keys.get(owner_user_id, set())
        ),
    }


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
                "message": "Отсутствует один или несколько обязательных столбцов",
                "columns": missing,
            }
        )
    if "sku_id" not in df.columns and "sku_code" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Должен быть заполнен один из столбцов: sku_id или sku_code",
                "columns": ["sku_id|sku_code"],
            }
        )
    if "branch_name" not in df.columns and "branch_id" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Должен присутствовать как минимум один из столбцов: branch_name или branch_id",
                "columns": ["branch_name|branch_id"],
            }
        )
    return errors


def _validate_branch_stock_norm_columns(df: pd.DataFrame) -> list[dict]:
    errors: list[dict] = []
    required_columns = [
        "branch_name",
        "current_stock",
        "stock_norm",
    ]
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Отсутствует один или несколько обязательных столбцов",
                "columns": missing,
            }
        )
    if "sku_id" not in df.columns and "sku_code" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Должен быть заполнен один из столбцов: sku_id или sku_code",
                "columns": ["sku_id|sku_code"],
            }
        )
    return errors


def _validate_price_list_columns(df: pd.DataFrame) -> list[dict]:
    errors: list[dict] = []
    required_columns = [
        "date",
        "invoice_price",
        "dsp",
    ]
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Отсутствует один или несколько обязательных столбцов",
                "columns": missing,
            }
        )
    if "sku_id" not in df.columns and "sku_code" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Должен быть заполнен один из столбцов: sku_id или sku_code",
                "columns": ["sku_id|sku_code"],
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
                "message": "Отсутствует один или несколько обязательных столбцов",
                "columns": missing,
            }
        )
    if "sku_id" not in df.columns and "sku_code" not in df.columns:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Должен быть заполнен один из столбцов: sku_id или sku_code",
                "columns": ["sku_id|sku_code"],
            }
        )
    return errors


def _validate_assortment_columns(df: pd.DataFrame) -> list[dict]:
    errors: list[dict] = []
    required_columns = [
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
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        errors.append(
            {
                "type": "missing_columns",
                "message": "Отсутствует один или несколько обязательных столбцов",
                "columns": missing,
            }
        )
    return errors


@router.get("/download-excel-templates-zip/", include_in_schema=False)
@router.get("/download-excel-templates-zip")
async def download_excel_templates_zip(user: CurrentUser):
    _ = user
    templates_dir = Path(__file__).resolve().parents[3] / "tmp_uploads" / "upload_files_templates"
    missing_files = [name for name in TEMPLATE_FILENAMES if not (templates_dir / name).exists()]
    if missing_files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Не найдены один или несколько файлов шаблонов",
                "missing_files": missing_files,
            },
        )

    output = BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for filename in TEMPLATE_FILENAMES:
            file_path = templates_dir / filename
            zip_file.write(file_path, arcname=filename)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="template.zip"'},
    )


@router.post("/assortment")
async def upload_assortment(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    await _cancel_pending_refresh_for_upload(owner_user_id)
    df = _load_excel(file)
    errors = _validate_assortment_columns(df)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    records = df[ASSORTMENT_SPEC.required_columns].to_dict(orient="records")
    row_errors: list[dict] = []
    for idx, r in enumerate(records):
        sku_code = str(r.get("sku_code", "") or "").strip()
        if not sku_code:
            row_errors.append(
                _row_error(int(idx), "sku_code", "Поле sku_code не может быть пустым", error_type="required_field")
            )
            continue
        # sku_id remains required by Product model; during sku_code cutover we mirror sku_code.
        r["sku_id"] = sku_code
        r["sku_code"] = sku_code
        normalized_status = normalize_product_status(str(r.get("status", "")))
        if normalized_status is None:
            normalized_status = "новый"
        r["status"] = normalized_status
        r["source"] = normalize_source_value(r.get("source"))
        r["owner_user_id"] = owner_user_id
    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_summarize_row_errors(row_errors),
        )
    count = await _upsert_records_by_key(
        db,
        Product,
        records,
        owner_user_id=owner_user_id,
        key_fields=["sku_code"],
    )
    changed_keys = await _expand_changed_keys(
        db,
        owner_user_id=owner_user_id,
        sku_codes=[str(r.get("sku_code", "")) for r in records],
    )
    _accumulate_changed_keys(owner_user_id, changed_keys)
    _defer_materialized_refresh_until_placed_orders(owner_user_id)
    return {
        "rows_inserted": count,
        "refresh_status": "pending_placed_orders_upload",
    }


@router.post("/branch-stock-norm")
async def upload_branch_stock_norm(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    await _cancel_pending_refresh_for_upload(owner_user_id)
    df = _load_excel(file)
    errors = _validate_branch_stock_norm_columns(df)
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
                    "Поле current_stock не может быть пустым",
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
                        "Поле current_stock должно быть числом",
                    )
                )
        if pd.isna(stock_norm) or str(stock_norm).strip() == "":
            row_errors.append(
                _row_error(
                    int(idx),
                    "stock_norm",
                    "Поле stock_norm не может быть пустым",
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
                        "Поле stock_norm должно быть числом",
                    )
                )
    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_summarize_row_errors(row_errors),
        )
    branch_map = await _resolve_branch_ids(
        db,
        owner_user_id=owner_user_id,
        branch_names=df["branch_name"].astype(str).tolist(),
    )
    products = (
        await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
    ).scalars().all()
    products_by_id = {str(p.sku_id).strip(): p for p in products}
    products_by_code = {str(p.sku_code).strip(): p for p in products}
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "sku_id": row.get("sku_id") if "sku_id" in df.columns else None,
                "sku_code": row.get("sku_code") if "sku_code" in df.columns else None,
                "branch_name": row.get("branch_name"),
                "current_stock": row.get("current_stock"),
                "stock_norm": row.get("stock_norm"),
            }
        )
    row_errors: list[dict] = []
    for idx, r in enumerate(records):
        branch_name = str(localize_branch_name(str(r.pop("branch_name")).strip()) or "").strip()
        r["branch_id"] = branch_map[branch_name]
        raw_sku_id = r.get("sku_id")
        raw_sku_code = r.get("sku_code")
        sku_id_value = "" if pd.isna(raw_sku_id) else str(raw_sku_id or "").strip()
        sku_code_value = "" if pd.isna(raw_sku_code) else str(raw_sku_code or "").strip()
        raw_sku = sku_id_value or sku_code_value
        if not raw_sku:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    "Должен быть заполнен sku_id или sku_code",
                    error_type="required_field",
                )
            )
            continue
        product = products_by_id.get(raw_sku) or products_by_code.get(raw_sku)
        if product is None:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id",
                    f"Товар не найден для sku_id/sku_code '{raw_sku}'",
                    error_type="foreign_key_missing",
                )
            )
            continue
        r["sku_id"] = str(product.sku_id).strip()
        r["sku_code"] = str(product.sku_code).strip()
        r["current_stock"] = float(r["current_stock"])
        r["stock_norm"] = float(r["stock_norm"])
        r["owner_user_id"] = owner_user_id
    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_summarize_row_errors(row_errors),
        )
    changed_keys = [
        (str(r.get("sku_code", "")).strip(), str(r.get("branch_id", "")).strip())
        for r in records
    ]
    _accumulate_changed_keys(owner_user_id, changed_keys)
    count = await _upsert_records_by_key(
        db,
        ProductBranch,
        records,
        owner_user_id=owner_user_id,
        key_fields=["sku_code", "branch_id"],
    )
    _defer_materialized_refresh_until_placed_orders(owner_user_id)
    return {
        "rows_inserted": count,
        "refresh_status": "pending_placed_orders_upload",
    }


@router.post("/price-list")
async def upload_price_list(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    await _cancel_pending_refresh_for_upload(owner_user_id)
    df = _load_excel(file)
    errors = _validate_price_list_columns(df)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
    rows = df.copy()
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
        raw_sku_id = row.get("sku_id") if "sku_id" in rows.columns else None
        raw_sku_code = row.get("sku_code") if "sku_code" in rows.columns else None
        sku_id_value = "" if pd.isna(raw_sku_id) else str(raw_sku_id).strip()
        sku_code_value = "" if pd.isna(raw_sku_code) else str(raw_sku_code).strip()
        raw_sku_value = sku_id_value or sku_code_value
        if not raw_sku_value:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    "Должен быть заполнен sku_id или sku_code",
                    error_type="required_field",
                )
            )
            continue
        product = products.get(raw_sku_value) or products_by_code.get(raw_sku_value)
        if product is None:
            row_errors.append(
                _row_error(
                    int(idx),
                    "sku_id|sku_code",
                    (
                        f"Товар не найден для sku_id='{sku_id_value}' и "
                        f"sku_code='{sku_code_value}'"
                    ),
                    error_type="foreign_key_missing",
                )
            )
            continue
        sku_id = str(product.sku_id).strip()
        try:
            invoice_price = float(row["invoice_price"])
        except Exception:
            row_errors.append(_row_error(int(idx), "invoice_price", "Поле должно быть числом"))
            continue
        try:
            dsp = float(row["dsp"])
        except Exception:
            row_errors.append(_row_error(int(idx), "dsp", "Поле должно быть числом"))
            continue
        records.append(
            {
                "sku_id": sku_id,
                "sku_code": str(product.sku_code).strip(),
                "date": parsed_date,
                "invoice_price": invoice_price,
                "dsp": dsp,
                "owner_user_id": owner_user_id,
            }
        )
    if row_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_summarize_row_errors(row_errors),
        )
    count = await _upsert_records_by_key(
        db,
        PriceList,
        records,
        owner_user_id=owner_user_id,
        key_fields=["sku_code", "date"],
    )
    changed_keys = await _expand_changed_keys(
        db,
        owner_user_id=owner_user_id,
        sku_codes=[str(r.get("sku_code", "")) for r in records],
    )
    _accumulate_changed_keys(owner_user_id, changed_keys)
    _defer_materialized_refresh_until_placed_orders(owner_user_id)
    return {
        "rows_inserted": count,
        "refresh_status": "pending_placed_orders_upload",
    }


@router.post("/historical-sales-monthly")
async def upload_historical_sales_monthly(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    await _cancel_pending_refresh_for_upload(owner_user_id)
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
                    "Должен быть заполнен sku_id или sku_code",
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
                        "Конфликтующие значения sku_id и sku_code в одной строке",
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
                        f"Товар не найден для sku_id='{sku_id_value}' и "
                        f"sku_code='{sku_code_value}'"
                    ),
                    error_type="foreign_key_missing",
                )
            )
            continue
        sku_id = product.sku_id

        raw_hub_name = row.get("hub_name") if "hub_name" in rows.columns else None
        # Backward compatibility: legacy sales files may not contain hub_name.
        if "hub_name" not in rows.columns:
            hub_name = "KZ-HUB"
        else:
            hub_name = "" if pd.isna(raw_hub_name) else str(raw_hub_name).strip()
            if not hub_name:
                hub_name = "KZ-HUB"

        raw_branch_name = row.get("branch_name") if "branch_name" in rows.columns else None
        raw_branch_id = row.get("branch_id") if "branch_id" in rows.columns else None
        branch_name_value = None if pd.isna(raw_branch_name) else str(raw_branch_name).strip()
        branch_id_value = None if pd.isna(raw_branch_id) else str(raw_branch_id).strip()
        is_hub_row = not branch_name_value and not branch_id_value

        resolved_branch_id_from_name = None
        if branch_name_value:
            localized_name = str(
                localize_branch_name(branch_name_value) or branch_name_value
            ).strip()
            resolved_branch_id_from_name = branch_map.get(localized_name)
        resolved_branch_id_from_id = (
            branch_id_value if branch_id_value and branch_id_value in branch_id_to_name else None
        )

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
                        "Конфликтующие значения branch_name и branch_id в одной строке",
                        error_type="conflict",
                    )
                )
                continue

        branch_id = "" if is_hub_row else (resolved_branch_id_from_name or resolved_branch_id_from_id)
        if not is_hub_row and branch_id is None:
            row_errors.append(
                _row_error(
                    int(idx),
                    "branch_name|branch_id",
                    "Филиал не найден по переданным branch_name/branch_id",
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
            raw_fact_qty = row.get("fact_quantity_in_mc")
            if is_hub_row and (pd.isna(raw_fact_qty) or str(raw_fact_qty).strip() == ""):
                fact_qty = 0.0
            else:
                fact_qty = float(raw_fact_qty)
        except Exception:
            row_errors.append(_row_error(int(idx), "fact_quantity_in_mc", "Поле должно быть числом"))
            continue
        try:
            raw_target_qty = row.get("target_quantity_in_mc")
            if is_hub_row and (pd.isna(raw_target_qty) or str(raw_target_qty).strip() == ""):
                target_qty = 0.0
            else:
                target_qty = float(raw_target_qty)
        except Exception:
            row_errors.append(_row_error(int(idx), "target_quantity_in_mc", "Поле должно быть числом"))
            continue
        try:
            past_available_stock = float(row["past_available_stock"])
        except Exception:
            row_errors.append(_row_error(int(idx), "past_available_stock", "Поле должно быть числом"))
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
                "sku_code": str(product.sku_code).strip(),
                "hub_name": hub_name,
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
            detail=_summarize_row_errors(row_errors),
        )

    count = await _upsert_records_by_key(
        db,
        HistoricalSalesMonthly,
        prepared,
        owner_user_id=owner_user_id,
        key_fields=["sku_code", "branch_id", "hub_name", "date"],
    )
    changed_keys = [
        (str(r.get("sku_code", "")).strip(), str(r.get("branch_id", "")).strip())
        for r in prepared
    ]
    _accumulate_changed_keys(owner_user_id, changed_keys)
    _defer_materialized_refresh_until_placed_orders(owner_user_id)
    return {
        "rows_inserted": count,
        "refresh_status": "pending_placed_orders_upload",
    }


@router.post("/placed-orders")
async def upload_placed_orders(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    owner_user_id = _owner_user_id(user)
    await _cancel_pending_refresh_for_upload(owner_user_id)
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
                    "Должен быть заполнен sku_id или sku_code",
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
                        "Конфликтующие значения sku_id и sku_code в одной строке",
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
                        f"Товар не найден для sku_id='{sku_id_value}' и "
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
            row_errors.append(_row_error(int(idx), "quantity_in_mc", "Поле должно быть числом"))
            continue
        prepared.append(
            {
                "order_id": str(row["order_id"]),
                "sku_id": sku_id,
                "sku_code": str(product.sku_code).strip(),
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
            detail=_summarize_row_errors(row_errors),
        )

    count = await _upsert_records_by_key(
        db,
        PlacedOrder,
        prepared,
        owner_user_id=owner_user_id,
        key_fields=["order_id", "sku_code"],
    )
    changed_keys = await _expand_changed_keys(
        db,
        owner_user_id=owner_user_id,
        sku_codes=[str(r.get("sku_code", "")) for r in prepared],
    )
    _accumulate_changed_keys(owner_user_id, changed_keys)
    await refresh_orders_aggregated(db, owner_user_id=owner_user_id)
    _schedule_materialized_refresh(owner_user_id)
    return {
        "rows_inserted": count,
        "refresh_status": "scheduled",
    }

