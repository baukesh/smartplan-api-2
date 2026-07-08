from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.derived import ForecastSalesMonthly
from app.models.reporting import DPReport, DPReportForecastOverride

CASE_OVERRIDE_METRIC = "adjusted_forecast_quantity_in_mc"
FORECAST_OVERRIDE_METRICS = {
    "adjusted_forecast_quantity_in_mc",
    "adjusted_forecast_amount_kzt",
    "adjusted_forecast_invoice_amount_kzt",
}


def month_start(value: date) -> date:
    return value.replace(day=1)


def latest_report_by_owner(report_rows: list[DPReport]) -> dict[int, DPReport]:
    latest: dict[int, DPReport] = {}
    for report in report_rows:
        owner_id = int(report.created_by_id or 0)
        if owner_id <= 0:
            continue
        current = latest.get(owner_id)
        report_updated = report.created_at or report.updated_at or datetime.min
        current_updated = (
            current.created_at or current.updated_at or datetime.min
            if current is not None
            else datetime.min
        )
        if current is None or (report_updated, int(report.id)) > (current_updated, int(current.id)):
            latest[owner_id] = report
    return latest


def override_specificity(row: DPReportForecastOverride) -> int:
    return sum(
        1
        for value in [
            row.branch_name,
            row.brand,
            row.category,
            row.sub_category,
            row.subline,
            row.sku_name,
        ]
        if value
    )


def apply_case_overrides_to_forecast_atoms(
    *,
    forecast_atoms: list[dict],
    override_rows: list[DPReportForecastOverride],
) -> None:
    relevant_overrides = [
        row for row in override_rows if row.metric_type == CASE_OVERRIDE_METRIC
    ]
    for ov in sorted(relevant_overrides, key=lambda row: (override_specificity(row), int(row.id))):
        target_period = month_start(ov.period)
        matched = [
            atom
            for atom in forecast_atoms
            if int(atom["owner_user_id"]) == int(ov.owner_user_id)
            and atom["period"] == target_period
            and (ov.branch_name is None or atom["branch_name"] == ov.branch_name)
            and (ov.brand is None or atom["brand"] == ov.brand)
            and (ov.category is None or atom["category"] == ov.category)
            and (ov.sub_category is None or atom["sub_category"] == ov.sub_category)
            and (ov.subline is None or atom["subline"] == ov.subline)
            and (ov.sku_name is None or atom["sku_name"] == ov.sku_name)
        ]
        if not matched:
            continue
        baseline_sum = sum(float(atom["baseline_qty"]) for atom in matched)
        if baseline_sum > 0:
            for atom in matched:
                atom["effective_qty"] = float(ov.value) * (float(atom["baseline_qty"]) / baseline_sum)
        else:
            even_share = float(ov.value) / len(matched)
            for atom in matched:
                atom["effective_qty"] = even_share


def apply_forecast_overrides_to_atoms(
    *,
    forecast_atoms: list[dict],
    override_rows: list[DPReportForecastOverride],
    metric_types: set[str] | None = None,
) -> None:
    allowed_metrics = metric_types or FORECAST_OVERRIDE_METRICS
    relevant_overrides = [
        row for row in override_rows if row.metric_type in allowed_metrics
    ]
    for ov in sorted(relevant_overrides, key=lambda row: (override_specificity(row), int(row.id))):
        target_period = month_start(ov.period)
        matched = [
            atom
            for atom in forecast_atoms
            if int(atom["owner_user_id"]) == int(ov.owner_user_id)
            and atom["period"] == target_period
            and (ov.branch_name is None or atom["branch_name"] == ov.branch_name)
            and (ov.brand is None or atom["brand"] == ov.brand)
            and (ov.category is None or atom["category"] == ov.category)
            and (ov.sub_category is None or atom["sub_category"] == ov.sub_category)
            and (ov.subline is None or atom["subline"] == ov.subline)
            and (ov.sku_name is None or atom["sku_name"] == ov.sku_name)
        ]
        if not matched:
            continue
        metric_type = str(ov.metric_type)
        baseline_sum = sum(
            float(atom.get("baseline_metrics", {}).get(metric_type, 0.0) or 0.0)
            for atom in matched
        )
        if baseline_sum > 0:
            for atom in matched:
                baseline_value = float(atom.get("baseline_metrics", {}).get(metric_type, 0.0) or 0.0)
                atom.setdefault("effective_metrics", {})[metric_type] = float(ov.value) * (
                    baseline_value / baseline_sum
                )
                atom.setdefault("applied_metrics", set()).add(metric_type)
        else:
            even_share = float(ov.value) / len(matched)
            for atom in matched:
                atom.setdefault("effective_metrics", {})[metric_type] = even_share
                atom.setdefault("applied_metrics", set()).add(metric_type)


