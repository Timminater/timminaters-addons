"""Local price estimator used by the quarter-hour adapter.

Only the active baseline and factor rules are implemented here. The numerical
parameters follow the documented v4 experiment; this is a separate implementation
with no dependency on the original forecast module or its inactive features.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import exp
from statistics import median


@dataclass(frozen=True)
class Estimate:
    baseline: float
    factor_points: dict[str, int]
    total_points: int
    predicted: float
    regime: str
    band_half: float
    extreme_event_prob: float


def _easter(year: int) -> date:
    """Gregorian computus, used only to derive Dutch movable holidays."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _holiday(day: date) -> bool:
    easter = _easter(day.year)
    movable = {easter + timedelta(days=offset) for offset in (-2, 0, 1, 39, 49, 50)}
    fixed = {(1, 1), (4, 27), (12, 25), (12, 26)}
    return day in movable or (day.month, day.day) in fixed or (
        day.month == 5 and day.day == 5 and day.year % 5 == 0
    )


def _day_class(stamp: datetime) -> str:
    if _holiday(stamp.date()):
        return "holiday"
    return "weekend" if stamp.weekday() >= 5 else "weekday"


def _crossborder_holiday(stamp: datetime) -> bool:
    return stamp.year in (2026, 2027) and stamp.month == 5 and stamp.day == 1


def baseline(target: datetime, history: list[dict]) -> float | None:
    """Blend recent same-day-type and long weekday/weekend medians."""
    observations = [(datetime.fromisoformat(row["time"]), float(row["price"])) for row in history]
    earlier = [stamp for stamp, _ in observations if stamp < target]
    if not earlier:
        return None
    anchor = max(earlier) + timedelta(hours=1)
    target_class = _day_class(target)
    low_demand = target_class != "weekday"
    short_start = anchor - timedelta(days=14 if low_demand else 7)
    long_start = anchor - timedelta(days=28)
    short_prices: list[float] = []
    long_prices: list[float] = []
    recent_sum = long_sum = 0.0
    recent_count = long_count = 0

    for stamp, price in observations:
        if stamp >= anchor:
            continue
        if stamp >= long_start:
            long_sum += price
            long_count += 1
            if stamp >= anchor - timedelta(days=7):
                recent_sum += price
                recent_count += 1
        if stamp.hour != target.hour or (target_class == "weekday" and _crossborder_holiday(stamp)):
            continue
        if stamp >= short_start and _day_class(stamp) == target_class:
            short_prices.append(price)
        if stamp >= long_start and (_day_class(stamp) != "weekday") == low_demand:
            long_prices.append(price)

    short_level = median(short_prices) if short_prices else None
    long_level = median(long_prices) if long_prices else None
    if short_level is None and long_level is None:
        return None
    if short_level is None:
        level = long_level
    elif long_level is None:
        level = short_level
    else:
        level = 0.25 * short_level + 0.75 * long_level
    trend = 1.0
    if recent_count and long_count and abs(long_sum / long_count) > 5:
        trend = min(2.0, max(0.5, (recent_sum / recent_count) / (long_sum / long_count)))
    return level * trend ** 0.25


def _regime(target: datetime, solar: float, wind: float, temperature: float) -> str:
    if solar < 0.6 and wind < 5 and temperature < 8:
        return "schaarste"
    if target.month in range(5, 10) and 18 <= target.hour <= 22 and wind < 5 and temperature > 20:
        return "zomerschaarste"
    low_demand = target.weekday() >= 5 or _holiday(target.date()) or temperature > 10
    if low_demand and ((solar > 1.4 and 8 <= target.hour <= 18) or wind > 14):
        return "oversupply"
    return "normaal"


def _bucket(value: float, limits: tuple[float, ...], scores: tuple[int, ...]) -> int:
    for limit, score in zip(limits, scores):
        if value < limit:
            return score
    return scores[-1]


def _ratio_score(value: float) -> int:
    if value < 0.7:
        return -2
    if value < 0.9:
        return -1
    if value <= 1.1:
        return 0
    return 1 if value <= 1.3 else 2


def _factors(target: datetime, solar: float, wind: float, temperature: float,
             gas: float, regime: str, prior_ratio: float | None) -> dict[str, int]:
    wind_score = _bucket(wind, (4, 8, 12, 16), (3, 1, 0, -2, -3))
    if target.weekday() == 6:
        wind_score *= 2
    gas_score = _ratio_score(gas)
    previous_score = 0 if prior_ratio is None else _ratio_score(prior_ratio)
    if _holiday(target.date()) or _crossborder_holiday(target):
        day_score = -2
    elif target.weekday() == 6:
        day_score = -2
    elif target.weekday() == 5:
        day_score = -1
    else:
        day_score = 0

    nonlinear = 0
    if regime == "oversupply":
        nonlinear = round(max(-3.0, -14 * max(0.0, solar - 1.3) ** 2 - 0.25 * max(0.0, wind - 16) ** 2))
    winter_scarcity = 0
    if regime == "schaarste":
        severity = (0.9 * max(0.0, 5 - wind) ** 2 + 0.04 * max(0.0, 8 - temperature) ** 2
                    + 6 * max(0.0, 0.6 - solar) ** 2)
        winter_scarcity = min(18, round(severity * (1 + max(0.0, gas - 1)) * 1.5))
    summer_scarcity = 0
    if regime == "zomerschaarste":
        ramp = {18: 0.5, 19: 0.8, 20: 1.0, 21: 1.0, 22: 0.7}[target.hour]
        severity = (1.5 * max(0.0, 5 - wind) ** 2 + 0.1 * max(0.0, temperature - 20) ** 2) * ramp
        summer_scarcity = min(18, round(severity * (1 + max(0.0, gas - 1))))
    return {
        "wind": wind_score,
        "gas": gas_score,
        "vorige_dag": previous_score,
        "dagtype": day_score,
        "nonlinear": nonlinear,
        "scarcity": winter_scarcity,
        "zomerschaarste": summer_scarcity,
    }


def forecast_one(target_dt: datetime, history: list[dict], shortwave_ratio: float,
                 wind_ms: float, temp_c: float, ttf_ratio: float, days_ahead: int,
                 prior_day_price: float | None = None) -> Estimate | None:
    level = baseline(target_dt, history)
    if level is None:
        return None
    prior_ratio = None
    if prior_day_price is not None:
        prior_level = baseline(target_dt - timedelta(days=1), history)
        if prior_level:
            prior_ratio = prior_day_price / prior_level
    regime = _regime(target_dt, shortwave_ratio, wind_ms, temp_c)
    scores = _factors(target_dt, shortwave_ratio, wind_ms, temp_c, ttf_ratio, regime, prior_ratio)
    enabled = ("wind", "gas", "vorige_dag", "dagtype", "nonlinear", "scarcity", "zomerschaarste")
    total = sum(scores[name] for name in enabled)
    predicted = round(level * (1 + total * 0.015), 2)
    band_half = 17.0 + 0.25 * abs(level * (1 + total * 0.015))
    extreme_prob = 0.0
    if regime == "oversupply":
        severity = max(shortwave_ratio / 1.4 if shortwave_ratio > 1.4 else 0,
                       wind_ms / 12 if wind_ms > 12 else 0)
        if severity > 0:
            extreme_prob = round(min(0.95, 1 / (1 + exp(-2.5 * (severity - 1.2)))), 3)
    return Estimate(round(level, 2), scores, total, predicted, regime, band_half, extreme_prob)
