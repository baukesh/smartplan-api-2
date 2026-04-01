from datetime import date
from io import BytesIO
import re
from typing import List

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select, update

from app.api.date_params import parse_query_date
from app.api.deps import CurrentUser, DBSession, is_admin
from app.core.branch_localization import normalize_branch_lookup
from app.core.product_status import PRODUCT_STATUS_OPTIONS, normalize_product_status
from app.core.source_normalization import normalize_source_value, source_matches
from app.models.data_uploads import Branch, Product, ProductBranch, PriceList

router = APIRouter(prefix="/assortment", tags=["assortment"])

STATUS_OPTIONS = PRODUCT_STATUS_OPTIONS
PAGE_SIZE_MAP = {"10": 10, "50": 50, "100": 100, "all": None}


class AssortmentItem(BaseModel):
    sku_code: str
    sku_name: str
    mother_sku: str | None = None
    pieces_in_master_carton: float
    master_carton_volume_cbm: float
    master_carton_gross_weight_kg: float
    master_carton_net_weight_kg: float
    lead_time: float
    source: str
    general_stock_norm_days: float
    status: str
    brand: str
    category: str
    sub_category: str
    subline: str


class AssortmentStatusUpdate(BaseModel):
    sku_code: str
    status: str


class AssortmentStatusUpdateRequest(BaseModel):
    updates: list[AssortmentStatusUpdate]


class AssortmentStockNormDaysUpdate(BaseModel):
    sku_code: str
    general_stock_norm_days: float = Field(
        validation_alias=AliasChoices("general_stock_norm_days", "stock_norm_days"),
        serialization_alias="general_stock_norm_days",
    )

    model_config = {"populate_by_name": True}


class AssortmentStockNormDaysUpdateRequest(BaseModel):
    updates: list[AssortmentStockNormDaysUpdate]


class BranchStockNormRow(BaseModel):
    sku_code: str
    sku_name: str
    mother_sku: str | None = None
    branch_name: str
    stock_norm: float


class BranchStockNormUpdate(BaseModel):
    sku_code: str
    branch_name: str
    stock_norm: float


class BranchStockNormUpdateRequest(BaseModel):
    updates: list[BranchStockNormUpdate]


class PriceListRow(BaseModel):
    sku_code: str
    sku_name: str
    date: date
    invoice_price: float
    dsp: float


class PriceListPatchPayload(BaseModel):
    new_date: date
    invoice_price: float
    dsp: float


class AssortmentItemsPage(BaseModel):
    items: list[AssortmentItem]
    total_items: int
    total_pages: int


class BranchStockNormPage(BaseModel):
    items: list[BranchStockNormRow]
    page_size: int | None
    total_items: int
    total_pages: int


class PriceListPage(BaseModel):
    items: list[PriceListRow]
    total_items: int
    total_pages: int


def _parse_page_size(page_size: str) -> int | None:
    normalized = page_size.strip().lower()
    if normalized not in PAGE_SIZE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be one of: 10, 50, 100, all",
        )
    return PAGE_SIZE_MAP[normalized]


def _paginate_list(items: list, page: int, page_size: str) -> tuple[list, int, int]:
    size = _parse_page_size(page_size)
    total_items = len(items)
    if size is None:
        return items, total_items, 1
    total_pages = (total_items + size - 1) // size if total_items > 0 else 0
    offset = (page - 1) * size
    return items[offset : offset + size], total_items, total_pages


@router.get("/items", response_model=AssortmentItemsPage)
async def list_assortment(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> AssortmentItemsPage:
    stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(Product.owner_user_id == user.id)
    if status:
        stmt = stmt.where(Product.status == status)
    rows = (await db.execute(stmt.order_by(Product.sku_code))).scalars().all()
    rows_out = [
        AssortmentItem(
            sku_code=r.sku_code,
            sku_name=r.sku_name,
            mother_sku=r.mother_sku,
            pieces_in_master_carton=r.pieces_in_master_carton,
            master_carton_volume_cbm=r.master_carton_volume_cbm,
            master_carton_gross_weight_kg=r.master_carton_gross_weight_kg,
            master_carton_net_weight_kg=r.master_carton_net_weight_kg,
            lead_time=r.lead_time,
            source=normalize_source_value(r.source),
            general_stock_norm_days=r.general_stock_norm_days,
            status=r.status,
            brand=r.brand,
            category=r.category,
            sub_category=r.sub_category,
            subline=r.sub_line,
        )
        for r in rows
    ]
    items, total_items, total_pages = _paginate_list(rows_out, page=page, page_size=page_size)
    return AssortmentItemsPage(items=items, total_items=total_items, total_pages=total_pages)


@router.get("/status-options", response_model=List[str])
async def get_status_options() -> list[str]:
    return STATUS_OPTIONS


@router.patch("/items/status")
async def update_assortment_item_statuses(
    db: DBSession,
    user: CurrentUser,
    payload: AssortmentStatusUpdateRequest,
) -> dict:
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No status updates provided",
        )
    invalid = [u.status for u in payload.updates if normalize_product_status(u.status) is None]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status values: {sorted(set(invalid))}. Allowed: {STATUS_OPTIONS}",
        )

    updated = 0
    for item in payload.updates:
        normalized_status = normalize_product_status(item.status)
        if normalized_status is None:
            continue
        stmt = update(Product).where(Product.sku_code == item.sku_code)
        if not is_admin(user):
            stmt = stmt.where(Product.owner_user_id == user.id)
        result = await db.execute(stmt.values(status=normalized_status))
        updated += int(result.rowcount or 0)
    await db.commit()
    return {"rows_updated": updated}


