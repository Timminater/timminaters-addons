"""Quarter-hour adapter around the locally implemented v4 price estimator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo
import math

from . import forecast_core

UTC = timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
MODEL_VERSION = "local-v4-quarter-adapter-2"
STEP = timedelta(minutes=15)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class ForecastPoint:
    start_utc: datetime
    end_utc: datetime
    price: float
    lower: float | None = None
    upper: float | None = None
    source: str = "quarter-v4"
    quality: str = "voorlopig"
    reason: str = "Kwartiermethode is nog niet gekalibreerd."


@dataclass(frozen=True)
class ForecastRun:
    model_version: str
    issued_at: datetime
    points: list[ForecastPoint]
    quality: str
    reasons: list[str]


def _dt(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be an aware datetime or ISO-8601 string")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return result.astimezone(UTC)


def _quarter_start(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def _prepare_history(
    history: Iterable[Mapping[str, Any]], issued_at: datetime, price_scale: float
) -> dict[datetime, dict[str, Any]]:
    """Normalize records and enforce their point-in-time availability."""
    known: dict[datetime, dict[str, Any]] = {}
    for row in history:
        if not isinstance(row, Mapping):
            continue
        try:
            start = _dt(row.get("start_utc", row.get("start")), field="start_utc")
            published = row.get("published_at")
            if published is not None:
                published_at = _dt(published, field="published_at")
                if published_at > issued_at:
                    continue
            elif start >= issued_at:
                # A future tariff without its publication timestamp is not known yet.
                continue
            end_raw = row.get("end_utc", row.get("end"))
            end = _dt(end_raw, field="end_utc") if end_raw is not None else start + STEP
        except (TypeError, ValueError, OverflowError):
            continue
        price = _number(row.get("price"))
        if start.second or start.microsecond or start.minute % 15 or end != start + STEP or price is None:
            continue
        known[start] = {"start": start, "end": end, "price": price * price_scale, "published_at": published}
    return known


def _weather_index(weather: Mapping[str, Any] | None) -> tuple[dict[datetime, Mapping[str, Any]], set[str]]:
    if not weather:
        return {}, {"weer ontbreekt; neutrale factorinvoer is gebruikt"}
    raw = weather.get("hourly", weather)
    if not isinstance(raw, Mapping):
        return {}, {"weerbron bevat geen uurlijkse waarden"}
    index: dict[datetime, Mapping[str, Any]] = {}
    for key, values in raw.items():
        try:
            stamp = _dt(key, field="weather timestamp")
        except (TypeError, ValueError):
            continue
        if isinstance(values, Mapping):
            index[stamp.replace(minute=0, second=0, microsecond=0)] = values
    return index, set()


_MONTHLY_SOLAR_NORM_MJ = {
    1: 2.5, 2: 5.0, 3: 9.0, 4: 14.0, 5: 17.5, 6: 18.5,
    7: 18.0, 8: 15.5, 9: 11.0, 10: 6.5, 11: 3.0, 12: 2.0,
}
_DAYLIGHT_HOURS = {
    1: (8.8, 16.8), 2: (8.2, 17.7), 3: (7.5, 19.5), 4: (6.4, 20.5),
    5: (5.7, 21.3), 6: (5.3, 21.8), 7: (5.5, 21.8), 8: (6.2, 21.1),
    9: (7.0, 20.0), 10: (7.5, 18.5), 11: (7.8, 17.0), 12: (8.5, 16.6),
}


def _seasonal_solar_norm_mj(target: datetime) -> float:
    month, day = target.month, target.day
    if day <= 15:
        previous = 12 if month == 1 else month - 1
        fraction = (day + 15) / 30
        return _MONTHLY_SOLAR_NORM_MJ[previous] * (1 - fraction) + _MONTHLY_SOLAR_NORM_MJ[month] * fraction
    following = 1 if month == 12 else month + 1
    fraction = (day - 15) / 30
    return _MONTHLY_SOLAR_NORM_MJ[month] * (1 - fraction) + _MONTHLY_SOLAR_NORM_MJ[following] * fraction


def _hourly_solar_norm_wh(target: datetime) -> float:
    """Exact De Bilt normalizer used by the pinned runner's hourly solar factor."""
    daily_wh = _seasonal_solar_norm_mj(target) * 1000.0 / 3.6
    rise, setting = _DAYLIGHT_HOURS[target.month]
    midpoint = target.hour + 0.5
    if midpoint <= rise or midpoint >= setting:
        return 0.0
    raw = math.sin(math.pi * (midpoint - rise) / (setting - rise))
    norm_sum = sum(
        math.sin(math.pi * (hour + 0.5 - rise) / (setting - rise))
        for hour in range(24)
        if rise < hour + 0.5 < setting
    )
    return daily_wh * raw / norm_sum if norm_sum else 0.0


