from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1 import uploads
from app.core.database import get_db
from app.models.base import Base
from app.models.data_uploads import Branch, PriceList, Product
from app.models.user import UserRole


def _xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return buf.read()


@pytest_asyncio.fixture
async def upload_test_app(tmp_path):
    db_path = tmp_path / "uploads_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        session.add(
            Product(
                sku_id="SKU-001-ID",
                sku_code="SKU-001",
                mother_sku=None,
                barcode=None,
                sku_name="SKU One",
                sku_name_local=None,
                pieces_in_master_carton=10.0,
                master_carton_volume_cbm=1.0,
                master_carton_gross_weight_kg=2.0,
                master_carton_net_weight_kg=1.8,
                lead_time=5.0,
                source="import",
                general_stock_norm_days=30.0,
                status="активный",
                brand="BrandA",
                category="CategoryA",
                sub_category="SubA",
                sub_line="SublineA",
                owner_user_id=1,
            )
        )
        session.add(
            Branch(
                owner_user_id=1,
                branch_id="300001",
                branch_name="Алматы",
            )
        )
        session.add(
            PriceList(
                sku_id="SKU-001-ID",
                date=date(2025, 3, 1),
                invoice_price=100.0,
                dsp=120.0,
                owner_user_id=1,
            )
        )
        await session.commit()

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    async def _override_current_user():
        return SimpleNamespace(id=1, role=UserRole.PLANNER, is_active=True)

    app = FastAPI()
    app.include_router(uploads.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user

    try:
        yield app
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upload_historical_sales_monthly_accepts_aliases_and_mixed_dates(upload_test_app):
    excel_serial = (pd.Timestamp(date(2025, 3, 5)) - pd.Timestamp("1899-12-30")).days
    df = pd.DataFrame(
        {
            "sku_code": ["SKU-001", "SKU-001", "SKU-001", "SKU-001"],
            "branch_id": ["300001", "300001", "300001", "300001"],
            "date": [excel_serial, datetime(2025, 3, 6, 12, 0), "2025-03", "15/03/2025"],
            "fact_quantity_in_mc": [10, 11, 12, 13],
            "target_quantity_in_mc": [12, 13, 14, 15],
            "past_available_stock": [5, 5, 5, 5],
        }
    )

    async with AsyncClient(
        transport=ASGITransport(app=upload_test_app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/uploads/historical-sales-monthly",
            files={
                "file": (
                    "historical.xlsx",
                    _xlsx_bytes(df),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("rows_inserted") == 4


@pytest.mark.asyncio
async def test_upload_historical_sales_monthly_conflicting_aliases_returns_422(upload_test_app):
    df = pd.DataFrame(
        {
            "sku_id": ["SKU-001-ID"],
            "sku_code": ["WRONG-SKU-CODE"],
            "branch_name": ["Алматы"],
            "branch_id": ["300001"],
            "date": ["2025-03-10"],
            "fact_quantity_in_mc": [10],
            "target_quantity_in_mc": [12],
            "past_available_stock": [5],
        }
    )

    async with AsyncClient(
        transport=ASGITransport(app=upload_test_app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/uploads/historical-sales-monthly",
            files={
                "file": (
                    "historical_conflict.xlsx",
                    _xlsx_bytes(df),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", [])
    assert detail and detail[0]["field"] == "sku_id|sku_code"


@pytest.mark.asyncio
async def test_upload_branch_stock_norm_empty_current_stock_returns_422(upload_test_app):
    df = pd.DataFrame(
        {
            "sku_id": ["SKU-001-ID"],
            "branch_name": ["Алматы"],
            "current_stock": [""],
            "stock_norm": [10],
        }
    )

    async with AsyncClient(
        transport=ASGITransport(app=upload_test_app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/uploads/branch-stock-norm",
            files={
                "file": (
                    "branch_stock_norm.xlsx",
                    _xlsx_bytes(df),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", [])
    assert detail and detail[0]["field"] == "current_stock"
    assert "cannot be empty" in detail[0]["message"]


@pytest.mark.asyncio
async def test_upload_price_list_invalid_date_returns_422(upload_test_app):
    df = pd.DataFrame(
        {
            "sku_id": ["SKU-001-ID"],
            "date": ["bad-date-value"],
            "invoice_price": [100],
            "dsp": [120],
        }
    )
    async with AsyncClient(
        transport=ASGITransport(app=upload_test_app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/uploads/price-list",
            files={
                "file": (
                    "price_list_bad_date.xlsx",
                    _xlsx_bytes(df),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", [])
    assert detail and detail[0]["field"] == "date"


@pytest.mark.asyncio
async def test_upload_placed_orders_accepts_sku_code_alias(upload_test_app):
    df = pd.DataFrame(
        {
            "order_id": ["PO-1"],
            "sku_code": ["SKU-001"],
            "order_name": ["Order One"],
            "creation_date": ["2025-03-10"],
            "receival_date": ["2025-03-20"],
            "quantity_in_mc": [5],
            "author": ["tester"],
            "status": ["Created"],
        }
    )
    async with AsyncClient(
        transport=ASGITransport(app=upload_test_app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/uploads/placed-orders",
            files={
                "file": (
                    "placed_orders_alias.xlsx",
                    _xlsx_bytes(df),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("rows_inserted") == 1

