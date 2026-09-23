"""Point-in-time evaluatie van kwartierprognoses.

Iedere run krijgt uitsluitend de op ``issued_at`` opgeslagen invoersnapshot.
Ontbrekende gerealiseerde kwartieren worden niet ingevuld of geïnterpoleerd.
Dit is een evaluatiehulpmiddel; resultaten worden pas als prestatiebewijs
gebruikt bij voldoende volwassen runs en historische dekking.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import argparse
import json
from math import isfinite
from statistics import mean, median
from typing import Iterable, Mapping

from .model import forecast_quarters

UTC = timezone.utc
QUARTER = timedelta(minutes=15)


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Tijdstip zonder UTC-offset")
    return value.astimezone(UTC)


def _known_history(history: Iterable[Mapping], issued_at: datetime) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for row in history:
        start = _utc(row["start_utc"])
        published = row.get("published_at")
        if published is None and start >= issued_at:
            continue
        if published is not None and _utc(published) > issued_at:
            continue
        price = float(row["price"])
        if isfinite(price):
            out[start] = price
    return out


def seasonal_quarter_baseline(
    history: Iterable[Mapping], issued_at: datetime, starts: Iterable[datetime]
) -> dict[datetime, float]:
    """Eenvoudige, causale basislijn: mediaan van hetzelfde kwartier in 7 dagen."""
    issued_at = _utc(issued_at)
    known = _known_history(history, issued_at)
    result: dict[datetime, float] = {}
    for start in starts:
        start = _utc(start)
        values = [known[start - timedelta(days=day)] for day in range(1, 8)
                  if start - timedelta(days=day) in known]
        if values:
            result[start] = median(values)
    return result


def cheapest_window(
    prices: Mapping[datetime, float], duration_quarters: int
) -> tuple[datetime, float] | None:
    """Kies een venster alleen als ieder volgend UTC-kwartier echt aanwezig is."""
    if duration_quarters <= 0:
        raise ValueError("duur moet positief zijn")
    best: tuple[datetime, float] | None = None
    for start in sorted(prices):
        values = [prices.get(start + QUARTER * offset) for offset in range(duration_quarters)]
        if any(value is None or not isfinite(value) for value in values):
            continue
        score = mean(values)
        if best is None or score < best[1]:
            best = start, score
    return best


@dataclass(frozen=True)
class HorizonMetrics:
    horizon: str
    n: int
    mae: float | None
    bias: float | None
    paired_n: int
    paired_model_mae: float | None
    baseline_mae: float | None
    band_coverage: float | None
    window_regret: float | None
    baseline_window_regret: float | None


def walk_forward(
    snapshots: Iterable[Mapping],
    actuals: Mapping[datetime | str, float],
    *,
    window_quarters: int = 4,
) -> list[HorizonMetrics]:
    """Vergelijk model en basislijn zonder latere invoer in oude runs.

    ``snapshots`` bevat mappings met ``issued_at``, ``history`` en ``weather``.
    ``actuals`` dient uitsluitend om *achteraf* te scoren, nooit als modelinvoer.
    """
    truth = {_utc(start): float(price) for start, price in actuals.items()
             if isfinite(float(price))}
    buckets = {"0-24u": [], "24-48u": [], "48-168u": []}
    for snap in sorted(snapshots, key=lambda row: _utc(row["issued_at"])):
        issued = _utc(snap["issued_at"])
        history = list(snap["history"])
        run = forecast_quarters(
            history, issued, snap.get("weather"),
            price_scale=float(snap.get("price_scale", 1.0)),
        )
        predictions = {point.start_utc: point for point in run.points}
        baseline = seasonal_quarter_baseline(history, issued, predictions)
        for start, point in predictions.items():
            if start not in truth:
                continue
            horizon_hours = (start - issued).total_seconds() / 3600
            bucket = "0-24u" if horizon_hours < 24 else "24-48u" if horizon_hours < 48 else "48-168u"
            lower, upper = point.lower, point.upper
            buckets[bucket].append({
                "start": start,
                "actual": truth[start],
                "forecast": point.price,
                "baseline": baseline.get(start),
                "covered": lower is not None and upper is not None and lower <= truth[start] <= upper,
                "has_band": lower is not None and upper is not None,
                "run": issued,
            })
    result = []
    for horizon, rows in buckets.items():
        errors = [row["forecast"] - row["actual"] for row in rows]
        paired = [row for row in rows if row["baseline"] is not None]
        paired_errors = [abs(row["forecast"] - row["actual"]) for row in paired]
        base_errors = [abs(row["baseline"] - row["actual"]) for row in paired]
        with_band = [row for row in rows if row["has_band"]]
        regrets = []
        baseline_regrets = []
        runs = {row["run"] for row in rows}
        for run in runs:
            subset = [row for row in rows if row["run"] == run]
            # Vergelijk beide keuzes uitsluitend op identieke kwartieren waar
            # gerealiseerde prijs en causale basislijn allebei beschikbaar zijn.
            comparable = [row for row in subset if row["baseline"] is not None]
            actual = {row["start"]: row["actual"] for row in comparable}
            predicted = {row["start"]: row["forecast"] for row in comparable}
            baseline_prices = {row["start"]: row["baseline"] for row in comparable}
            chosen = cheapest_window(predicted, window_quarters)
            baseline_chosen = cheapest_window(baseline_prices, window_quarters)
            oracle = cheapest_window(actual, window_quarters)
            if chosen is None or baseline_chosen is None or oracle is None:
                continue
            for selection, destination in ((chosen, regrets), (baseline_chosen, baseline_regrets)):
                starts = [selection[0] + QUARTER * offset for offset in range(window_quarters)]
                destination.append(mean(actual[start] for start in starts) - oracle[1])
        result.append(HorizonMetrics(
            horizon=horizon,
            n=len(rows),
            mae=mean(map(abs, errors)) if errors else None,
            bias=mean(errors) if errors else None,
            paired_n=len(paired),
            paired_model_mae=mean(paired_errors) if paired_errors else None,
            baseline_mae=mean(base_errors) if base_errors else None,
            band_coverage=mean(row["covered"] for row in with_band) if with_band else None,
            window_regret=mean(regrets) if regrets else None,
            baseline_window_regret=mean(baseline_regrets) if baseline_regrets else None,
        ))
    return result


def evaluate_archive(
    store: object, entity_id: str, price_field: str, tariff_unit: str,
    now: datetime | None = None,
) -> list[HorizonMetrics]:
    """Evalueer bewaarde runs alleen tegen inmiddels verstreken echte kwartieren."""
    from .storage import price_archive_key

    cutoff = _utc(now or datetime.now(UTC))
    snapshots = store.forecast_snapshots(entity_id, price_field=price_field, tariff_unit=tariff_unit)
    archive_key = price_archive_key(entity_id, price_field, tariff_unit)
    actuals = store.actual_quarters(archive_key, cutoff)
    return walk_forward(snapshots, actuals)


def main() -> None:
    """Lokale inspectie: ``python -m app.backtest --data-dir /data --entity-id ...``."""
    from .storage import Store

    parser = argparse.ArgumentParser(description="Point-in-time kwartierbacktest op lokale App-data")
    parser.add_argument("--data-dir", required=True, help="Lokale /data-map van de App")
    parser.add_argument("--entity-id", required=True, help="Te evalueren tariefentiteit")
    parser.add_argument("--price-field", choices=("tax_included", "tax_excluded"), required=True)
    parser.add_argument("--tariff-unit", choices=("EUR/kWh", "EUR/MWh"), required=True)
    args = parser.parse_args()
    store = Store(args.data_dir)
    metrics = evaluate_archive(store, args.entity_id, args.price_field, args.tariff_unit)
    print(json.dumps([asdict(metric) for metric in metrics], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
