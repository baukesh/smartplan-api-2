from collections import defaultdict
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_uploads import PlacedOrder
from app.models.derived import OrdersAggregated

STATUS_PRIORITY = {
    "Created": 1,
    "In transit": 2,
    "Completed": 3,
}


def _status_max(values: list[str]) -> str:
    if not values:
        return "Created"
    return max(values, key=lambda v: STATUS_PRIORITY.get(v, -1))


async def refresh_orders_aggregated(db: AsyncSession, owner_user_id: int) -> None:
    rows = (
        await db.execute(
            select(PlacedOrder).where(PlacedOrder.owner_user_id == owner_user_id)
        )
    ).scalars().all()

    grouped: dict[str, list[PlacedOrder]] = defaultdict(list)
    for row in rows:
        grouped[row.order_id].append(row)

    await db.execute(
        delete(OrdersAggregated).where(OrdersAggregated.owner_user_id == owner_user_id)
    )

    inserts: list[OrdersAggregated] = []
    for order_id, items in grouped.items():
        items_sorted = sorted(items, key=lambda x: (x.creation_date, x.receival_date))
        creation_date: date = items_sorted[0].creation_date
        receival_date: date = max(i.receival_date for i in items_sorted)
        total_quantity = sum(float(i.quantity_in_mc or 0.0) for i in items_sorted)
        total_amount = sum(float(i.amount_kzt or 0.0) for i in items_sorted)
        status = _status_max([str(i.status) for i in items_sorted])
        inserts.append(
            OrdersAggregated(
                order_id=order_id,
                creation_date=creation_date,
                receival_date=receival_date,
                total_quantity_in_mc=round(total_quantity, 2),
                total_amount_kzt=round(total_amount, 2),
                status=status,
                owner_user_id=owner_user_id,
            )
        )

    if inserts:
        db.add_all(inserts)
    await db.commit()
