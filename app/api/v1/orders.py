from datetime import date
from typing import List

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
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
from app.models.data_uploads import PlacedOrder, PriceList, Product
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
    filter_options: "OrdersFilterOptions"


class OrdersFilterOptions(BaseModel):
    order_id: list[str] = Field(default_factory=list)
    creation_date: list[str] = Field(default_factory=list)
    receival_date: list[str] = Field(default_factory=list)
    total_quantity_in_mc: list[str] = Field(default_factory=list)
    total_amount_kzt: list[str] = Field(default_factory=list)
    status: list[str] = Field(default_factory=list)


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
    filter_options: "OrderDetailsFilterOptions"


class OrderDetailsFilterOptions(BaseModel):
    sku_code: list[str] = Field(default_factory=list)
    sku_name: list[str] = Field(default_factory=list)
    quantity_in_mc: list[str] = Field(default_factory=list)
    gross_weight_kg: list[str] = Field(default_factory=list)
    volume_cbm: list[str] = Field(default_factory=list)
    amount_kzt: list[str] = Field(default_factory=list)


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
            detail="Параметр page_size должен быть одним из: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


def _normalize_order_id_input(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("#"):
        normalized = normalized[1:].strip()
    if normalized.startswith("№"):
        normalized = normalized[1:].strip()
    return normalized


def _parse_float_filters(
    values: list[str] | None,
    *,
    field_name: str,
) -> set[float] | None:
    if not values:
        return None
    parsed: set[float] = set()
    for raw in values:
        normalized = str(raw).strip()
        if not normalized:
            continue
        try:
            parsed.add(float(normalized))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Параметр {field_name} должен содержать числовые значения",
            ) from exc
    return parsed or None


def _parse_exact_date_filters(
    values: list[str] | None,
    *,
    field_name: str,
) -> set[date] | None:
    if not values:
        return None
    parsed: set[date] = set()
    for value in values:
        dt = parse_query_date(value, field_name=field_name)
        if dt is not None:
            parsed.add(dt)
    return parsed or None


async def _list_aggregated_orders(
    db: DBSession,
    user: CurrentUser,
    status_filter: list[str] | None = None,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    order_id: list[str] | None = None,
    creation_date: list[str] | None = None,
    receival_date: list[str] | None = None,
    total_quantity_in_mc: list[str] | None = None,
    total_amount_kzt: list[str] | None = None,
    page: int = 1,
    page_size: str = "10",
) -> OrdersPage:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    parsed_creation_dates = _parse_exact_date_filters(
        creation_date, field_name="creation_date"
    )
    parsed_receival_dates = _parse_exact_date_filters(
        receival_date, field_name="receival_date"
    )
    parsed_total_quantities = _parse_float_filters(
        total_quantity_in_mc,
        field_name="total_quantity_in_mc",
    )
    parsed_total_amounts = _parse_float_filters(
        total_amount_kzt,
        field_name="total_amount_kzt",
    )
    stmt = _base_aggregated_stmt(user)
    normalized_status_filters: set[str] | None = None
    if status_filter:
        normalized_status_filters = set()
        for raw_status in status_filter:
            normalized = normalize_order_status(raw_status)
            if normalized is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Неподдерживаемое значение status: {raw_status}",
                )
            normalized_status_filters.add(normalized)
    if parsed_date_from:
        stmt = stmt.where(OrdersAggregated.receival_date >= parsed_date_from)
    if parsed_date_to:
        stmt = stmt.where(OrdersAggregated.receival_date <= parsed_date_to)
    size = _parse_page_size(page_size)
    stmt = stmt.order_by(OrdersAggregated.creation_date.desc())
    raw_rows = list((await db.execute(stmt)).scalars().all())
    scoped_rows = raw_rows
    if normalized_status_filters:
        scoped_rows = [
            r for r in scoped_rows if str(r.status).strip() in normalized_status_filters
        ]
    filter_options = OrdersFilterOptions(
        order_id=sorted({str(r.order_id).strip() for r in scoped_rows if str(r.order_id).strip()}),
        creation_date=sorted(
            {r.creation_date.isoformat() for r in scoped_rows if r.creation_date is not None}
        ),
        receival_date=sorted(
            {r.receival_date.isoformat() for r in scoped_rows if r.receival_date is not None}
        ),
        total_quantity_in_mc=sorted(
            {str(round(float(r.total_quantity_in_mc or 0.0), 2)) for r in scoped_rows}
        ),
        total_amount_kzt=sorted(
            {str(round(float(r.total_amount_kzt or 0.0), 2)) for r in scoped_rows}
        ),
        status=sorted(
            {
                str(display_order_status(r.status) or "").strip()
                for r in scoped_rows
                if str(display_order_status(r.status) or "").strip()
            }
        ),
    )
    filtered_rows = scoped_rows
    if order_id:
        normalized_order_ids = {
            _normalize_order_id_input(v) for v in order_id if _normalize_order_id_input(v)
        }
        filtered_rows = [
            r for r in filtered_rows if str(r.order_id).strip() in normalized_order_ids
        ]
    if parsed_creation_dates:
        filtered_rows = [
            r for r in filtered_rows if r.creation_date in parsed_creation_dates
        ]
    if parsed_receival_dates:
        filtered_rows = [
            r
            for r in filtered_rows
            if r.receival_date is not None and r.receival_date in parsed_receival_dates
        ]
    if parsed_total_quantities:
        normalized_quantities = {round(v, 2) for v in parsed_total_quantities}
        filtered_rows = [
            r
            for r in filtered_rows
            if round(float(r.total_quantity_in_mc or 0.0), 2) in normalized_quantities
        ]
    if parsed_total_amounts:
        normalized_amounts = {round(v, 2) for v in parsed_total_amounts}
        filtered_rows = [
            r
            for r in filtered_rows
            if round(float(r.total_amount_kzt or 0.0), 2) in normalized_amounts
        ]

    rows = [
        OrderRow(
            order_id=row.order_id,
            creation_date=row.creation_date,
            receival_date=row.receival_date,
            total_quantity_in_mc=row.total_quantity_in_mc,
            total_amount_kzt=row.total_amount_kzt,
            status=display_order_status(row.status),
        )
        for row in filtered_rows
    ]
    total_items = len(rows)
    if size is None:
        return OrdersPage(
            items=rows,
            total_items=total_items,
            total_pages=1 if total_items > 0 else 0,
            filter_options=filter_options,
        )
    total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    offset = (page - 1) * size
    return OrdersPage(
        items=rows[offset : offset + size],
        total_items=total_items,
        total_pages=total_pages,
        filter_options=filter_options,
    )


