from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    reports,
    supply_chain,
    datasets,
    uploads,
)
from app.core.config import settings
from app.core.database import engine
from app.models.base import Base


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup for MVP; replace with Alembic in production
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_owner_columns(conn)
        await _ensure_placed_orders_author_column(conn)
        await _ensure_dp_reports_columns(conn)
        if conn.dialect.name == "sqlite":
            await _ensure_product_owner_unique_sqlite(conn)
            await _ensure_numeric_health_index_columns_sqlite(conn)
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    lifespan=lifespan,
)

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


@app.get("/")
async def healthcheck() -> dict:
    return {"status": "ok"}

