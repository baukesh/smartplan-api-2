from datetime import date
from typing import List

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, select, update

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin
from app.core.order_status import (
    ORDER_STATUS_OPTIONS,
    ORDER_STATUS_OPTIONS_ORDERED,
    ORDER_STATUS_OPTIONS_ORDERED_DISPLAY,
    display_order_status,
    normalize_order_status,
)
from app.models.data_uploads import PlacedOrder, Product
from app.models.derived import OrdersAggregated
from app.services.orders_aggregation import refresh_orders_aggregated

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderRow(BaseModel):
    order_id: str
    creation_date: date
    receival_date: date | None = None
    total_quantity_in_mc: float
    total_amount_kzt: float
    status: str | None = None

    model_config = {"from_attributes": True}


class OrdersPage(BaseModel):
    items: list[OrderRow]
    total_items: int
    total_pages: int


class OrderStatusUpdateRow(BaseModel):
    order_id: str
    status: str


class OrderStatusUpdateRequest(BaseModel):
    updates: list[OrderStatusUpdateRow]


class OrderDetailsRow(BaseModel):
    sku_code: str
    sku_name: str
    quantity_in_mc: float
    gross_weight_kg: float | None = None
    volume_cbm: float | None = None
    amount_kzt: float | None = None


class OrderDetailsResponse(BaseModel):
    order_id: str
    order_name: str | None = None
    status: str | None = None
    creation_date: date
    receival_date: date
    author: str | None = None
    total_skus: int
    total_amount_kzt: float
    items: list[OrderDetailsRow]
    total_items: int
    total_pages: int


class OrderDetailsPatchRequest(BaseModel):
    order_id: str | None = None
    order_name: str | None = None
    receival_date: date | None = None


PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


def _base_aggregated_stmt(user: CurrentUser):
    stmt = select(OrdersAggregated)
    if not is_admin(user):
        stmt = stmt.where(OrdersAggregated.owner_user_id == user.id)
    return stmt


@router.get("/status-options", response_model=List[str])
async def get_order_status_options() -> list[str]:
    # Stable ordering for frontend dropdown rendering.
    return ORDER_STATUS_OPTIONS_ORDERED_DISPLAY


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be one of: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


async def _list_aggregated_orders(
    db: DBSession,
    user: CurrentUser,
    status_filter: str | None = None,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    page: int = 1,
    page_size: str = "10",
) -> OrdersPage:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    stmt = _base_aggregated_stmt(user)
    normalized_status_filter = normalize_order_status(status_filter) if status_filter else None
    if status_filter and normalized_status_filter is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported status value: {status_filter}",
        )
    if normalized_status_filter:
        stmt = stmt.where(OrdersAggregated.status == normalized_status_filter)
    if parsed_date_from:
        stmt = stmt.where(OrdersAggregated.creation_date >= parsed_date_from)
    if parsed_date_to:
        stmt = stmt.where(OrdersAggregated.creation_date <= parsed_date_to)
    size = _parse_page_size(page_size)
    stmt = stmt.order_by(OrdersAggregated.creation_date.desc())
    raw_rows = list((await db.execute(stmt)).scalars().all())
    rows = [
        OrderRow(
            order_id=row.order_id,
            creation_date=row.creation_date,
            receival_date=row.receival_date,
            total_quantity_in_mc=row.total_quantity_in_mc,
            total_amount_kzt=row.total_amount_kzt,
            status=display_order_status(row.status),
        )
        for row in raw_rows
    ]
    total_items = len(rows)
    if size is None:
        return OrdersPage(items=rows, total_items=total_items, total_pages=1 if total_items > 0 else 0)
    total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    offset = (page - 1) * size
    return OrdersPage(
        items=rows[offset : offset + size],
        total_items=total_items,
        total_pages=total_pages,
    )


