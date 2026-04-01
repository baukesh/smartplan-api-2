import re


def normalize_source_value(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if re.fullmatch(r"seoul\s*\d*", lowered):
        return "Seoul"
    return raw


def source_matches(filter_value: str | None, source_value: str | None) -> bool:
    return normalize_source_value(filter_value).lower() == normalize_source_value(source_value).lower()
