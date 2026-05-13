from contextlib import asynccontextmanager
import json
import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import (
    assortment,
    auth,
    dashboard,
    dp_report,
    distribution,
    inventory_health,
    notifications,
    orders,
    product_filter_options,
    reports,
    supply_chain,
    datasets,
    uploads,
)
from app.core.branch_localization import localize_branch_name
from app.core.order_status import normalize_order_status
from app.core.product_status import normalize_product_status
from app.core.config import settings
from app.core.database import engine
from app.core.response_cache import CachedResponse, response_cache
from app.models.base import Base

logger = logging.getLogger(__name__)


async def _column_exists(conn, table_name: str, column_name: str) -> bool:
    dialect = conn.dialect.name
    if dialect == "sqlite":
        rows = (await conn.execute(text(f"PRAGMA table_info({table_name})"))).fetchall()
        return any(r[1] == column_name for r in rows)
    result = await conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    )
    return result.first() is not None


async def _default_owner_id(conn) -> int:
    row = (await conn.execute(text("SELECT MIN(id) FROM users"))).first()
    if row and row[0] is not None:
        return int(row[0])
    return 1


async def _ensure_product_owner_unique_sqlite(conn) -> None:
    rows = (await conn.execute(text("PRAGMA index_list(product)"))).fetchall()
    has_legacy_unique = False
    has_owner_unique = False

    for row in rows:
        index_name = row[1]
        is_unique = bool(row[2])
        if not is_unique:
            continue
        cols = (await conn.execute(text(f"PRAGMA index_info({index_name})"))).fetchall()
        col_names = [c[2] for c in cols]
        if col_names == ["sku_id"]:
            has_legacy_unique = True
        if col_names == ["owner_user_id", "sku_id"]:
            has_owner_unique = True

    if not has_legacy_unique and has_owner_unique:
        return

    owner_id = await _default_owner_id(conn)
    await conn.execute(
        text(
            """
            CREATE TABLE product_new (
                id INTEGER PRIMARY KEY,
                sku_id VARCHAR(64) NOT NULL,
                sku_code VARCHAR(128) NOT NULL,
                mother_sku VARCHAR(64),
                barcode VARCHAR(64),
                sku_name VARCHAR(255) NOT NULL,
                sku_name_local VARCHAR(255),
                pieces_in_master_carton FLOAT NOT NULL,
                master_carton_volume_cbm FLOAT NOT NULL,
                master_carton_gross_weight_kg FLOAT NOT NULL,
                master_carton_net_weight_kg FLOAT NOT NULL,
                lead_time FLOAT NOT NULL,
                source VARCHAR(128) NOT NULL,
                general_stock_norm_days FLOAT NOT NULL,
                status VARCHAR(64) NOT NULL,
                brand VARCHAR(128) NOT NULL,
                category VARCHAR(128) NOT NULL,
                sub_category VARCHAR(128) NOT NULL,
                sub_line VARCHAR(128) NOT NULL,
                owner_user_id INTEGER NOT NULL,
                created_at DATETIME,
                updated_at DATETIME,
                UNIQUE(owner_user_id, sku_id)
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            INSERT INTO product_new (
                id, sku_id, sku_code, mother_sku, barcode, sku_name, sku_name_local,
                pieces_in_master_carton, master_carton_volume_cbm, master_carton_gross_weight_kg,
                master_carton_net_weight_kg, lead_time, source, general_stock_norm_days, status,
                brand, category, sub_category, sub_line, owner_user_id, created_at, updated_at
            )
            SELECT
                id, sku_id, sku_code, mother_sku, barcode, sku_name, sku_name_local,
                pieces_in_master_carton, master_carton_volume_cbm, master_carton_gross_weight_kg,
                master_carton_net_weight_kg, lead_time, source, general_stock_norm_days, status,
                brand, category, sub_category, sub_line, COALESCE(owner_user_id, :owner_id),
                created_at, updated_at
            FROM product
            """
        ),
        {"owner_id": owner_id},
    )
    await conn.execute(text("DROP TABLE product"))
    await conn.execute(text("ALTER TABLE product_new RENAME TO product"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_product_sku_id ON product (sku_id)"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_product_owner_user_id ON product (owner_user_id)")
    )


