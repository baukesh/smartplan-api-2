from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession
from app.services.promo_service import (
    compute_promo_values,
    format_promo_date,
    load_owner_promos,
    normalize_promo_filters,
    parse_promo_date,
    parse_promo_list,
)

router = APIRouter(prefix="/promo-calendar", tags=["promo-calendar"])


class PromoCalendarRow(BaseModel):
    promo_id: int
    promo_name: str
    promo_channel: str | None = None
    promo_branches: list[str]
    promo_sku_codes: list[str]
    promo_start_date: str
    promo_end_date: str
    fact_value: float
    baseline_forecast_value: float
    promo_effect: float
    promo_plan_value: float
    promo_is_active: bool


@router.get("/", response_model=list[PromoCalendarRow])
@router.get("", response_model=list[PromoCalendarRow], include_in_schema=False)
async def get_promo_calendar(
    db: DBSession,
    user: CurrentUser,
    view_type: str | None = Query(
        default=None,
        description="Projection filter. Values: DSP, Invoice price, Cases, Gross weight, Net weight.",
    ),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sku_code: list[str] | None = Query(default=None),
    sku_name: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
    sublines: list[str] | None = Query(default=None),
    branch_name: list[str] | None = Query(default=None),
    promo_channel: list[str] | None = Query(default=None),
) -> list[PromoCalendarRow]:
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    merged_subline = (subline or []) + (sublines or [])
    filters = normalize_promo_filters(
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        sku_code=sku_code,
        sku_name=sku_name,
        brand=brand,
        category=category,
        sub_category=sub_category,
        subline=merged_subline or None,
        branch_name=branch_name,
        promo_channel=promo_channel,
    )
    owner_user_id = int(user.id)
    promos = await load_owner_promos(db, owner_user_id)
    rows: list[PromoCalendarRow] = []
    for promo in promos:
        if not promo.promo_is_active:
            continue
        values = await compute_promo_values(
            db,
            owner_user_id=owner_user_id,
            promo=promo,
            view_type=view_type or "cases",
            filters=filters,
            all_promos=promos,
            include_overlaps=True,
        )
        if values is None or values.promo_effect < 0:
            continue
        rows.append(
            PromoCalendarRow(
                promo_id=promo.id,
                promo_name=promo.promo_name,
                promo_channel=promo.promo_channel,
                promo_branches=parse_promo_list(promo.promo_branches),
                promo_sku_codes=parse_promo_list(promo.promo_sku_codes),
                promo_start_date=format_promo_date(promo.promo_start_date),
                promo_end_date=format_promo_date(promo.promo_end_date),
                fact_value=values.fact_value,
                baseline_forecast_value=values.baseline_forecast_value,
                promo_effect=values.promo_effect,
                promo_plan_value=values.promo_plan_value,
                promo_is_active=bool(promo.promo_is_active),
            )
        )
    rows.sort(key=lambda row: (parse_promo_date(row.promo_start_date, field_name="promo_start_date"), row.promo_id))
    return rows