@router.get("", response_model=OrdersPage, include_in_schema=False)
@router.get("/", response_model=OrdersPage)
async def list_orders(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrdersPage:
    return await _list_aggregated_orders(
        db=db,
        user=user,
        status_filter=status,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@router.get("/in-transit", response_model=OrdersPage)
async def list_in_transit_orders(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrdersPage:
    return await _list_aggregated_orders(
        db=db,
        user=user,
        status_filter=status or "в пути",
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@router.get("/completed", response_model=OrdersPage)
async def list_completed_orders(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrdersPage:
    return await _list_aggregated_orders(
        db=db,
        user=user,
        status_filter=status or "завершен",
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@router.patch("/status")
async def update_order_statuses(
    db: DBSession,
    user: CurrentUser,
    payload: OrderStatusUpdateRequest,
) -> dict:
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="updates cannot be empty",
        )
    invalid = [u.status for u in payload.updates if normalize_order_status(u.status) is None]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported status values: {sorted(set(invalid))}",
        )

    updated = 0
    for change in payload.updates:
        normalized_status = normalize_order_status(change.status)
        if normalized_status is None:
            continue
        stmt = update(PlacedOrder).where(PlacedOrder.order_id == change.order_id)
        if not is_admin(user):
            stmt = stmt.where(PlacedOrder.owner_user_id == user.id)
        result = await db.execute(stmt.values(status=normalized_status))
        updated += int(result.rowcount or 0)
    await db.commit()

    if not is_admin(user):
        await refresh_orders_aggregated(db, owner_user_id=user.id)
    else:
        owner_ids = {
            row[0]
            for row in (
                await db.execute(
                    select(PlacedOrder.owner_user_id)
                    .where(PlacedOrder.order_id.in_([u.order_id for u in payload.updates]))
                    .distinct()
                )
            ).all()
        }
        for owner_id in owner_ids:
            await refresh_orders_aggregated(db, owner_user_id=owner_id)

    return {"rows_updated": updated}


@router.get("/details", response_model=OrderDetailsResponse)
async def get_order_details(
    db: DBSession,
    user: CurrentUser,
    order_id: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrderDetailsResponse:
    stmt = select(PlacedOrder).where(PlacedOrder.order_id == order_id)
    if not is_admin(user):
        stmt = stmt.where(PlacedOrder.owner_user_id == user.id)
    order_rows = (await db.execute(stmt)).scalars().all()
    if not order_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    owner_user_id = order_rows[0].owner_user_id
    products = {
        p.sku_id: p
        for p in (
            await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
        ).scalars().all()
    }

    items: list[OrderDetailsRow] = []
    total_amount = 0.0
    for row in order_rows:
        prod = products.get(row.sku_id)
        if not prod:
            continue
        amount = float(row.amount_kzt or 0.0)
        total_amount += amount
        items.append(
            OrderDetailsRow(
                sku_code=prod.sku_code,
                sku_name=prod.sku_name,
                quantity_in_mc=float(row.quantity_in_mc or 0.0),
                gross_weight_kg=float(row.gross_weight_kg) if row.gross_weight_kg is not None else None,
                volume_cbm=float(row.volume_cbm) if row.volume_cbm is not None else None,
                amount_kzt=float(row.amount_kzt) if row.amount_kzt is not None else None,
            )
        )

    header = sorted(order_rows, key=lambda r: (r.creation_date, r.receival_date))[0]
    agg_stmt = select(OrdersAggregated).where(
        OrdersAggregated.order_id == order_id,
        OrdersAggregated.owner_user_id == owner_user_id,
    )
    aggregated = (await db.execute(agg_stmt)).scalars().first()
    size = _parse_page_size(page_size)
    total_items = len(items)
    if size is not None:
        offset = (page - 1) * size
        paged_items = items[offset : offset + size]
        total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    else:
        paged_items = items
        total_pages = 1 if total_items > 0 else 0

    return OrderDetailsResponse(
        order_id=header.order_id,
        order_name=header.order_name,
        status=display_order_status(aggregated.status if aggregated is not None else header.status),
        creation_date=header.creation_date,
        receival_date=header.receival_date,
        author=header.author,
        total_skus=len(items),
        total_amount_kzt=round(total_amount, 2),
        items=paged_items,
        total_items=total_items,
        total_pages=total_pages,
    )


@router.patch("/details")
async def patch_order_details(
    db: DBSession,
    user: CurrentUser,
    source_order_id: str = Query(...),
    payload: OrderDetailsPatchRequest = ...,
) -> dict:
    stmt = select(PlacedOrder).where(PlacedOrder.order_id == source_order_id)
    if not is_admin(user):
        stmt = stmt.where(PlacedOrder.owner_user_id == user.id)
    order_rows = (await db.execute(stmt)).scalars().all()
    if not order_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    owner_user_id = order_rows[0].owner_user_id
    target_order_id = payload.order_id.strip() if payload.order_id else source_order_id
    if target_order_id != source_order_id:
        exists_stmt = select(PlacedOrder.id).where(
            and_(
                PlacedOrder.order_id == target_order_id,
                PlacedOrder.owner_user_id == owner_user_id,
            )
        )
        if (await db.execute(exists_stmt)).first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Order '{target_order_id}' already exists",
            )

    values: dict = {}
    if payload.order_id is not None:
        values["order_id"] = target_order_id
    if payload.order_name is not None:
        values["order_name"] = payload.order_name
    if payload.receival_date is not None:
        values["receival_date"] = payload.receival_date
    if not values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update",
        )

    upd = update(PlacedOrder).where(PlacedOrder.order_id == source_order_id)
    if not is_admin(user):
        upd = upd.where(PlacedOrder.owner_user_id == user.id)
    result = await db.execute(upd.values(**values))
    await db.commit()

    await refresh_orders_aggregated(db, owner_user_id=owner_user_id)
    return {"rows_updated": int(result.rowcount or 0)}