@router.get("", response_model=OrdersPage, include_in_schema=False)
@router.get("/", response_model=OrdersPage)
async def list_orders(
    db: DBSession,
    user: CurrentUser,
    status: list[str] | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    order_id: list[str] | None = Query(None),
    creation_date: list[str] | None = Query(None),
    receival_date: list[str] | None = Query(None),
    total_quantity_in_mc: list[str] | None = Query(None),
    total_amount_kzt: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrdersPage:
    return await _list_aggregated_orders(
        db=db,
        user=user,
        status_filter=status,
        date_from=date_from,
        date_to=date_to,
        order_id=order_id,
        creation_date=creation_date,
        receival_date=receival_date,
        total_quantity_in_mc=total_quantity_in_mc,
        total_amount_kzt=total_amount_kzt,
        page=page,
        page_size=page_size,
    )


@router.get("/in-transit", response_model=OrdersPage)
async def list_in_transit_orders(
    db: DBSession,
    user: CurrentUser,
    status: list[str] | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    order_id: list[str] | None = Query(None),
    creation_date: list[str] | None = Query(None),
    receival_date: list[str] | None = Query(None),
    total_quantity_in_mc: list[str] | None = Query(None),
    total_amount_kzt: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrdersPage:
    return await _list_aggregated_orders(
        db=db,
        user=user,
        status_filter=status or ["в пути"],
        date_from=date_from,
        date_to=date_to,
        order_id=order_id,
        creation_date=creation_date,
        receival_date=receival_date,
        total_quantity_in_mc=total_quantity_in_mc,
        total_amount_kzt=total_amount_kzt,
        page=page,
        page_size=page_size,
    )


@router.get("/completed", response_model=OrdersPage)
async def list_completed_orders(
    db: DBSession,
    user: CurrentUser,
    status: list[str] | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    order_id: list[str] | None = Query(None),
    creation_date: list[str] | None = Query(None),
    receival_date: list[str] | None = Query(None),
    total_quantity_in_mc: list[str] | None = Query(None),
    total_amount_kzt: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrdersPage:
    return await _list_aggregated_orders(
        db=db,
        user=user,
        status_filter=status or ["завершен"],
        date_from=date_from,
        date_to=date_to,
        order_id=order_id,
        creation_date=creation_date,
        receival_date=receival_date,
        total_quantity_in_mc=total_quantity_in_mc,
        total_amount_kzt=total_amount_kzt,
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
            detail="Список updates не может быть пустым",
        )
    invalid = [u.status for u in payload.updates if normalize_order_status(u.status) is None]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Неподдерживаемые значения status: {sorted(set(invalid))}",
        )

    updated = 0
    for change in payload.updates:
        normalized_status = normalize_order_status(change.status)
        if normalized_status is None:
            continue
        normalized_order_id = _normalize_order_id_input(change.order_id)
        stmt = update(PlacedOrder).where(PlacedOrder.order_id == normalized_order_id)
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
                    .where(
                        PlacedOrder.order_id.in_(
                            [_normalize_order_id_input(u.order_id) for u in payload.updates]
                        )
                    )
                    .distinct()
                )
            ).all()
        }
        for owner_id in owner_ids:
            await refresh_orders_aggregated(db, owner_user_id=owner_id)

    return {"rows_updated": updated}