async def latest_forecast_overrides_for_owners(
    db: AsyncSession,
    owner_user_ids: set[int],
    metric_types: set[str] | None = None,
) -> list[DPReportForecastOverride]:
    owner_ids = {int(owner_id) for owner_id in owner_user_ids if int(owner_id) > 0}
    if not owner_ids:
        return []
    report_rows = (
        await db.execute(
            select(DPReport).where(
                DPReport.created_by_id.in_(owner_ids),
                DPReport.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    latest_reports = latest_report_by_owner(report_rows)
    latest_report_ids = [int(report.id) for report in latest_reports.values()]
    if not latest_report_ids:
        return []
    stmt = select(DPReportForecastOverride).where(
        DPReportForecastOverride.report_id.in_(latest_report_ids),
        DPReportForecastOverride.owner_user_id.in_(owner_ids),
    )
    if metric_types:
        stmt = stmt.where(DPReportForecastOverride.metric_type.in_(metric_types))
    return list((await db.execute(stmt)).scalars().all())


async def latest_case_overrides_for_owner(
    db: AsyncSession,
    owner_user_id: int,
) -> tuple[DPReport | None, list[DPReportForecastOverride]]:
    report_rows = (
        await db.execute(
            select(DPReport).where(
                DPReport.created_by_id == owner_user_id,
                DPReport.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    latest_report = latest_report_by_owner(report_rows).get(owner_user_id)
    if latest_report is None:
        return None, []
    override_rows = (
        await db.execute(
            select(DPReportForecastOverride).where(
                DPReportForecastOverride.report_id == int(latest_report.id),
                DPReportForecastOverride.owner_user_id == owner_user_id,
                DPReportForecastOverride.metric_type == CASE_OVERRIDE_METRIC,
            )
        )
    ).scalars().all()
    return latest_report, list(override_rows)


async def apply_latest_case_overrides_to_forecast_rows(
    db: AsyncSession,
    *,
    owner_user_id: int,
    forecast_rows: list[ForecastSalesMonthly],
    product_by_sku: dict[str, object],
    branch_name_by_id: dict[str, str],
) -> dict[tuple[str, str, date], float]:
    _latest_report, override_rows = await latest_case_overrides_for_owner(db, owner_user_id)
    atoms: list[dict] = []
    for row in forecast_rows:
        sku_code = str(row.sku_code or "").strip()
        product = product_by_sku.get(sku_code)
        branch_id = str(row.branch_id or "").strip()
        baseline_qty = float(row.baseline_forecast_quantity_in_mc or 0.0)
        effective_qty = (
            float(row.adjusted_forecast_quantity_in_mc)
            if row.adjusted_forecast_quantity_in_mc is not None
            else baseline_qty
        )
        atoms.append(
            {
                "owner_user_id": int(row.owner_user_id),
                "branch_id": branch_id,
                "branch_name": branch_name_by_id.get(branch_id, branch_id),
                "period": month_start(row.date),
                "sku_code": sku_code,
                "sku_name": str(getattr(product, "sku_name", "") or ""),
                "brand": str(getattr(product, "brand", "") or ""),
                "category": str(getattr(product, "category", "") or ""),
                "sub_category": str(getattr(product, "sub_category", "") or ""),
                "subline": str(getattr(product, "sub_line", "") or ""),
                "baseline_qty": baseline_qty,
                "effective_qty": effective_qty,
            }
        )
    apply_case_overrides_to_forecast_atoms(
        forecast_atoms=atoms,
        override_rows=override_rows,
    )
    return {
        (str(atom["sku_code"]), str(atom["branch_id"]), atom["period"]): float(atom["effective_qty"])
        for atom in atoms
    }
