from datetime import date, datetime

import pandas as pd

from app.api.v1.uploads import _parse_upload_date, _validate_historical_sales_columns


def test_parse_upload_date_supports_expected_formats() -> None:
    target = date(2025, 3, 15)
    excel_serial = (pd.Timestamp(target) - pd.Timestamp("1899-12-30")).days

    assert _parse_upload_date(pd.Timestamp(target)) == target
    assert _parse_upload_date(datetime(2025, 3, 15, 10, 30, 0)) == target
    assert _parse_upload_date("2025-03-15") == target
    assert _parse_upload_date("15/03/2025") == target
    assert _parse_upload_date(excel_serial) == target

    assert _parse_upload_date("2025-03") == date(2025, 3, 1)
    assert _parse_upload_date("03/2025") == date(2025, 3, 1)


def test_parse_upload_date_invalid_raises_clear_message() -> None:
    try:
        _parse_upload_date("not-a-date")
        assert False, "Expected ValueError for invalid date format"
    except ValueError as exc:
        assert "Unsupported date format" in str(exc)


def test_validate_historical_sales_columns_accepts_alias_headers() -> None:
    df = pd.DataFrame(
        {
            "sku_code": ["SKU-001"],
            "branch_id": ["300001"],
            "date": ["2025-03"],
            "fact_quantity_in_mc": [10],
            "target_quantity_in_mc": [12],
            "past_available_stock": [5],
        }
    )
    errors = _validate_historical_sales_columns(df)
    assert errors == []


def test_validate_historical_sales_columns_requires_alias_groups() -> None:
    df = pd.DataFrame(
        {
            "date": ["2025-03"],
            "fact_quantity_in_mc": [10],
            "target_quantity_in_mc": [12],
            "past_available_stock": [5],
        }
    )
    errors = _validate_historical_sales_columns(df)
    assert len(errors) == 2
    messages = {e["message"] for e in errors}
    assert "One of sku_id or sku_code must be provided" in messages
    assert "One of branch_name or branch_id must be provided" in messages

