from sqlalchemy import Date, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Assortment(Base, TimestampMixin):
    __tablename__ = "assortment"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_code: Mapped[str] = mapped_column(String(64))
    mother_sku: Mapped[str | None] = mapped_column(String(64))
    barcode: Mapped[str | None] = mapped_column(String(64))
    sku_name: Mapped[str] = mapped_column(String(255))
    sku_name_local: Mapped[str | None] = mapped_column(String(255))
    pieces_in_master_carton: Mapped[float | None] = mapped_column(Float)
    master_carton_volume_cbm: Mapped[float | None] = mapped_column(Float)
    master_carton_gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    master_carton_net_weight_kg: Mapped[float | None] = mapped_column(Float)
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(128))
    general_stock_norm_days: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(64))
    brand: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    sub_category: Mapped[str | None] = mapped_column(String(128))
    sub_line: Mapped[str | None] = mapped_column(String(128))
    line: Mapped[str | None] = mapped_column(String(128))


class BranchStockNorm(Base, TimestampMixin):
    __tablename__ = "branch_stock_norm"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    branch_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    current_stock: Mapped[float | None] = mapped_column(Float)
    stock_norm_days: Mapped[float | None] = mapped_column(Float)


class PriceList(Base, TimestampMixin):
    __tablename__ = "price_list"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[Date] = mapped_column(Date)
    invoice_price: Mapped[float | None] = mapped_column(Float)
    dsp: Mapped[float | None] = mapped_column(Float)


class HistoricalSalesMonthly(Base, TimestampMixin):
    __tablename__ = "historical_sales_monthly"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    branch_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[Date] = mapped_column(Date, index=True)
    fact_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    target_quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    past_available_stock: Mapped[float | None] = mapped_column(Float)


class PlacedOrder(Base, TimestampMixin):
    __tablename__ = "placed_orders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    order_name: Mapped[str | None] = mapped_column(String(255))
    creation_date: Mapped[Date] = mapped_column(Date)
    receival_date: Mapped[Date | None] = mapped_column(Date)
    quantity_in_mc: Mapped[float | None] = mapped_column(Float)
    gross_weight_kg: Mapped[float | None] = mapped_column(Float)
    volume_cbm: Mapped[float | None] = mapped_column(Float)
    amount_kzt: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(64))