def _weather_for(target: datetime, index: Mapping[datetime, Mapping[str, Any]]) -> tuple[dict[str, float], set[str]]:
    values = index.get(target.astimezone(UTC).replace(minute=0, second=0, microsecond=0), {})
    inputs: dict[str, float] = {}
    missing: set[str] = set()
    solar = _number(values.get("solar_ratio"), _number(values.get("shortwave_ratio")))
    if solar is None:
        radiation = _number(values.get("shortwave_radiation"), _number(values.get("shortwave_wm2")))
        daily = _number(values.get("shortwave_mj"))
        normal = _hourly_solar_norm_wh(target)
        if radiation is not None and normal >= 10:
            solar = radiation / normal
        else:
            if daily is None:
                local_date = target.astimezone(AMSTERDAM).date()
                radiation_values = []
                for stamp, hour_values in index.items():
                    if stamp.astimezone(AMSTERDAM).date() != local_date:
                        continue
                    hour_radiation = _number(
                        hour_values.get("shortwave_radiation"),
                        _number(hour_values.get("shortwave_wm2")),
                    )
                    if hour_radiation is not None:
                        radiation_values.append(hour_radiation)
                # Rebuild the source runner's daily total only from a reasonably
                # complete actual hourly forecast; otherwise leave it unavailable.
                if len(radiation_values) >= 18:
                    daily = sum(radiation_values) * 3.6 / 1000.0
            if daily is not None and _seasonal_solar_norm_mj(target) > 0:
                solar = daily / _seasonal_solar_norm_mj(target)
    inputs: dict[str, float] = {}
    missing: set[str] = set()
    if solar is None:
        solar = 1.0
        missing.add("solar_ratio")
    inputs["solar_ratio"] = solar

    wind = _number(values.get("wind_ms"))
    if wind is None:
        wind_10m = _number(values.get("wind_ms_10m"))
        if wind_10m is not None:
            wind = wind_10m * 1.38
    candidates = {
        "wind_ms": wind,
        "temp_c": _number(values.get("temp_c")),
        "ttf_ratio": _number(values.get("ttf_ratio")),
    }
    defaults = {"wind_ms": 8.0, "temp_c": 15.0, "ttf_ratio": 1.0}
    for field, value in candidates.items():
        if value is None:
            missing.add(field)
            value = defaults[field]
        inputs[field] = value
    return inputs, missing