async def _ensure_owner_columns(conn) -> None:
    tables = [
        "product",
        "product_branch",
        "price_list",
        "historical_sales_monthly",
        "placed_orders",
        "forecast_sales_monthly",
        "forecast_orders",
        "inventory_health",
        "branch_distribution",
        "dp_report",
        "orders_aggregated",
        "distribution_branch_adjustments",
        "distribution_sku_adjustments",
        "dp_report_forecast_overrides",
        "forecast_inference_cache",
    ]
    owner_id = await _default_owner_id(conn)
    for table_name in tables:
        if not await _column_exists(conn, table_name, "owner_user_id"):
            await conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN owner_user_id INTEGER")
            )
        await conn.execute(
            text(
                f"UPDATE {table_name} SET owner_user_id = :owner_id WHERE owner_user_id IS NULL"
            ),
            {"owner_id": owner_id},
        )


async def _ensure_sku_code_columns_and_backfill(conn) -> None:
    tables_with_sku = [
        "product_branch",
        "price_list",
        "historical_sales_monthly",
        "placed_orders",
        "forecast_sales_monthly",
        "forecast_orders",
        "inventory_health",
        "dp_report",
        "distribution_sku_adjustments",
        "forecast_inference_cache",
    ]
    for table_name in tables_with_sku:
        if not await _column_exists(conn, table_name, "sku_code"):
            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN sku_code VARCHAR(128)"))
        await conn.execute(
            text(
                f"""
                UPDATE {table_name}
                SET sku_code = (
                    SELECT p.sku_code
                    FROM product p
                    WHERE p.owner_user_id = {table_name}.owner_user_id
                      AND p.sku_id = {table_name}.sku_id
                    LIMIT 1
                )
                WHERE (sku_code IS NULL OR sku_code = '')
                  AND sku_id IS NOT NULL
                """
            )
        )
        await conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{table_name}_sku_code ON {table_name} (sku_code)"
            )
        )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_product_owner_user_id_sku_code ON product (owner_user_id, sku_code)")
    )


async def _ensure_hub_name_on_historical_sales(conn) -> None:
    if not await _column_exists(conn, "historical_sales_monthly", "hub_name"):
        await conn.execute(text("ALTER TABLE historical_sales_monthly ADD COLUMN hub_name VARCHAR(255)"))
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_historical_sales_monthly_hub_name ON historical_sales_monthly (hub_name)"
        )
    )

    # Existing rows with branch data but no hub mapping are linked to KZ-HUB.
    await conn.execute(
        text(
            """
            UPDATE historical_sales_monthly
            SET hub_name = 'KZ-HUB'
            WHERE (hub_name IS NULL OR TRIM(hub_name) = '')
              AND branch_id IS NOT NULL
              AND TRIM(branch_id) != ''
            """
        )
    )

    # Removed: INSERT of synthetic hub-only rows with hub_name='KZ-HUB' at latest date.
    # Those rows were not from user uploads; real templates already carry hub_name.


