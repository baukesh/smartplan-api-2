from __future__ import annotations

import json
from datetime import date
from statistics import pstdev

import httpx

from app.core.config import settings


SYSTEM_PROMPT = (
    "You are a demand-planning forecasting engine. "
    "Use only the provided data. Do not invent facts. "
    "Return strict JSON only, no markdown, no prose."
)


def _as_month_start(d: date) -> str:
    return d.replace(day=1).isoformat()


def _fallback_average(history: list[dict]) -> float:
    values = [float(x.get("fact_quantity_in_mc") or 0.0) for x in history]
    tail = values[-6:] if len(values) >= 6 else values
    if not tail:
        return 0.0
    return sum(tail) / len(tail)


def _extract_json_text(content: str) -> str:
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return raw


def _history_values(history: list[dict]) -> list[float]:
    return [float(x.get("fact_quantity_in_mc") or 0.0) for x in history]


def _is_flat(series: list[float], tol: float = 1e-6) -> bool:
    if not series:
        return True
    return max(series) - min(series) <= tol


def _derive_features(history: list[dict]) -> dict:
    values = _history_values(history)
    if not values:
        return {
            "last_3_avg": 0.0,
            "last_6_avg": 0.0,
            "last_12_avg": 0.0,
            "trend_slope_6m": 0.0,
            "avg_mom_growth_6m": 0.0,
            "volatility_cv_12m": 0.0,
            "seasonality_index_by_month": {},
            "anti_flat_expected": False,
        }

    def _avg(tail: int) -> float:
        chunk = values[-tail:] if len(values) >= tail else values
        return (sum(chunk) / len(chunk)) if chunk else 0.0

    last_3_avg = _avg(3)
    last_6_avg = _avg(6)
    last_12_avg = _avg(12)

    slope = 0.0
    tail6 = values[-6:] if len(values) >= 6 else values
    if len(tail6) >= 2:
        slope = (tail6[-1] - tail6[0]) / (len(tail6) - 1)

    mom: list[float] = []
    for i in range(max(1, len(tail6) - 5), len(values)):
        prev = values[i - 1]
        cur = values[i]
        if prev > 0:
            mom.append((cur - prev) / prev)
    avg_mom_growth_6m = (sum(mom) / len(mom)) if mom else 0.0

    tail12 = values[-12:] if len(values) >= 12 else values
    mean12 = (sum(tail12) / len(tail12)) if tail12 else 0.0
    volatility_cv_12m = (pstdev(tail12) / mean12) if mean12 > 0 and len(tail12) > 1 else 0.0

    month_bucket: dict[str, list[float]] = {}
    for item in history[-24:]:
        m = str(item.get("date", ""))[5:7]
        if len(m) == 2:
            month_bucket.setdefault(m, []).append(float(item.get("fact_quantity_in_mc") or 0.0))
    seasonality_index_by_month: dict[str, float] = {}
    if mean12 > 0:
        for mm, vals in month_bucket.items():
            seasonality_index_by_month[mm] = (sum(vals) / len(vals)) / mean12

    trend_strength = abs(slope) / max(last_6_avg, 1.0)
    anti_flat_expected = trend_strength >= 0.03 or volatility_cv_12m >= 0.15

    return {
        "last_3_avg": round(last_3_avg, 4),
        "last_6_avg": round(last_6_avg, 4),
        "last_12_avg": round(last_12_avg, 4),
        "trend_slope_6m": round(slope, 4),
        "avg_mom_growth_6m": round(avg_mom_growth_6m, 6),
        "volatility_cv_12m": round(volatility_cv_12m, 6),
        "seasonality_index_by_month": seasonality_index_by_month,
        "anti_flat_expected": anti_flat_expected,
    }


def _validate_response_months(
    parsed: dict,
    forecast_months: list[date],
) -> list[float] | None:
    items = parsed.get("baseline_quantities_in_mc")
    if not isinstance(items, list) or len(items) != len(forecast_months):
        return None

    expected_months = [_as_month_start(d) for d in forecast_months]
    result: list[float] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return None
        month_value = str(item.get("date", "")).strip()
        if month_value != expected_months[idx]:
            return None
        try:
            qty = float(item.get("quantity_in_mc"))
        except Exception:
            return None
        if qty < 0:
            return None
        result.append(qty)
    return result