@router.patch("/items")
async def update_assortment_items_stock_norm_days(
    db: DBSession,
    user: CurrentUser,
    payload: AssortmentStockNormDaysUpdateRequest,
    status_filter: str | None = Query(None, alias="status"),
    sku_code: str | None = Query(None),
    brand: str | None = Query(None),
    category: str | None = Query(None),
    source: str | None = Query(None),
) -> dict:
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No stock_norm_days updates provided",
        )

    scope_stmt = select(Product)
    if not is_admin(user):
        scope_stmt = scope_stmt.where(Product.owner_user_id == user.id)
    if status_filter:
        scope_stmt = scope_stmt.where(Product.status == status_filter.strip())
    if sku_code:
        scope_stmt = scope_stmt.where(Product.sku_code == sku_code.strip())
    if brand:
        scope_stmt = scope_stmt.where(Product.brand == brand.strip())
    if category:
        scope_stmt = scope_stmt.where(Product.category == category.strip())
    scoped_products = (await db.execute(scope_stmt)).scalars().all()
    if source:
        scoped_products = [p for p in scoped_products if source_matches(source, p.source)]
    scoped_map: dict[str, list[Product]] = {}
    for p in scoped_products:
        scoped_map.setdefault(str(p.sku_code).strip(), []).append(p)

    unresolved: list[str] = []
    updated = 0
    for item in payload.updates:
        normalized_sku = str(item.sku_code).strip()
        matches = scoped_map.get(normalized_sku, [])
        if not matches:
            unresolved.append(normalized_sku)
            continue

        for p in matches:
            result = await db.execute(
                update(Product)
                .where(Product.owner_user_id == p.owner_user_id, Product.sku_id == p.sku_id)
                .values(general_stock_norm_days=float(item.general_stock_norm_days))
            )
            updated += int(result.rowcount or 0)

    if unresolved:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": (
                    "Some sku_code values were not found under current access/query filter scope"
                ),
                "unresolved": unresolved,
            },
        )

    await db.commit()
    return {"rows_updated": updated}


@router.get("/items/download")
async def download_assortment_items(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(None),
    sku_code: str | None = Query(None),
    brand: str | None = Query(None),
    category: str | None = Query(None),
    source: str | None = Query(None),
):
    stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(Product.owner_user_id == user.id)
    if status:
        stmt = stmt.where(Product.status == status)
    if sku_code:
        stmt = stmt.where(Product.sku_code == sku_code.strip())
    if brand:
        stmt = stmt.where(Product.brand == brand.strip())
    if category:
        stmt = stmt.where(Product.category == category.strip())
    rows = (await db.execute(stmt.order_by(Product.sku_code))).scalars().all()
    if source:
        rows = [r for r in rows if source_matches(source, r.source)]

    export_rows = [
        {
            "sku_code": r.sku_code,
            "sku_name": r.sku_name,
            "mother_sku": r.mother_sku,
            "pieces_in_master_carton": r.pieces_in_master_carton,
            "master_carton_volume_cbm": r.master_carton_volume_cbm,
            "master_carton_gross_weight_kg": r.master_carton_gross_weight_kg,
            "master_carton_net_weight_kg": r.master_carton_net_weight_kg,
            "lead_time": r.lead_time,
            "source": normalize_source_value(r.source),
            "general_stock_norm_days": r.general_stock_norm_days,
            "status": r.status,
            "brand": r.brand,
            "category": r.category,
            "sub_category": r.sub_category,
            "subline": r.sub_line,
        }
        for r in rows
    ]

    output = BytesIO()
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="assortment")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="assortment_items.xlsx"'},
    )


