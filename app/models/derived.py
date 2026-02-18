from datetime import date

from sqlalchemy import Date, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ForecastSalesMonthly(Base, TimestampMixin):
    __tablename__ = "forecast_sales_monthly"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    branch_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    baseline_forecast_quantity_in_mc: Mapped[float] = mapped_column(Float)
    baseline_forecast_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    baseline_forecast_volume_cbm: Mapped[float | None] = mapped_column(Float)
    baseline_forecast_amount_kzt: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_volume_cbm: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_amount_kzt: Mapped[float | None] = mapped_column(Float)
    future_available_stock: Mapped[float] = mapped_column(Float)


class ForecastOrders(Base, TimestampMixin):
    __tablename__ = "forecast_orders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    month_prior_available_stock: Mapped[float] = mapped_column(Float)
    average_l3m_quantity_in_mc: Mapped[float] = mapped_column(Float)
    average_f3m_quantity_in_mc: Mapped[float] = mapped_column(Float)
    recommended_quantity_in_mc: Mapped[float] = mapped_column(Float)
    adjusted_quantity_in_mc: Mapped[float | None] = mapped_column(Float)


class InventoryHealth(Base, TimestampMixin):
    __tablename__ = "inventory_health"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    branch_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    sales_quantity_in_mc: Mapped[float] = mapped_column(Float)
    sales_gross_weight_kg: Mapped[float] = mapped_column(Float)
    sales_volume_cbm: Mapped[float] = mapped_column(Float)
    sales_amount_kzt: Mapped[float] = mapped_column(Float)
    total_sales_share: Mapped[float] = mapped_column(Float)
    available_stock: Mapped[float] = mapped_column(Float)
    average_f3m_quantity_in_mc: Mapped[float] = mapped_column(Float)
    dsp: Mapped[float] = mapped_column(Float)
    available_stock_days: Mapped[float] = mapped_column(Float)
    stock_norm_days: Mapped[float] = mapped_column(Float)
    overstock: Mapped[float] = mapped_column(Float)
    understock: Mapped[float] = mapped_column(Float)
    stock_out: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(8))
    health_index: Mapped[str] = mapped_column(String(32))


class BranchDistribution(Base, TimestampMixin):
    __tablename__ = "branch_distribution"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    branch_id: Mapped[str] = mapped_column(String(64), index=True)
    available_quantity_in_mc: Mapped[float] = mapped_column(Float)
    available_volume_cbm: Mapped[float] = mapped_column(Float)
    available_gross_weight_kg: Mapped[float] = mapped_column(Float)
    available_amount_kzt: Mapped[float] = mapped_column(Float)
    recommended_quantity_in_mc: Mapped[float] = mapped_column(Float)
    recommended_volume_cbm: Mapped[float] = mapped_column(Float)
    recommended_gross_weight_kg: Mapped[float] = mapped_column(Float)
    recommended_amount_kzt: Mapped[float] = mapped_column(Float)
    branch_health_index: Mapped[str] = mapped_column(String(32))


class DPReportMart(Base, TimestampMixin):
    __tablename__ = "dp_report"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    branch_id: Mapped[str] = mapped_column(String(64), index=True)
    fact_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    fact_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    fact_volume_cbm: Mapped[float | None] = mapped_column(Float)
    fact_amount_kzt: Mapped[float | None] = mapped_column(Float)
    target_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    target_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    target_volume_cbm: Mapped[float | None] = mapped_column(Float)
    target_amount_kzt: Mapped[float | None] = mapped_column(Float)
    past_available_stock: Mapped[float | None] = mapped_column(Float)
    baseline_forecast_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    baseline_forecast_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    baseline_forecast_volume_cbm: Mapped[float | None] = mapped_column(Float)
    baseline_forecast_amount_kzt: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_volume_cbm: Mapped[float | None] = mapped_column(Float)
    adjusted_forecast_amount_kzt: Mapped[float | None] = mapped_column(Float)
    future_available_stock: Mapped[float | None] = mapped_column(Float)

