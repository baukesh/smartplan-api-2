from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.models.reporting import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: int
    title: str
    body: str | None = None
    is_read: bool

    model_config = {"from_attributes": True}


@router.get("/", response_model=List[NotificationOut])
async def list_notifications(
    db: DBSession,
    user: CurrentUser,
) -> list[Notification]:
    stmt = select(Notification).where(
        (Notification.user_id == user.id) | (Notification.user_id.is_(None))
    )
    result = await db.execute(stmt.order_by(Notification.created_at.desc()))  # type: ignore[attr-defined]
    return list(result.scalars().all())


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    db: DBSession,
    user: CurrentUser,
    notification_id: int,
) -> Notification:
    notification = await db.get(Notification, notification_id)
    if not notification or (notification.user_id not in (None, user.id)):
        # For MVP just return empty 404; frontend can hide it
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True
    await db.commit()
    await db.refresh(notification)
    return notification