@router.get("/branch-matrix", response_model=BranchStockNormPage)
async def get_branch_stock_matrix(
    db: DBSession,
    user: CurrentUser,
    sku_code: str | None = Query(None),
    branch_name: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> BranchStockNormPage:
    stmt = select(ProductBranch)
    if not is_admin(user):
        stmt = stmt.where(ProductBranch.owner_user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    branch_stmt = select(Branch)
    product_stmt = select(Product)
    if not is_admin(user):
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
    branches = (await db.execute(branch_stmt)).scalars().all()
    products = (await db.execute(product_stmt)).scalars().all()
    branch_map = {(b.owner_user_id, b.branch_id): b.branch_name for b in branches}
    product_map = {(p.owner_user_id, p.sku_id): p for p in products}
    rows_out = [
        BranchStockNormRow(
            sku_code=product_map[(r.owner_user_id, r.sku_id)].sku_code,
            sku_name=product_map[(r.owner_user_id, r.sku_id)].sku_name,
            mother_sku=product_map[(r.owner_user_id, r.sku_id)].mother_sku,
            branch_name=branch_map.get((r.owner_user_id, r.branch_id), r.branch_id),
            stock_norm=r.stock_norm,
        )
        for r in rows
        if (r.owner_user_id, r.sku_id) in product_map
    ]
    if sku_code:
        sku_norm = sku_code.strip()
        rows_out = [x for x in rows_out if x.sku_code == sku_norm]
    if branch_name:
        branch_norm = normalize_branch_lookup(branch_name)
        rows_out = [x for x in rows_out if normalize_branch_lookup(x.branch_name) == branch_norm]
    unique_skus = sorted({x.sku_code for x in rows_out})
    size = _parse_page_size(page_size)
    total_items = len(unique_skus)
    if size is None:
        paged_skus = set(unique_skus)
        total_pages = 1 if total_items > 0 else 0
        page_size_out: int | None = None
    else:
        offset = (page - 1) * size
        paged_skus = set(unique_skus[offset : offset + size])
        total_pages = (total_items + size - 1) // size if total_items > 0 else 0
        page_size_out = size
    items = [x for x in rows_out if x.sku_code in paged_skus]
    return BranchStockNormPage(
        items=items,
        page_size=page_size_out,
        total_items=total_items,
        total_pages=total_pages,
    )


@router.patch("/branch-matrix/stock-norm")
async def update_branch_matrix_stock_norm(
    db: DBSession,
    user: CurrentUser,
    payload: BranchStockNormUpdateRequest,
) -> dict:
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No stock_norm updates provided",
        )

    product_stmt = select(Product)
    branch_stmt = select(Branch)
    if not is_admin(user):
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
        branch_stmt = branch_stmt.where(Branch.owner_user_id == user.id)
    products = (await db.execute(product_stmt)).scalars().all()
    branches = (await db.execute(branch_stmt)).scalars().all()

    product_ids_by_key: dict[tuple[int, str], str] = {
        (p.owner_user_id, str(p.sku_code).strip()): p.sku_id for p in products
    }
    branch_ids_by_key: dict[tuple[int, str], str] = {
        (b.owner_user_id, normalize_branch_lookup(b.branch_name)): b.branch_id for b in branches
    }
    owners = sorted({owner_id for owner_id, _ in product_ids_by_key.keys()})

    updated = 0
    unresolved: list[dict[str, str]] = []
    for item in payload.updates:
        normalized_sku = str(item.sku_code).strip()
        normalized_branch = normalize_branch_lookup(item.branch_name)
        matched_any = False
        for owner_id in owners:
            sku_id = product_ids_by_key.get((owner_id, normalized_sku))
            branch_id = branch_ids_by_key.get((owner_id, normalized_branch))
            if not sku_id or not branch_id:
                continue
            matched_any = True
            stmt = (
                update(ProductBranch)
                .where(
                    ProductBranch.owner_user_id == owner_id,
                    ProductBranch.sku_id == sku_id,
                    ProductBranch.branch_id == branch_id,
                )
                .values(stock_norm=item.stock_norm)
            )
            result = await db.execute(stmt)
            updated += int(result.rowcount or 0)
        if not matched_any:
            unresolved.append(
                {
                    "sku_code": normalized_sku,
                    "branch_name": str(item.branch_name).strip(),
                }
            )

    if unresolved:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Some updates could not be resolved by sku_code/branch_name",
                "unresolved": unresolved,
            },
        )

    await db.commit()
    return {"rows_updated": updated}


@router.get("/branch-matrix/download")
async def download_branch_matrix(
    db: DBSession,
    user: CurrentUser,
    sku_code: str | None = Query(None),
    branch_name: str | None = Query(None),
):
    rows_page = await get_branch_stock_matrix(
        db=db,
        user=user,
        sku_code=sku_code,
        branch_name=branch_name,
        page=1,
        page_size="all",
    )
    export_rows = [
        {
            "sku_code": r.sku_code,
            "sku_name": r.sku_name,
            "mother_sku": r.mother_sku,
            "branch_name": r.branch_name,
            "stock_norm": r.stock_norm,
        }
        for r in rows_page.items
    ]
    output = BytesIO()
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="branch_matrix")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="assortment_branch_matrix.xlsx"'},
    )


