BRANCH_NAME_RU_MAP = {
    "almaty": "Алматы",
    "astana": "Астана",
    "shymkent": "Шымкент",
    "aktau": "Актау",
    "aktobe": "Актобе",
}


def localize_branch_name(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    mapped = BRANCH_NAME_RU_MAP.get(raw.lower())
    return mapped if mapped is not None else raw


def normalize_branch_lookup(value: str | None) -> str:
    localized = localize_branch_name(value)
    return str(localized or "").strip().lower()

