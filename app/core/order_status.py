ORDER_STATUS_OPTIONS_ORDERED = ["завершен", "в пути", "создан"]
ORDER_STATUS_OPTIONS = set(ORDER_STATUS_OPTIONS_ORDERED)
ORDER_STATUS_OPTIONS_ORDERED_DISPLAY = ["В транзите", "Создан", "Завершен"]

_DISPLAY_BY_CANONICAL = {
    "завершен": "Завершен",
    "в пути": "В транзите",
    "создан": "Создан",
}

_ORDER_STATUS_MAP = {
    "completed": "завершен",
    "complete": "завершен",
    "done": "завершен",
    "received": "завершен",
    "delivered": "завершен",
    "завершен": "завершен",
    "завершён": "завершен",
    "завершено": "завершен",
    "завершённый": "завершен",
    "завершенный": "завершен",
    "in transit": "в пути",
    "in-transit": "в пути",
    "in_transit": "в пути",
    "transit": "в пути",
    "moving": "в пути",
    "в пути": "в пути",
    "в транзите": "в пути",
    "created": "создан",
    "new": "создан",
    "создан": "создан",
    "создано": "создан",
}


def normalize_order_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return _ORDER_STATUS_MAP.get(normalized)


def display_order_status(value: str | None) -> str | None:
    canonical = normalize_order_status(value)
    if canonical is None:
        return None
    return _DISPLAY_BY_CANONICAL[canonical]