@router.get("/price-list", response_model=PriceListPage)
async def get_price_list(
    db: DBSession,
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: str = Query("10"),
) -> PriceListPage:
    stmt = select(PriceList)
    product_stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(PriceList.owner_user_id == user.id)
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
    rows = (await db.execute(stmt.order_by(PriceList.date))).scalars().all()
    products = (await db.execute(product_stmt)).scalars().all()
    product_by_key = {(p.owner_user_id, p.sku_id): p for p in products}
    rows_out = [
        PriceListRow(
            sku_code=product_by_key[(r.owner_user_id, r.sku_id)].sku_code,
            sku_name=product_by_key[(r.owner_user_id, r.sku_id)].sku_name,
            date=r.date,
            invoice_price=r.invoice_price,
            dsp=r.dsp,
        )
        for r in rows
        if (r.owner_user_id, r.sku_id) in product_by_key
    ]
    items, total_items, total_pages = _paginate_list(rows_out, page=page, page_size=page_size)
    return PriceListPage(items=items, total_items=total_items, total_pages=total_pages)


@router.patch("/price-list")
async def update_price_list_rows(
    db: DBSession,
    user: CurrentUser,
    sku_code: str = Query(...),
    date: date = Query(...),
    payload: PriceListPatchPayload = ...,
) -> dict:
    def _normalize_sku_lookup(value: str) -> str:
        # Query strings may decode '+' as space; normalize both to avoid false 404s.
        return re.sub(r"\s+", " ", str(value or "").replace("+", " ")).strip().lower()

    product_stmt = select(Product)
    if not is_admin(user):
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(product_stmt)).scalars().all()
    sku_by_owner_and_code: dict[tuple[int, str], str] = {}
    for p in products:
        normalized = _normalize_sku_lookup(p.sku_code)
        sku_by_owner_and_code[(p.owner_user_id, normalized)] = p.sku_id
    owners = sorted({owner_id for owner_id, _ in sku_by_owner_and_code.keys()})

    normalized_sku = _normalize_sku_lookup(sku_code)
    updated = 0
    conflicts: list[dict[str, str]] = []
    for owner_id in owners:
        sku_id = sku_by_owner_and_code.get((owner_id, normalized_sku))
        if not sku_id:
            continue
        if payload.new_date != date:
            existing_stmt = select(PriceList.id).where(
                PriceList.owner_user_id == owner_id,
                PriceList.sku_id == sku_id,
                PriceList.date == payload.new_date,
            )
            existing_row = (await db.execute(existing_stmt)).first()
            if existing_row is not None:
                conflicts.append(
                    {
                        "sku_code": sku_code.strip(),
                        "existing_date": payload.new_date.isoformat(),
                    }
                )
                continue
        stmt = (
            update(PriceList)
            .where(
                PriceList.owner_user_id == owner_id,
                PriceList.sku_id == sku_id,
                PriceList.date == date,
            )
            .values(
                date=payload.new_date,
                invoice_price=payload.invoice_price,
                dsp=payload.dsp,
            )
        )
        result = await db.execute(stmt)
        updated += int(result.rowcount or 0)
    if conflicts:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "Cannot update date: target date already exists for the same sku_code"
                ),
                "conflicts": conflicts,
            },
        )
    if updated == 0:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Price-list row not found for provided sku_code/date under current access scope"
            ),
        )
    await db.commit()
    return {"rows_updated": updated}


@router.get("/price-list/download")
async def download_price_list(
    db: DBSession,
    user: CurrentUser,
    sku_code: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    parsed_date_from = parse_query_date(date_from, field_name="date_from")
    parsed_date_to = parse_query_date(date_to, field_name="date_to", end_of_month=True)
    rows_page = await get_price_list(db=db, user=user, page=1, page_size="all")
    filtered_rows = rows_page.items
    if sku_code:
        sku_norm = sku_code.strip()
        filtered_rows = [r for r in filtered_rows if r.sku_code == sku_norm]
    if parsed_date_from:
        filtered_rows = [r for r in filtered_rows if r.date >= parsed_date_from]
    if parsed_date_to:
        filtered_rows = [r for r in filtered_rows if r.date <= parsed_date_to]
    export_rows = [
        {
            "sku_code": r.sku_code,
            "sku_name": r.sku_name,
            "date": r.date,
            "invoice_price": r.invoice_price,
            "dsp": r.dsp,
        }
        for r in filtered_rows
    ]
    output = BytesIO()
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="price_list")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="assortment_price_list.xlsx"'},
    )
