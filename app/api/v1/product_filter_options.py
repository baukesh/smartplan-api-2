from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import Product

router = APIRouter(tags=["product-filter-options"])


class ProductFilterOptionsResponse(BaseModel):
    brand: list[str]
    category: list[str]
    sub_category: list[str]
    subline: list[str]
    sku_code: list[str]


def _clean_list(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


@router.get("/product-filter-options", response_model=ProductFilterOptionsResponse)
async def get_product_filter_options(
    db: DBSession,
    user: CurrentUser,
    sku_code: list[str] | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    sub_category: list[str] | None = Query(default=None),
    subline: list[str] | None = Query(default=None),
) -> ProductFilterOptionsResponse:
    stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(stmt)).scalars().all()

    sku_set = set(_clean_list(sku_code))
    brand_set = set(_clean_list(brand))
    category_set = set(_clean_list(category))
    sub_category_set = set(_clean_list(sub_category))
    subline_set = set(_clean_list(subline))

    filtered = [
        p
        for p in products
        if (not sku_set or p.sku_code in sku_set)
        and (not brand_set or p.brand in brand_set)
        and (not category_set or p.category in category_set)
        and (not sub_category_set or p.sub_category in sub_category_set)
        and (not subline_set or p.sub_line in subline_set)
    ]

    return ProductFilterOptionsResponse(
        brand=sorted({str(p.brand).strip() for p in filtered if str(p.brand).strip()}),
        category=sorted(
            {str(p.category).strip() for p in filtered if str(p.category).strip()}
        ),
        sub_category=sorted(
            {
                str(p.sub_category).strip()
                for p in filtered
                if str(p.sub_category).strip()
            }
        ),
        subline=sorted(
            {str(p.sub_line).strip() for p in filtered if str(p.sub_line).strip()}
        ),
        sku_code=sorted(
            {str(p.sku_code).strip() for p in filtered if str(p.sku_code).strip()}
        ),
    )