def _source_history(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    # The source code uses local calendar hours. Preserve offset/fold in ISO text.
    return [
        {"time": row["start"].astimezone(AMSTERDAM).isoformat(), "price": row["price"]}
        for row in rows
    ]


def _hourly_aggregates(known: Mapping[datetime, Mapping[str, Any]], before: datetime) -> dict[datetime, float]:
    by_hour: dict[datetime, dict[datetime, float]] = {}
    for start, row in known.items():
        if start >= before:
            continue
        hour = start.replace(minute=0)
        by_hour.setdefault(hour, {})[start] = row["price"]
    result = {}
    for hour, quarters in by_hour.items():
        expected = {hour + STEP * i for i in range(4)}
        if set(quarters) == expected:
            result[hour] = sum(quarters.values()) / 4
    return result


def _quarter_samples(target: datetime, rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    local = target.astimezone(AMSTERDAM)
    ambiguous_hour = local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
    result = []
    for row in rows:
        t = row["start"].astimezone(AMSTERDAM)
        if t.hour != local.hour or t.minute != local.minute:
            continue
        if ambiguous_hour and t.utcoffset() != local.utcoffset():
            # Keep the two occurrences of a repeated autumn hour distinct.
            continue
        result.append(row)
    return result


def forecast_quarters(
    history: Iterable[Mapping[str, Any]],
    issued_at: datetime | str,
    weather: Mapping[str, Any] | None = None,
    max_points: int = 672,
    price_scale: float = 1.0,
) -> ForecastRun:
    """Forecast up to seven days in real quarter intervals.

    Inputs must contain actual quarter prices and their availability time for
    future published tariffs. price_scale converts them to the source model's
    EUR/MWh scale; returned values use the input scale. Missing weather uses
    neutral factor values and is surfaced in the run's provisional reasons.
    """
    issue = _dt(issued_at, field="issued_at")
    if not 1 <= max_points <= 672:
        raise ValueError("max_points must be between 1 and 672")
    if not math.isfinite(price_scale) or price_scale <= 0:
        raise ValueError("price_scale must be a positive finite number")
    known = _prepare_history(history, issue, price_scale)
    if not known:
        return ForecastRun(MODEL_VERSION, issue, [], "voorlopig", ["geen bruikbare kwartierhistorie beschikbaar"])
    all_rows = sorted(known.values(), key=lambda row: row["start"])
    weather_index, reasons = _weather_index(weather)
    reasons = set(reasons)

    # Never use measured/archived target or later prices; published future prices
    # are valid only because _prepare_history requires publication <= issued_at.
    now_quarter = _quarter_start(issue)
    cursor = now_quarter
    while cursor in known and known[cursor]["end"] == cursor + STEP:
        cursor += STEP

    # If current state is absent, begin at its first gap. Never skip a missing
    # quarter in the output sequence.
    stop = cursor + STEP * max_points
    stop = min(stop, now_quarter + timedelta(days=7))
    hourly = _hourly_aggregates(known, issue)
    points: list[ForecastPoint] = []

    while cursor < stop and len(points) < max_points:
        if cursor in known:
            cursor += STEP
            continue
        target = cursor.astimezone(AMSTERDAM)
        weather_values, missing = _weather_for(target, weather_index)
        reasons.update(f"weerinput ontbreekt: {name}" for name in missing)
        sample_rows = _quarter_samples(target, all_rows)

        # Two actual same-local-quarter observations are the minimum for
        # calling the quarter baseline supported. Otherwise use only complete
        # UTC hours formed from four real quarters and repeat one hourly result.
        use_quarter = len(sample_rows) >= 2
        if use_quarter:
            source_rows = _source_history(sample_rows)
            source = "quarter-v4"
            reason = "v4-factoren op dezelfde lokale kwartierpositie; voorlopige kwartierresolutie"
        else:
            target_is_ambiguous = target.replace(fold=0).utcoffset() != target.replace(fold=1).utcoffset()
            aggregates = [
                {"start": start, "price": value}
                for start, value in hourly.items()
                if not target_is_ambiguous or start.astimezone(AMSTERDAM).utcoffset() == target.utcoffset()
            ]
            source_rows = _source_history(aggregates)
            source = "hour-v4-flat-quarter"
            reason = "onvoldoende echte kwartierbasis; vier vlakke kwartierwaarden uit complete geobserveerde uurgemiddelden"
            reasons.add("kwartierbasis heeft minder dan 2 vergelijkbare echte kwartierwaarnemingen")

        day_ahead = max(0, (target.date() - issue.astimezone(AMSTERDAM).date()).days)
        fc = forecast_core.forecast_one(
            target_dt=target,
            history=source_rows,
            shortwave_ratio=weather_values["solar_ratio"],
            wind_ms=weather_values["wind_ms"],
            temp_c=weather_values["temp_c"],
            ttf_ratio=weather_values["ttf_ratio"],
            days_ahead=day_ahead,
        )
        if fc is None or not math.isfinite(fc.predicted):
            reasons.add("modelbasis ontbreekt voor een of meer toekomstige kwartieren")
            # Do not manufacture a point when the source model has no baseline.
            cursor += STEP
            continue

        # Flatten within the source-hour for the fallback: recalculate once at
        # the first target quarter and cache subsequent quarters in that hour.
        if source == "hour-v4-flat-quarter":
            base_hour = cursor.replace(minute=0)
            existing = next((p for p in points if p.source == source and p.start_utc.replace(minute=0) == base_hour), None)
            if existing is not None:
                price = existing.price
            else:
                price = fc.predicted / price_scale
        else:
            price = fc.predicted / price_scale
        # The source model's absolute hourly margin is an uncalibrated visual
        # guide here, not a confidence interval for quarter-hour tariffs.
        margin = fc.band_half / price_scale
        if source == "hour-v4-flat-quarter" and existing is not None:
            lower, upper = existing.lower, existing.upper
        else:
            lower, upper = float(price - margin), float(price + margin)
        points.append(ForecastPoint(cursor, cursor + STEP, float(price), lower, upper,
                                    source=source, reason=reason))
        cursor += STEP

    quality_reasons = sorted(reasons | {"kwartierfout en banddekking zijn niet operationeel gekalibreerd"})
    return ForecastRun(MODEL_VERSION, issue, points, "voorlopig", quality_reasons)
