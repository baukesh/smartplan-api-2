from datetime import date

from sqlalchemy import Boolean, Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class DPReport(Base, TimestampMixin):
    """Demand planning report definition stored as raw parameters."""

    __tablename__ = "dp_reports"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    product_filter: Mapped[str | None] = mapped_column(String(255))
    branch_filter: Mapped[str | None] = mapped_column(String(255))
    view_type: Mapped[str] = mapped_column(String(32), default="cases")  # dsp/boxes/net_weight
    date_from: Mapped[date | None] = mapped_column(Date)
    date_to: Mapped[date | None] = mapped_column(Date)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int | None]
    updated_by_id: Mapped[int | None]


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int | None]
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(String(2000))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)


class DPReportAccess(Base, TimestampMixin):
    __tablename__ = "dp_report_access"
    __table_args__ = (UniqueConstraint("report_id", "user_id", name="uq_report_user_access"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(index=True)
    user_id: Mapped[int] = mapped_column(index=True)
    granted_by_id: Mapped[int | None]