@router.get("/details/", response_model=OrderDetailsResponse, include_in_schema=False)
@router.get("/details", response_model=OrderDetailsResponse)
async def get_order_details(
    db: DBSession,
    user: CurrentUser,
    order_id: str = Query(...),
    sku_code: list[str] | None = Query(None),
    sku_name: list[str] | None = Query(None),
    quantity_in_mc: list[str] | None = Query(None),
    gross_weight_kg: list[str] | None = Query(None),
    volume_cbm: list[str] | None = Query(None),
    amount_kzt: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> OrderDetailsResponse:
    normalized_order_id = _normalize_order_id_input(order_id)
    stmt = select(PlacedOrder).where(PlacedOrder.order_id == normalized_order_id)
    if not is_admin(user):
        stmt = stmt.where(PlacedOrder.owner_user_id == user.id)
    order_rows = (await db.execute(stmt)).scalars().all()
    if not order_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заказ не найден")

    owner_user_id = order_rows[0].owner_user_id
    products = {
        str(p.sku_code).strip(): p
        for p in (
            await db.execute(select(Product).where(Product.owner_user_id == owner_user_id))
        ).scalars().all()
    }
    prices = (
        await db.execute(select(PriceList).where(PriceList.owner_user_id == owner_user_id))
    ).scalars().all()
    prices_by_sku: dict[str, list[PriceList]] = {}
    for price in prices:
        prices_by_sku.setdefault(str(price.sku_code or "").strip(), []).append(price)
    for sku_code_key in prices_by_sku:
        prices_by_sku[sku_code_key].sort(key=lambda x: x.date)

    items: list[OrderDetailsRow] = []
    total_amount = 0.0
    for row in order_rows:
        sku_code_key = str(row.sku_code or "").strip()
        prod = products.get(sku_code_key)
        if not prod:
            continue
        sorted_prices = prices_by_sku.get(sku_code_key, [])
        closest_price = None
        earliest_price = sorted_prices[0] if sorted_prices else None
        for p in sorted_prices:
            if p.date <= row.creation_date:
                closest_price = p
        selected_price = closest_price if closest_price is not None else earliest_price
        amount = (
            float(row.quantity_in_mc or 0.0)
            * float(prod.pieces_in_master_carton or 0.0)
            * float(selected_price.dsp or 0.0)
            if selected_price is not None
            else 0.0
        )
        total_amount += amount
        items.append(
            OrderDetailsRow(
                sku_code=prod.sku_code,
                sku_name=prod.sku_name,
                quantity_in_mc=float(row.quantity_in_mc or 0.0),
                gross_weight_kg=float(row.gross_weight_kg) if row.gross_weight_kg is not None else None,
                volume_cbm=float(row.volume_cbm) if row.volume_cbm is not None else None,
                amount_kzt=round(amount, 2),
            )
        )

    filter_options = OrderDetailsFilterOptions(
        sku_code=sorted({str(x.sku_code).strip() for x in items if str(x.sku_code).strip()}),
        sku_name=sorted({str(x.sku_name).strip() for x in items if str(x.sku_name).strip()}),
        quantity_in_mc=sorted(
            {str(round(float(x.quantity_in_mc or 0.0), 2)) for x in items}
        ),
        gross_weight_kg=sorted(
            {
                str(round(float(x.gross_weight_kg or 0.0), 4))
                for x in items
                if x.gross_weight_kg is not None
            }
        ),
        volume_cbm=sorted(
            {
                str(round(float(x.volume_cbm or 0.0), 4))
                for x in items
                if x.volume_cbm is not None
            }
        ),
        amount_kzt=sorted({str(round(float(x.amount_kzt or 0.0), 2)) for x in items}),
    )
    filtered_items = items
    if sku_code:
        sku_code_values = {str(v).strip() for v in sku_code if str(v).strip()}
        filtered_items = [
            x for x in filtered_items if str(x.sku_code).strip() in sku_code_values
        ]
    if sku_name:
        sku_name_values = {str(v).strip() for v in sku_name if str(v).strip()}
        filtered_items = [
            x for x in filtered_items if str(x.sku_name).strip() in sku_name_values
        ]
    parsed_quantities = _parse_float_filters(
        quantity_in_mc,
        field_name="quantity_in_mc",
    )
    if parsed_quantities:
        quantity_values = {round(v, 2) for v in parsed_quantities}
        filtered_items = [
            x
            for x in filtered_items
            if round(float(x.quantity_in_mc or 0.0), 2) in quantity_values
        ]
    parsed_gross_weights = _parse_float_filters(
        gross_weight_kg,
        field_name="gross_weight_kg",
    )
    if parsed_gross_weights:
        gross_weight_values = {round(v, 4) for v in parsed_gross_weights}
        filtered_items = [
            x
            for x in filtered_items
            if x.gross_weight_kg is not None
            and round(float(x.gross_weight_kg or 0.0), 4) in gross_weight_values
        ]
    parsed_volumes = _parse_float_filters(
        volume_cbm,
        field_name="volume_cbm",
    )
    if parsed_volumes:
        volume_values = {round(v, 4) for v in parsed_volumes}
        filtered_items = [
            x
            for x in filtered_items
            if x.volume_cbm is not None
            and round(float(x.volume_cbm or 0.0), 4) in volume_values
        ]
    parsed_amounts = _parse_float_filters(
        amount_kzt,
        field_name="amount_kzt",
    )
    if parsed_amounts:
        amount_values = {round(v, 2) for v in parsed_amounts}
        filtered_items = [
            x
            for x in filtered_items
            if round(float(x.amount_kzt or 0.0), 2) in amount_values
        ]
    total_amount_filtered = sum(float(x.amount_kzt or 0.0) for x in filtered_items)

    header = sorted(order_rows, key=lambda r: (r.creation_date, r.receival_date))[0]
    agg_stmt = select(OrdersAggregated).where(
        OrdersAggregated.order_id == normalized_order_id,
        OrdersAggregated.owner_user_id == owner_user_id,
    )
    aggregated = (await db.execute(agg_stmt)).scalars().first()
    size = _parse_page_size(page_size)
    total_items = len(filtered_items)
    if size is not None:
        offset = (page - 1) * size
        paged_items = filtered_items[offset : offset + size]
        total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    else:
        paged_items = filtered_items
        total_pages = 1 if total_items > 0 else 0

    return OrderDetailsResponse(
        order_id=header.order_id,
        order_name=header.order_name,
        status=display_order_status(aggregated.status if aggregated is not None else header.status),
        creation_date=header.creation_date,
        receival_date=header.receival_date,
        author=header.author,
        total_skus=len(filtered_items),
        total_amount_kzt=round(total_amount_filtered, 2),
        items=paged_items,
        total_items=total_items,
        total_pages=total_pages,
        filter_options=filter_options,
    )


@router.patch("/details/", include_in_schema=False)
@router.patch("/details")
async def patch_order_details(
    db: DBSession,
    user: CurrentUser,
    source_order_id: str = Query(...),
    payload: OrderDetailsPatchRequest = ...,
) -> dict:
    normalized_source_order_id = _normalize_order_id_input(source_order_id)
    stmt = select(PlacedOrder).where(PlacedOrder.order_id == normalized_source_order_id)
    if not is_admin(user):
        stmt = stmt.where(PlacedOrder.owner_user_id == user.id)
    order_rows = (await db.execute(stmt)).scalars().all()
    if not order_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заказ не найден")

    owner_user_id = order_rows[0].owner_user_id
    target_order_id = (
        _normalize_order_id_input(payload.order_id)
        if payload.order_id
        else normalized_source_order_id
    )
    if target_order_id != normalized_source_order_id:
        exists_stmt = select(PlacedOrder.id).where(
            and_(
                PlacedOrder.order_id == target_order_id,
                PlacedOrder.owner_user_id == owner_user_id,
            )
        )
        if (await db.execute(exists_stmt)).first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Заказ '{target_order_id}' уже существует",
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
            detail="Не переданы поля для обновления",
        )

    upd = update(PlacedOrder).where(PlacedOrder.order_id == normalized_source_order_id)
    if not is_admin(user):
        upd = upd.where(PlacedOrder.owner_user_id == user.id)
    result = await db.execute(upd.values(**values))
    await db.commit()

    await refresh_orders_aggregated(db, owner_user_id=owner_user_id)
    return {"rows_updated": int(result.rowcount or 0)}

