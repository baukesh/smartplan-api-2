BRANCH_NAME_RU_MAP = {
    "almaty": "Алматы",
    "astana": "Астана",
    "shymkent": "Шымкент",
    "aktau": "Актау",
    "aktobe": "Актобе",
}

_LATIN_CONFUSABLES_MAP = {
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
    "к": "k",
    "м": "m",
    "т": "t",
    "в": "b",
    "н": "h",
    "і": "i",
    "ї": "i",
    "ы": "y",
}


def localize_branch_name(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    lowered = raw.lower()
    mapped = BRANCH_NAME_RU_MAP.get(lowered)
    if mapped is None:
        # Support mixed-script variants like "Almatы" (latin + cyrillic).
        deconfused = "".join(_LATIN_CONFUSABLES_MAP.get(ch, ch) for ch in lowered)
        mapped = BRANCH_NAME_RU_MAP.get(deconfused)
    return mapped if mapped is not None else raw


def normalize_branch_lookup(value: str | None) -> str:
    localized = localize_branch_name(value)
    return str(localized or "").strip().lower()

