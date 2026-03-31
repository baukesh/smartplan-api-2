from calendar import monthrange
from datetime import date

from fastapi import HTTPException, status


def parse_query_date(
    value: str | date | None,
    *,
    field_name: str,
    end_of_month: bool = False,
) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value

    raw = str(value).strip()
    if not raw:
        return None

    try:
        # Supports YYYY-MM-DD
        return date.fromisoformat(raw)
    except ValueError:
        pass

    # Supports YYYY-MM
    try:
        parts = raw.split("-")
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            year = int(parts[0])
            month = int(parts[1])
            day = monthrange(year, month)[1] if end_of_month else 1
            return date(year, month, day)
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"{field_name} must be in YYYY-MM or YYYY-MM-DD format",
    )