async def _call_openai_forecast(
    *,
    user_payload: dict,
    timeout_seconds: float,
) -> list[float] | None:
    request_body = {
        "model": settings.OPENAI_FORECAST_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(
            f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        resp.raise_for_status()
        data = resp.json()
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    parsed = json.loads(_extract_json_text(content))
    return parsed


async def forecast_baseline_quantities_in_mc(
    *,
    sku_code: str,
    branch_id: str,
    forecast_months: list[date],
    history: list[dict],
    current_stock: float,
    stock_norm_days: float,
    placed_orders_history: list[dict],
    timeout_seconds: float = 30.0,
) -> list[float]:
    """
    Returns one baseline quantity per forecast month.
    Falls back to moving-average baseline if GPT is unavailable/invalid.
    """
    if not forecast_months:
        return []

    fallback = _fallback_average(history)
    fallback_series = [fallback for _ in forecast_months]
    if not settings.OPENAI_API_KEY:
        return fallback_series

    month_list = [_as_month_start(d) for d in forecast_months]
    features = _derive_features(history)
    user_payload = {
        "task": "Predict baseline monthly demand in master cartons (quantity_in_mc).",
        "methodology": [
            "Start from a weighted base: 50% recent_6m average, 30% recent_3m trend-adjusted level, 20% annual-level with seasonality index.",
            "Apply trend_slope_6m forward month by month as a gentle drift, not a jump.",
            "Use seasonality_index_by_month only when that month exists in history; otherwise use 1.0.",
            "Respect inventory signal: if current_stock_in_mc is critically low vs stock_norm_days, avoid unrealistic demand collapse.",
            "Output should be smooth but not artificially flat when trend/volatility exists.",
        ],
        "constraints": [
            "Use only provided data.",
            "Do not hallucinate external events.",
            "Return exactly one value per requested month.",
            "quantity_in_mc must be a non-negative number.",
            "If anti_flat_expected=true, output must contain at least 3 distinct monthly quantities across the horizon.",
            "Do not copy a moving-average baseline unless data clearly indicates flat demand.",
            "Do not include markdown or explanation outside JSON.",
        ],
        "output_schema": {
            "baseline_quantities_in_mc": [
                {"date": "YYYY-MM-01", "quantity_in_mc": 0.0}
            ]
        },
        "forecast_months": month_list,
        "context": {
            "sku_code": sku_code,
            "branch_id": branch_id,
            "current_stock_in_mc": current_stock,
            "stock_norm_days": stock_norm_days,
            "historical_monthly": history[-24:],
            "placed_orders_history": placed_orders_history[-24:],
            "derived_features": features,
        },
    }

    try:
        parsed = await _call_openai_forecast(
            user_payload=user_payload,
            timeout_seconds=timeout_seconds,
        )
        validated = _validate_response_months(parsed, forecast_months)
        if validated is None:
            return fallback_series
        # One retry with stronger anti-flat constraints when signal says non-flat is expected.
        if features.get("anti_flat_expected") and _is_flat(validated):
            retry_payload = {
                **user_payload,
                "retry_reason": (
                    "Previous output was flat despite anti_flat_expected=true. "
                    "Regenerate with visible month-to-month variation driven by provided trend/seasonality."
                ),
                "constraints": user_payload["constraints"]
                + [
                    "Output must be non-flat and reflect trend_slope_6m direction unless contradicted by stock/order context.",
                ],
            }
            parsed_retry = await _call_openai_forecast(
                user_payload=retry_payload,
                timeout_seconds=timeout_seconds,
            )
            validated_retry = _validate_response_months(parsed_retry, forecast_months)
            if validated_retry is not None:
                validated = validated_retry
        return validated
    except Exception:
        return fallback_series
