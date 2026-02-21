from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User, UserRole
from app.schemas.user import TokenPayload

DBSession = Annotated[AsyncSession, Depends(get_db)]

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    db: DBSession,
    token: Annotated[str, Depends(oauth2_scheme)],
    api_access_key: Annotated[str | None, Header(alias=settings.API_ACCESS_KEY_HEADER)] = None,
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not api_access_key or not compare_digest(api_access_key, settings.API_ACCESS_KEY):
        raise unauthorized
    try:
        payload = TokenPayload.model_validate(decode_token(token))
    except Exception:
        raise unauthorized

    if payload.type != "access":
        raise unauthorized

    user = await db.get(User, int(payload.sub))
    if not user or not user.is_active:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def require_roles(*roles: UserRole):
    async def checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required roles: {[r.value for r in roles]}",
            )
        return user

    return checker

