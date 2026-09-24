"""Causale analyse van opgeslagen stroomprijsprognoses.

Elke lokale kalenderdag levert precies één run. Intervalkalibratie gebruikt
uitsluitend fouten van eerder uitgegeven runs waarvan de tarieven op het
uitgiftetijdstip al waren gepubliceerd.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo
from typing import Any, Mapping

UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
HORIZONS = (("0-24h", 0.0, 24.0), ("24-48h", 24.0, 48.0), ("48-168h", 48.0, 168.0))
MIN_CALIBRATION_DAYS = 14
MIN_CALIBRATION_POINTS = 100
MIN_HOLDOUT_DAYS = 7
MIN_HOLDOUT_POINTS = 100
NOMINAL_COVERAGE = 0.90
EVALUATION_DAYS = 30
LOOKBACK_DAYS = 45


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _horizon(hours: float) -> str | None:
    for label, lower, upper in HORIZONS:
        if lower <= hours < upper:
            return label
    return None


def _nearest_noon(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the available issue nearest local noon, with earlier tie-break."""
    days: dict[Any, list[dict[str, Any]]] = {}
    for run in runs:
        issued = _utc(run["issued_at"])
        days.setdefault(issued.astimezone(LOCAL_TZ).date(), []).append(run)
    selected = []
    for day, candidates in sorted(days.items()):
        noon = datetime.combine(day, datetime.min.time(), LOCAL_TZ) + timedelta(hours=12)
        selected.append(min(candidates, key=lambda run: (
            abs((_utc(run["issued_at"]) - noon.astimezone(UTC)).total_seconds()),
            _utc(run["issued_at"]),
        )))
    return selected


