from datetime import date
from io import BytesIO
from typing import List

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DBSession, is_admin
from app.models.data_uploads import Branch, Product, ProductBranch, PriceList

router = APIRouter(prefix="/assortment", tags=["assortment"])

STATUS_OPTIONS = ["Active", "Inactive", "Discontinued", "TBD"]
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


class PriceListUpdateRow(BaseModel):
    sku_code: str
    original_date: date
    date: date
    invoice_price: float
    dsp: float


class PriceListUpdateRequest(BaseModel):
    updates: list[PriceListUpdateRow]


class AssortmentItemsPage(BaseModel):
    items: list[AssortmentItem]
    total_items: int
    total_pages: int


class BranchStockNormPage(BaseModel):
    items: list[BranchStockNormRow]
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
            source=r.source,
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
    invalid = [u.status for u in payload.updates if u.status not in STATUS_OPTIONS]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status values: {sorted(set(invalid))}. Allowed: {STATUS_OPTIONS}",
        )

    updated = 0
    for item in payload.updates:
        stmt = update(Product).where(Product.sku_code == item.sku_code)
        if not is_admin(user):
            stmt = stmt.where(Product.owner_user_id == user.id)
        result = await db.execute(stmt.values(status=item.status))
        updated += int(result.rowcount or 0)
    await db.commit()
    return {"rows_updated": updated}


@router.get("/items/download")
async def download_assortment_items(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(None),
):
    stmt = select(Product)
    if not is_admin(user):
        stmt = stmt.where(Product.owner_user_id == user.id)
    if status:
        stmt = stmt.where(Product.status == status)
    rows = (await db.execute(stmt.order_by(Product.sku_code))).scalars().all()

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
            "source": r.source,
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
        branch_norm = branch_name.strip().lower()
        rows_out = [x for x in rows_out if x.branch_name.strip().lower() == branch_norm]
    items, total_items, total_pages = _paginate_list(rows_out, page=page, page_size=page_size)
    return BranchStockNormPage(items=items, total_items=total_items, total_pages=total_pages)


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
        (b.owner_user_id, str(b.branch_name).strip().lower()): b.branch_id for b in branches
    }
    owners = sorted({owner_id for owner_id, _ in product_ids_by_key.keys()})

    updated = 0
    unresolved: list[dict[str, str]] = []
    for item in payload.updates:
        normalized_sku = str(item.sku_code).strip()
        normalized_branch = str(item.branch_name).strip().lower()
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
):
    rows_page = await get_branch_stock_matrix(db=db, user=user, page=1, page_size="all")
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
    payload: PriceListUpdateRequest,
) -> dict:
    if not payload.updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No price-list updates provided",
        )

    product_stmt = select(Product)
    if not is_admin(user):
        product_stmt = product_stmt.where(Product.owner_user_id == user.id)
    products = (await db.execute(product_stmt)).scalars().all()
    sku_by_owner_and_code: dict[tuple[int, str], str] = {
        (p.owner_user_id, p.sku_code): p.sku_id for p in products
    }
    owners = sorted({owner_id for owner_id, _ in sku_by_owner_and_code.keys()})

    updated = 0
    for item in payload.updates:
        for owner_id in owners:
            sku_id = sku_by_owner_and_code.get((owner_id, item.sku_code))
            if not sku_id:
                continue
            stmt = (
                update(PriceList)
                .where(
                    PriceList.owner_user_id == owner_id,
                    PriceList.sku_id == sku_id,
                    PriceList.date == item.original_date,
                )
                .values(
                    date=item.date,
                    invoice_price=item.invoice_price,
                    dsp=item.dsp,
                )
            )
            result = await db.execute(stmt)
            updated += int(result.rowcount or 0)
    await db.commit()
    return {"rows_updated": updated}


@router.get("/price-list/download")
async def download_price_list(
    db: DBSession,
    user: CurrentUser,
):
    rows_page = await get_price_list(db=db, user=user, page=1, page_size="all")
    export_rows = [
        {
            "sku_code": r.sku_code,
            "sku_name": r.sku_name,
            "date": r.date,
            "invoice_price": r.invoice_price,
            "dsp": r.dsp,
        }
        for r in rows_page.items
    ]
    output = BytesIO()
    pd.DataFrame(export_rows).to_excel(output, index=False, sheet_name="price_list")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="assortment_price_list.xlsx"'},
    )
