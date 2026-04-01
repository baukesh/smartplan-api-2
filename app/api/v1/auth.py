from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.models.data_uploads import HistoricalSalesMonthly, PlacedOrder, PriceList, Product, ProductBranch
from app.models.derived import BranchDistribution, DPReportMart, ForecastOrders, InventoryHealth
from app.models.user import User, UserRole
from app.schemas.user import Token, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


class UserInfoOut(BaseModel):
    username: str
    role: str
    full_name: str
    isAssortmentCreated: bool
    isOrdersCreated: bool
    isInventoryHealthCreated: bool
    isDashboardCreated: bool
    isSupplyChainCreated: bool
    isDistributionCreated: bool
    isReportsCreated: bool


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register_user(db: DBSession, user_in: UserCreate) -> User:
    existing = await db.execute(select(User).where(User.email == user_in.email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    user = User(
        email=user_in.email,
        full_name=user_in.full_name,
        role=user_in.role,
        hashed_password=hash_password(user_in.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(
    db: DBSession,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    stmt = select(User).where(User.email == form_data.username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    access = create_access_token(sub=str(user.id))
    refresh = create_refresh_token(sub=str(user.id))
    return Token(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=Token)
async def refresh_token(token: str) -> Token:
    # For MVP we simply issue a new access token given a valid refresh token
    from app.core.security import decode_token

    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user_id = str(payload.get("sub"))
    access = create_access_token(sub=user_id)
    refresh = create_refresh_token(sub=user_id)
    return Token(access_token=access, refresh_token=refresh)


async def _count_by_owner(db: AsyncSession, model, owner_user_id: int) -> int:
    stmt = select(func.count()).select_from(model).where(model.owner_user_id == owner_user_id)
    return int((await db.execute(stmt)).scalar_one() or 0)


@router.get("/user-info", response_model=UserInfoOut)
async def user_info(db: DBSession, user: CurrentUser) -> UserInfoOut:
    owner_user_id = int(user.id)

    product_count = await _count_by_owner(db, Product, owner_user_id)
    product_branch_count = await _count_by_owner(db, ProductBranch, owner_user_id)
    historical_count = await _count_by_owner(db, HistoricalSalesMonthly, owner_user_id)
    price_count = await _count_by_owner(db, PriceList, owner_user_id)
    placed_orders_count = await _count_by_owner(db, PlacedOrder, owner_user_id)
    inventory_health_count = await _count_by_owner(db, InventoryHealth, owner_user_id)
    forecast_orders_count = await _count_by_owner(db, ForecastOrders, owner_user_id)
    branch_distribution_count = await _count_by_owner(db, BranchDistribution, owner_user_id)
    report_mart_count = await _count_by_owner(db, DPReportMart, owner_user_id)

    # Stage 1: assortment prerequisites are fully uploaded.
    is_assortment_created = (
        product_count > 0
        and product_branch_count > 0
        and historical_count > 0
        and price_count > 0
    )
    # Stage 2: orders file uploaded.
    is_orders_created = is_assortment_created and placed_orders_count > 0
    # Stage 3: inventory-health/dashboard marts are materialized.
    is_inventory_health_created = is_orders_created and inventory_health_count > 0
    is_dashboard_created = is_inventory_health_created
    # Stage 4: supply-chain/distribution marts are ready (post-forecast refresh).
    is_supply_chain_created = is_dashboard_created and forecast_orders_count > 0
    is_distribution_created = is_supply_chain_created and branch_distribution_count > 0
    # Final stage: reports mart is ready.
    is_reports_created = is_distribution_created and report_mart_count > 0

    return {
        "username": user.email,
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "full_name": user.full_name,
        "isAssortmentCreated": is_assortment_created,
        "isOrdersCreated": is_orders_created,
        "isInventoryHealthCreated": is_inventory_health_created,
        "isDashboardCreated": is_dashboard_created,
        "isSupplyChainCreated": is_supply_chain_created,
        "isDistributionCreated": is_distribution_created,
        "isReportsCreated": is_reports_created,
    }