def _finite_price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _rank_quantile(values: list[float], coverage: float) -> float:
    """Conservative empirical nearest-rank absolute-error quantile."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((coverage * len(ordered) + 0.999999)) - 1))
    return ordered[index]


def _truth_as_of(store: Any, archive_entity: str, issued: datetime,
                 points: list[Mapping[str, Any]], current_truth: Mapping[datetime, float | None]
                 ) -> dict[datetime, float | None]:
    """Read only tariff revisions already published by a historical issue."""
    valid_points = [p for p in points if p.get("start_utc") and p.get("end_utc")]
    if not valid_points:
        return {}
    starts = [_utc(p["start_utc"]) for p in valid_points]
    if callable(getattr(store, "quarters", None)):
        rows = store.quarters(archive_entity, min(starts), max(starts) + timedelta(minutes=15),
                              as_of=issued)
        result: dict[datetime, float | None] = {}
        for row in rows:
            start = _utc(row["start_utc"])
            price = _finite_price(row.get("price"))
            if row.get("quality", "valid") == "valid" and price is not None:
                result[start] = price
        return result
    # Small test doubles may implement only actual_quarters. Their publication
    # history is unavailable, so use the conservative ended-quarter fallback.
    return {start: current_truth.get(start) for start in starts
            if any(_utc(p["start_utc"]) == start and _utc(p["end_utc"]) <= issued
                   for p in valid_points)}


def build_analysis(store: Any, entity: str, archive_entity: str, price_field: str,
                   unit: str, now: datetime | None = None) -> dict[str, Any]:
    """Build recent per-quarter scores and walk-forward empirical intervals.

    The store's archive namespace is passed explicitly so tax field/unit price
    histories cannot be accidentally mixed. Returned comparisons cover the
    latest 30 local dates; calibration can use all retained earlier dates.
    """
    cutoff = _utc(now or datetime.now(UTC))
    local_today = cutoff.astimezone(LOCAL_TZ).date()
    # Keep decompression and scoring bounded even though the app may poll
    # every five minutes. Forty-five daily representatives cover calibration
    # history plus the recent holdout window.
    since = cutoff - timedelta(days=LOOKBACK_DAYS)
    daily_reader = getattr(store, "daily_stored_forecasts", None)
    if callable(daily_reader):
        daily = daily_reader(entity, since, cutoff, price_field, unit, "Europe/Amsterdam")
    else:
        runs = store.stored_forecasts(entity, since, price_field, unit)
        runs = [r for r in runs if _utc(r["issued_at"]) <= cutoff]
        daily = _nearest_noon(runs)
    published_reader = getattr(store, "published_quarters", None)
    truth = (published_reader(archive_entity, cutoff) if callable(published_reader)
             else store.actual_quarters(archive_entity, cutoff))
    truth_by_start = {_utc(start): _finite_price(price) for start, price in truth.items()}
    pending_starts = []
    for run in daily:
        issued = _utc(run["issued_at"])
        for point in run.get("points", []):
            start = _utc(point["start_utc"])
            if (_horizon((start - issued).total_seconds() / 3600) is not None
                    and _finite_price(point.get("price")) is not None
                    and truth_by_start.get(start) is None):
                pending_starts.append(start)

    # Published day-ahead tariffs count once known, even before their quarter
    # elapses. Scores at the current cutoff support the recent evaluation and
    # the independently fitted live bands. Separate as-of scores below are
    # used to estimate historical holdout coverage without lookahead.
    scored_by_day: list[tuple[Any, datetime, list[dict[str, Any]]]] = []
    for run in daily:
        issued = _utc(run["issued_at"])
        day = issued.astimezone(LOCAL_TZ).date()
        rows = []
        for point in run.get("points", []):
            start = _utc(point["start_utc"])
            forecast = _finite_price(point.get("price"))
            actual = truth_by_start.get(start)
            bucket = _horizon((start - issued).total_seconds() / 3600)
            if bucket is None or forecast is None or actual is None:
                continue
            rows.append({"start": start, "end": _utc(point["end_utc"]), "issued": issued,
                         "forecast": forecast, "actual": actual,
                         "error": forecast - actual, "horizon": bucket})
        scored_by_day.append((day, issued, rows))

    eval_from = local_today - timedelta(days=EVALUATION_DAYS - 1)
    evaluation = [(day, issued, rows) for day, issued, rows in scored_by_day
                  if eval_from <= day <= local_today]
    comparison_rows: list[dict[str, Any]] = []
    horizons = []
    holdout_by_horizon: dict[str, list[bool]] = {label: [] for label, _, _ in HORIZONS}
    holdout_days_by_horizon: dict[str, set[Any]] = {label: set() for label, _, _ in HORIZONS}
    runs_by_day = { _utc(run["issued_at"]).astimezone(LOCAL_TZ).date(): run for run in daily }

    for label, _, _ in HORIZONS:
        bucket_eval: list[dict[str, Any]] = []
        for day, issued, rows in evaluation:
            # For each historical holdout issue, score only earlier-day runs
            # whose quarter outcomes had been observed by that holdout issue.
            old_runs = [(old_day, old_run) for old_day, old_run in runs_by_day.items() if old_day < day]
            old_points = [point for _, old_run in old_runs for point in old_run.get("points", [])
                          if _horizon((_utc(point["start_utc"]) - _utc(old_run["issued_at"])).total_seconds() / 3600) == label]
            actual_as_of_issue = _truth_as_of(store, archive_entity, issued, old_points, truth_by_start)
            calibration = []
            for old_day, old_run in old_runs:
                old_issue = _utc(old_run["issued_at"])
                for point in old_run.get("points", []):
                    start = _utc(point["start_utc"])
                    actual = actual_as_of_issue.get(start)
                    forecast = _finite_price(point.get("price"))
                    if (_horizon((start - old_issue).total_seconds() / 3600) == label
                            and forecast is not None and actual is not None):
                        calibration.append({"issued": old_issue, "error": forecast - actual})
            distinct_days = {r["issued"].astimezone(LOCAL_TZ).date() for r in calibration}
            enough = (len(distinct_days) >= MIN_CALIBRATION_DAYS
                      and len(calibration) >= MIN_CALIBRATION_POINTS)
            radius = _rank_quantile([abs(r["error"]) for r in calibration], NOMINAL_COVERAGE) if enough else None
            for row in rows:
                if row["horizon"] != label:
                    continue
                enriched = {**row, "lower": row["forecast"] - radius if radius is not None else None,
                            "upper": row["forecast"] + radius if radius is not None else None,
                            "covered": (radius is not None and abs(row["error"]) <= radius)}
                bucket_eval.append(enriched)
                if radius is not None:
                    holdout_by_horizon[label].append(bool(enriched["covered"]))
                    holdout_days_by_horizon[label].add(day)
                comparison_rows.append({
                    "start": row["start"].isoformat().replace("+00:00", "Z"),
                    "issued_at": row["issued"].isoformat().replace("+00:00", "Z"),
                    "forecast": row["forecast"], "actual": row["actual"], "error": row["error"],
                    "lower": enriched["lower"], "upper": enriched["upper"],
                })
        errors = [r["error"] for r in bucket_eval]
        with_band = [r for r in bucket_eval if r["lower"] is not None]
        horizons.append({"horizon": label, "n": len(errors),
            "mae": sum(abs(e) for e in errors) / len(errors) if errors else None,
            "bias": sum(errors) / len(errors) if errors else None,
            "lower_coverage": sum(bool(r["covered"]) for r in with_band) / len(with_band) if with_band else None,
            "lower_n": len(with_band)})

    comparison_rows.sort(key=lambda row: (row["issued_at"], row["start"]))
    distinct_eval_days = len({_utc(row["issued_at"]).astimezone(LOCAL_TZ).date()
                              for row in comparison_rows})
    # Live chart bands are fitted at the current cutoff from matured residuals
    # on earlier local days. Holdout coverage above remains a distinct,
    # sequential evaluation and is never used to tune these widths.
    live_bands = {}
    for label, _, _ in HORIZONS:
        calibration = [r for old_day, _, rows in scored_by_day if old_day < local_today
                       for r in rows if r["horizon"] == label]
        distinct_days = {r["issued"].astimezone(LOCAL_TZ).date() for r in calibration}
        enough = (len(distinct_days) >= MIN_CALIBRATION_DAYS
                  and len(calibration) >= MIN_CALIBRATION_POINTS)
        fit_radius = (_rank_quantile([abs(r["error"]) for r in calibration], NOMINAL_COVERAGE)
                      if enough else None)
        holdout_days = len(holdout_days_by_horizon[label])
        holdout_points = len(holdout_by_horizon[label])
        holdout_enough = holdout_days >= MIN_HOLDOUT_DAYS and holdout_points >= MIN_HOLDOUT_POINTS
        ready_for_horizon = enough and holdout_enough
        if not enough:
            reason = (f"Minimaal {MIN_CALIBRATION_DAYS} eerdere dagen en {MIN_CALIBRATION_POINTS} kwartieren nodig; "
                      f"beschikbaar: {len(distinct_days)} dagen en {len(calibration)} kwartieren.")
        elif not holdout_enough:
            reason = (f"Minimaal {MIN_HOLDOUT_DAYS} onafhankelijke holdout-dagen en {MIN_HOLDOUT_POINTS} kwartieren nodig; "
                      f"beschikbaar: {holdout_days} dagen en {holdout_points} kwartieren.")
        else:
            reason = None
        live_bands[label] = {"radius": fit_radius if ready_for_horizon else None,
                             "days": len(distinct_days), "points": len(calibration),
                             "holdout_days": holdout_days, "holdout_points": holdout_points,
                             "ready": ready_for_horizon, "reason": reason}
    ready = bool(live_bands) and all(item["ready"] for item in live_bands.values())
    reasons = [item["reason"] for item in live_bands.values() if item["reason"]]
    total_covered = sum(sum(values) for values in holdout_by_horizon.values())
    total_holdout = sum(len(values) for values in holdout_by_horizon.values())
    total_comparisons = len(comparison_rows)
    returned_comparisons = list(reversed(comparison_rows[-500:]))
    return {
        "status": "ready" if comparison_rows else "insufficient_data",
        "unit": unit,
        "summary": {"days": distinct_eval_days, "points": total_comparisons,
                    "returned_points": len(returned_comparisons),
                    "runs": len(daily), "pending_points": len(pending_starts),
                    "first_pending_start": (min(pending_starts).isoformat().replace("+00:00", "Z")
                                            if pending_starts else None)},
        "horizons": horizons,
        "comparisons": returned_comparisons,
        "calibration": {
            "ready": ready,
            "reason": None if ready else " ".join(dict.fromkeys(reasons)) or "Onvoldoende oudere forecast- en archiefdata.",
            "nominal_coverage": NOMINAL_COVERAGE,
            "observed_coverage": total_covered / total_holdout if total_holdout else None,
            "days": min((v["days"] for v in live_bands.values()), default=0),
            "points": min((v["points"] for v in live_bands.values()), default=0),
            "bands": [{
                "horizon": label,
                "half_width": live_bands[label]["radius"],
                "calibration_days": live_bands[label]["days"],
                "calibration_points": live_bands[label]["points"],
                "holdout_days": live_bands[label]["holdout_days"],
                "holdout_coverage": (sum(holdout_by_horizon[label]) / len(holdout_by_horizon[label])
                                     if holdout_by_horizon[label] else None),
                "holdout_points": len(holdout_by_horizon[label]),
            } for label, _, _ in HORIZONS],
        },
    }