async def _ensure_inventory_health_sku_code_column(conn) -> None:
    # Safety net: some legacy SQLite rebuild paths recreated inventory_health
    # without sku_code. Ensure column/index exist and backfill from product.
    if not await _column_exists(conn, "inventory_health", "sku_code"):
        await conn.execute(text("ALTER TABLE inventory_health ADD COLUMN sku_code VARCHAR(128)"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_inventory_health_sku_code ON inventory_health (sku_code)")
    )
    await conn.execute(
        text(
            """
            UPDATE inventory_health
            SET sku_code = (
                SELECT p.sku_code
                FROM product p
                WHERE p.owner_user_id = inventory_health.owner_user_id
                  AND p.sku_id = inventory_health.sku_id
                LIMIT 1
            )
            WHERE (sku_code IS NULL OR sku_code = '')
              AND sku_id IS NOT NULL
            """
        )
    )


async def _ensure_placed_orders_author_column(conn) -> None:
    if await _column_exists(conn, "placed_orders", "author"):
        return
    await conn.execute(text("ALTER TABLE placed_orders ADD COLUMN author VARCHAR(255)"))


async def _ensure_dp_reports_columns(conn) -> None:
    if not await _column_exists(conn, "dp_reports", "product_filter_json"):
        await conn.execute(text("ALTER TABLE dp_reports ADD COLUMN product_filter_json VARCHAR(8000)"))
    if not await _column_exists(conn, "dp_reports", "branch_filter_json"):
        await conn.execute(text("ALTER TABLE dp_reports ADD COLUMN branch_filter_json VARCHAR(8000)"))
    if not await _column_exists(conn, "dp_reports", "planning_month"):
        await conn.execute(text("ALTER TABLE dp_reports ADD COLUMN planning_month DATE"))
    if not await _column_exists(conn, "dp_report_forecast_overrides", "adjustment_reason"):
        await conn.execute(
            text("ALTER TABLE dp_report_forecast_overrides ADD COLUMN adjustment_reason VARCHAR(2000)")
        )
    # Backfill legacy reports where planning_month is null to avoid period validation errors.
    await conn.execute(
        text(
            """
            UPDATE dp_reports
            SET planning_month = COALESCE(
                date(date_to, 'start of month'),
                date(date_from, 'start of month')
            )
            WHERE planning_month IS NULL
            """
        )
    )


async def _sqlite_column_type(conn, table_name: str, column_name: str) -> str | None:
    rows = (await conn.execute(text(f"PRAGMA table_info({table_name})"))).fetchall()
    for r in rows:
        if r[1] == column_name:
            return str(r[2] or "").upper()
    return None


async def _ensure_numeric_health_index_columns_sqlite(conn) -> None:
    # Enforce numeric-only health index columns by rebuilding legacy tables.
    ih_type = await _sqlite_column_type(conn, "inventory_health", "health_index")
    bd_type = await _sqlite_column_type(conn, "branch_distribution", "branch_health_index")
    ih_is_numeric = ih_type in {"REAL", "FLOAT", "DOUBLE", "NUMERIC"}
    bd_is_numeric = bd_type in {"REAL", "FLOAT", "DOUBLE", "NUMERIC"}
    if ih_is_numeric and bd_is_numeric:
        return

    await conn.execute(text("DROP TABLE IF EXISTS inventory_health_new"))
    await conn.execute(text("DROP TABLE IF EXISTS branch_distribution_new"))

    await conn.execute(
        text(
            """
            CREATE TABLE inventory_health_new (
                id INTEGER PRIMARY KEY,
                sku_id VARCHAR(64) NOT NULL,
                branch_id VARCHAR(64) NOT NULL,
                date DATE NOT NULL,
                sales_quantity_in_mc FLOAT NOT NULL,
                sales_gross_weight_kg FLOAT NOT NULL,
                sales_volume_cbm FLOAT NOT NULL,
                sales_amount_kzt FLOAT NOT NULL,
                total_sales_share FLOAT NOT NULL,
                available_stock FLOAT NOT NULL,
                average_f3m_quantity_in_mc FLOAT NOT NULL,
                dsp FLOAT NOT NULL,
                available_stock_days FLOAT NOT NULL,
                stock_norm_days FLOAT NOT NULL,
                overstock FLOAT NOT NULL,
                understock FLOAT NOT NULL,
                stock_out FLOAT NOT NULL,
                category VARCHAR(8) NOT NULL,
                health_index FLOAT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE TABLE branch_distribution_new (
                id INTEGER PRIMARY KEY,
                branch_id VARCHAR(64) NOT NULL,
                available_quantity_in_mc FLOAT NOT NULL,
                available_volume_cbm FLOAT NOT NULL,
                available_gross_weight_kg FLOAT NOT NULL,
                available_amount_kzt FLOAT NOT NULL,
                recommended_quantity_in_mc FLOAT NOT NULL,
                recommended_volume_cbm FLOAT NOT NULL,
                recommended_gross_weight_kg FLOAT NOT NULL,
                recommended_amount_kzt FLOAT NOT NULL,
                branch_health_index FLOAT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
    )

    # Drop old legacy tables and replace with strict numeric schema tables.
    await conn.execute(text("DROP TABLE IF EXISTS inventory_health"))
    await conn.execute(text("DROP TABLE IF EXISTS branch_distribution"))
    await conn.execute(text("ALTER TABLE inventory_health_new RENAME TO inventory_health"))
    await conn.execute(text("ALTER TABLE branch_distribution_new RENAME TO branch_distribution"))

    # Recreate key indexes expected by the application.
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_inventory_health_id ON inventory_health (id)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_inventory_health_sku_id ON inventory_health (sku_id)")
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_inventory_health_branch_id ON inventory_health (branch_id)"
        )
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_inventory_health_date ON inventory_health (date)")
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_inventory_health_owner_user_id ON inventory_health (owner_user_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_branch_distribution_id ON branch_distribution (id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_branch_distribution_branch_id ON branch_distribution (branch_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_branch_distribution_owner_user_id ON branch_distribution (owner_user_id)"
        )
    )


async def _ensure_product_status_russian(conn) -> None:
    rows = (await conn.execute(text("SELECT id, status FROM product"))).fetchall()
    for row_id, raw_status in rows:
        normalized = normalize_product_status(raw_status)
        if normalized is None:
            normalized = "новый"
        if str(raw_status or "") == normalized:
            continue
        await conn.execute(
            text("UPDATE product SET status = :status WHERE id = :id"),
            {"status": normalized, "id": row_id},
        )


async def _ensure_order_status_russian(conn) -> None:
    order_tables = ["placed_orders", "orders_aggregated"]
    for table_name in order_tables:
        rows = (await conn.execute(text(f"SELECT id, status FROM {table_name}"))).fetchall()
        for row_id, raw_status in rows:
            normalized = normalize_order_status(raw_status)
            if normalized is None:
                normalized = "создан"
            if str(raw_status or "") == normalized:
                continue
            await conn.execute(
                text(f"UPDATE {table_name} SET status = :status WHERE id = :id"),
                {"status": normalized, "id": row_id},
            )


async def _ensure_branch_names_russian(conn) -> None:
    branch_rows = (await conn.execute(text("SELECT id, branch_name FROM branches"))).fetchall()
    for row_id, branch_name in branch_rows:
        localized = localize_branch_name(branch_name)
        if localized is None or str(branch_name or "") == localized:
            continue
        await conn.execute(
            text("UPDATE branches SET branch_name = :branch_name WHERE id = :id"),
            {"branch_name": localized, "id": row_id},
        )

    override_rows = (
        await conn.execute(text("SELECT id, branch_name FROM dp_report_forecast_overrides"))
    ).fetchall()
    for row_id, branch_name in override_rows:
        localized = localize_branch_name(branch_name)
        if localized is None or str(branch_name or "") == localized:
            continue
        await conn.execute(
            text(
                "UPDATE dp_report_forecast_overrides SET branch_name = :branch_name WHERE id = :id"
            ),
            {"branch_name": localized, "id": row_id},
        )

    report_rows = (
        await conn.execute(text("SELECT id, branch_filter_json FROM dp_reports"))
    ).fetchall()
    for row_id, branch_filter_json in report_rows:
        if not branch_filter_json:
            continue
        try:
            parsed = json.loads(str(branch_filter_json))
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        localized_list = [str(localize_branch_name(v) or str(v).strip()) for v in parsed]
        if localized_list == parsed:
            continue
        await conn.execute(
            text("UPDATE dp_reports SET branch_filter_json = :branch_filter_json WHERE id = :id"),
            {"branch_filter_json": json.dumps(localized_list, ensure_ascii=False), "id": row_id},
        )


async def _ensure_performance_indexes(conn) -> None:
    index_statements = [
        "CREATE INDEX IF NOT EXISTS ix_hsm_owner_date_branch_sku ON historical_sales_monthly (owner_user_id, date, branch_id, sku_code)",
        "CREATE INDEX IF NOT EXISTS ix_hsm_owner_branch_date ON historical_sales_monthly (owner_user_id, branch_id, date)",
        "CREATE INDEX IF NOT EXISTS ix_hsm_owner_sku_date ON historical_sales_monthly (owner_user_id, sku_code, date)",
        "CREATE INDEX IF NOT EXISTS ix_hsm_owner_hub_sku_date ON historical_sales_monthly (owner_user_id, hub_name, sku_code, date)",
        "CREATE INDEX IF NOT EXISTS ix_fsm_owner_date_branch_sku ON forecast_sales_monthly (owner_user_id, date, branch_id, sku_code)",
        "CREATE INDEX IF NOT EXISTS ix_fsm_owner_branch_sku_date ON forecast_sales_monthly (owner_user_id, branch_id, sku_code, date)",
        "CREATE INDEX IF NOT EXISTS ix_product_branch_owner_branch_sku ON product_branch (owner_user_id, branch_id, sku_code)",
        "CREATE INDEX IF NOT EXISTS ix_price_list_owner_sku_date ON price_list (owner_user_id, sku_code, date)",
        "CREATE INDEX IF NOT EXISTS ix_dist_branch_adj_owner_date_branch ON distribution_branch_adjustments (owner_user_id, planning_date, branch_id)",
        "CREATE INDEX IF NOT EXISTS ix_dist_sku_adj_owner_date_branch_sku ON distribution_sku_adjustments (owner_user_id, planning_date, branch_id, sku_code)",
        "CREATE INDEX IF NOT EXISTS ix_dp_overrides_owner_report_period ON dp_report_forecast_overrides (owner_user_id, report_id, period)",
        "CREATE INDEX IF NOT EXISTS ix_orders_owner_sku_receival ON placed_orders (owner_user_id, sku_code, receival_date)",
    ]
    for statement in index_statements:
        await conn.execute(text(statement))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup for MVP; replace with Alembic in production
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_owner_columns(conn)
        await _ensure_sku_code_columns_and_backfill(conn)
        await _ensure_hub_name_on_historical_sales(conn)
        await _ensure_placed_orders_author_column(conn)
        await _ensure_dp_reports_columns(conn)
        await _ensure_product_status_russian(conn)
        await _ensure_order_status_russian(conn)
        await _ensure_branch_names_russian(conn)
        if conn.dialect.name == "sqlite":
            await _ensure_product_owner_unique_sqlite(conn)
            await _ensure_numeric_health_index_columns_sqlite(conn)
        await _ensure_inventory_health_sku_code_column(conn)
        await _ensure_performance_indexes(conn)
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    lifespan=lifespan,
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start = time.perf_counter()
    cacheable_prefixes = (f"{settings.API_V1_PREFIX}/dashboard/", f"{settings.API_V1_PREFIX}/reports/")
    cacheable_get = request.method == "GET" and request.url.path.startswith(cacheable_prefixes)
    cache_key = None
    if cacheable_get:
        cache_key = (
            "http-response",
            request.headers.get("authorization", ""),
            request.headers.get("x-api-key", ""),
            request.url.path,
            request.url.query,
        )
        cached = await response_cache.get(cache_key)
        if cached is not None:
            response = Response(
                content=cached.body,
                status_code=cached.status_code,
                media_type=cached.media_type,
                headers=cached.headers,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            response.headers["X-Process-Time-Ms"] = str(round(elapsed_ms, 1))
            response.headers["X-Cache"] = "HIT"
            return response

    response = await call_next(request)
    if cache_key is not None and response.status_code == 200:
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "x-process-time-ms", "x-cache"}
        }
        cached_response = CachedResponse(
            status_code=response.status_code,
            media_type=response.media_type,
            headers=headers,
            body=body,
        )
        await response_cache.set(cache_key, cached_response)
        response = Response(
            content=body,
            status_code=cached_response.status_code,
            media_type=cached_response.media_type,
            headers=headers,
        )
        response.headers["X-Cache"] = "MISS"
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    response.headers["X-Process-Time-Ms"] = str(round(elapsed_ms, 1))
    if elapsed_ms >= 500.0:
        logger.info(
            "slow_request method=%s path=%s query=%s status=%s elapsed_ms=%s",
            request.method,
            request.url.path,
            request.url.query,
            response.status_code,
            round(elapsed_ms, 1),
        )
    return response

allow_all_cors = settings.BACKEND_CORS_ALLOW_ALL
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all_cors else settings.BACKEND_CORS_ORIGINS,
    allow_origin_regex=None if allow_all_cors else settings.BACKEND_CORS_ORIGIN_REGEX,
    allow_credentials=not allow_all_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(uploads.router, prefix=settings.API_V1_PREFIX)
app.include_router(dashboard.router, prefix=settings.API_V1_PREFIX)
app.include_router(reports.router, prefix=settings.API_V1_PREFIX)
app.include_router(dp_report.router, prefix=settings.API_V1_PREFIX)
app.include_router(assortment.router, prefix=settings.API_V1_PREFIX)
app.include_router(orders.router, prefix=settings.API_V1_PREFIX)
app.include_router(datasets.router, prefix=settings.API_V1_PREFIX)
app.include_router(supply_chain.router, prefix=settings.API_V1_PREFIX)
app.include_router(distribution.router, prefix=settings.API_V1_PREFIX)
app.include_router(inventory_health.router, prefix=settings.API_V1_PREFIX)
app.include_router(notifications.router, prefix=settings.API_V1_PREFIX)
app.include_router(product_filter_options.router, prefix=settings.API_V1_PREFIX)


@app.get("/")
async def healthcheck() -> dict:
    return {"status": "ok"}

