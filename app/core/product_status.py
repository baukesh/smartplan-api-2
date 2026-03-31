PRODUCT_STATUS_OPTIONS = ["активный", "неактивный", "на вывод", "новый"]

_PRODUCT_STATUS_MAP = {
    "active": "активный",
    "активный": "активный",
    "inactive": "неактивный",
    "неактивный": "неактивный",
    "discontinued": "на вывод",
    "на вывод": "на вывод",
    "tbd": "новый",
    "new": "новый",
    "новый": "новый",
}


def normalize_product_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return _PRODUCT_STATUS_MAP.get(normalized)

