"""Optional forecasters. No heavy imports, downloads or network work when disabled."""
from __future__ import annotations

import json
import hashlib
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .model import ForecastPoint, ForecastRun, _prepare_history, _quarter_start

UTC = timezone.utc
LOCAL = ZoneInfo("Europe/Amsterdam")
STEP = timedelta(minutes=15)
METHODS = {"local": "Lokaal adaptief", "v4": "Oorspronkelijke v4",
           "epex": "EpexPredictor NL (lokaal)", "timestral": "Timestral (lokaal)"}
VERSIONS = {"local": "adaptive-quarter-1", "epex": "epex-local-nl-1", "timestral": "timestral-local-209m-1"}
TIMESTRAL_REVISION = "9874d252ab0aae16f7f7c999f98f98d6ad7d8365"


def epex_version(settings: Mapping) -> str:
    # A new endpoint or tariff mapping starts its own score/calibration series.
    keys = ("epex_tariff_profile", "epex_training_days", "epex_tariff_confirmed", "epex_multiplier", "epex_offset_eur_kwh",
            "price_source", "price_choice", "tariff_unit", "price_field", "energy_tax_eur_kwh",
            "vat_percent", "supplier_fee_eur_kwh_incl_vat", "tariff_valid_from", "tariff_valid_to")
    digest = hashlib.sha256(json.dumps({k: settings.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()[:12]
    return VERSIONS["epex"] + ":" + digest


def timestral_version(settings: Mapping) -> str:
    payload = [settings.get("timestral_context_days", 28)]
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:12]
    from .timestral_models import model_spec
    size = settings.get("timestral_model", "209M")
    spec = model_spec(size)
    return "timestral-local-" + size.lower() + "-1:" + spec["revision"][:12] + ":" + digest


def configured_version(method: str, settings: Mapping) -> str | None:
    return epex_version(settings) if method == "epex" else timestral_version(settings) if method == "timestral" else None


def _analog(known: Mapping, target: datetime, cutoff: datetime, days: int, level: bool) -> float | None:
    local = target.astimezone(LOCAL)
    candidates = [r for t, r in known["slots"].get((local.hour, local.minute, local.weekday() >= 5), [])
                  if cutoff - timedelta(days=days) <= t < cutoff and r["available_at"] <= cutoff]
    if len(candidates) < 2:
        return None
    value = median(r["price"] for r in candidates)
    if level:
        recent = [r["price"] for t, r in known["rows"] if cutoff - timedelta(days=2) <= t < cutoff and r["available_at"] <= cutoff]
        prior = [r["price"] for t, r in known["rows"] if cutoff - timedelta(days=days) <= t < cutoff and r["available_at"] <= cutoff]
        if len(recent) >= 96 and len(prior) >= 192:
            value += median(recent) - median(prior)
    return value


def adaptive_forecast(history, issued_at, weather=None, max_points=672, price_scale=1.0, *, base):
    """Choose a seasonal analogue on a past-only validation window, blend with v4.

    Fitting uses ended prices only, in tariff units. Future published prices stay
    usable by v4 but cannot train the adaptive correction. No weather is invented.
    """
    run = base(history, issued_at=issued_at, weather=weather,
               max_points=max_points, price_scale=price_scale)
    known = {t: r for t, r in _prepare_history(history, issued_at, 1).items() if t + STEP <= issued_at}
    for row in known.values():
        published = row["published_at"]
        row["available_at"] = (datetime.fromisoformat(published.replace("Z", "+00:00"))
                               if isinstance(published, str) else published) if published else row["end"]
    configs = [(7, False), (14, False), (28, False), (14, True), (28, True)]
    # Hourly-spaced validation targets bound CPU use; every fit excludes its target.
    validation = [(t, r) for t, r in sorted(known.items())
                  if issued_at - timedelta(days=7) <= t and t.minute == 0]
    indexed = {"rows": list(known.items()), "slots": {}}
    for t, row in known.items():
        local = t.astimezone(LOCAL)
        indexed["slots"].setdefault((local.hour, local.minute, local.weekday() >= 5), []).append((t, row))
    scores = []
    for days, level in configs:
        errors = []
        for t, row in validation:
            guess = _analog(indexed, t, t - timedelta(hours=24), days, level)
            if guess is not None:
                errors.append(abs(guess - row["price"]))
        if len(errors) >= 48:
            scores.append((sum(errors) / len(errors), days, level))
    if not scores:
        return ForecastRun(VERSIONS["local"], issued_at, list(run.points), run.quality,
                       [*run.reasons, "Adaptatie wacht op voldoende historische validatiepunten"])
    _, days, level = min(scores)
    points = []
    for point in run.points:
        analog = _analog(indexed, point.start_utc, issued_at, days, level)
        if analog is None:
            points.append(point)
        else:
            # Retain weather-sensitive v4 information, reduce fixed-factor influence.
            price = .25 * point.price + .75 * analog
            delta = price - point.price
            points.append(replace(point, price=price,
                lower=point.lower + delta if point.lower is not None else None,
                upper=point.upper + delta if point.upper is not None else None,
                source="local-adaptive", reason=f"Causaal gekozen {days}-daagse analogie; niveaucorrectie={level}"))
    return replace(run, model_version=VERSIONS["local"], points=points,
                   reasons=[*run.reasons, "Adaptieve mix is experimenteel; nauwkeurigheid wordt afzonderlijk gemeten"])


def _market_to_tariff(value: float, settings: Mapping, start: datetime) -> float:
    """Epex always supplies bare EUR/MWh. Never infer an HA supplier formula."""
    from .energy_prices import all_in_price
    if settings.get("price_source") != "energy_charts_nl":
        if not settings.get("epex_tariff_confirmed"):
            raise ValueError("Bevestig eerst de markt-naar-HA-tariefomrekening voor EpexPredictor")
        from .tariff_profiles import validate_profile_date
        validate_profile_date(settings, start)
        # Explicit affine conversion, e.g. an all-in contract with fixed offset.
        value = value / 1000 * settings["epex_multiplier"] + settings["epex_offset_eur_kwh"]
    else:
        value /= 1000
        if settings.get("price_choice") == "all_in":
            value = all_in_price(value, settings, start)
    return value * (1000 if settings.get("tariff_unit") == "EUR/MWh" else 1)


def epex_forecast(history, issued_at, settings, max_points=672, *, runner=None):
    if settings.get("price_source") != "energy_charts_nl" and not settings.get("epex_tariff_confirmed"):
        raise ValueError("Bevestig eerst de markt-naar-HA-tariefomrekening voor EpexPredictor")
    if settings.get("price_source") != "energy_charts_nl":
        from .tariff_profiles import validate_profile_date
        validate_profile_date(settings, issued_at)
    from .local_runtime import run_local
    payload = (runner or run_local)("epex", {"issued_at": issued_at.isoformat(),
        "training_days": settings.get("epex_training_days", 180), "max_points": max_points}, settings, timeout=600)
    known = _prepare_history(history, issued_at, 1)
    points, seen = [], set()
    stop = _quarter_start(issued_at) + max_points * STEP
    for row in payload["prices"]:
        start = datetime.fromisoformat(row["startsAt"].replace("Z", "+00:00"))
        if start.tzinfo is None or start.second or start.microsecond or start.minute % 15:
            raise ValueError("EpexPredictor levert ongeldige kwartierstart")
        start = start.astimezone(UTC)
        if start in seen:
            raise ValueError("Dubbele EpexPredictor-kwartieren")
        seen.add(start)
        value = float(row["total"])
        if not math.isfinite(value):
            raise ValueError("EpexPredictor levert een niet-eindige prijs")
        # Locally known prices are not predictions and never enter scoring.
        if start < issued_at or start >= stop or start in known:
            continue
        price = _market_to_tariff(value, settings, start)
        points.append(ForecastPoint(start, start + STEP, price, source="epex-local",
                                   reason="Ingebouwd EpexPredictor NL-model, lokaal getraind met markt- en weerdata"))
    if not points:
        raise ValueError("EpexPredictor leverde geen onbekende toekomstige kwartieren")
    return ForecastRun(epex_version(settings), issued_at, sorted(points, key=lambda p: p.start_utc), "voorlopig",
                       ["Lokale EpexPredictor NL single-stage; marktdata Energy-Charts, weerdata Open-Meteo",
                        "NL-model zonder internationale tweede modelstap; prestaties afzonderlijk evalueren",
                        "Weer uit lokale cache" if payload.get("weather_cached") else "Weerbron ververst tijdens deze run"])


def timestral_forecast(history, issued_at, settings, max_points=672, *, runner=None):
    from .local_runtime import run_local
    known = _prepare_history(history, issued_at, 1)
    if not known:
        raise ValueError("Timestral heeft kwartierhistorie nodig")
    # Use a regular UTC grid, preserve gaps as null, and include already-published
    # contiguous day-ahead prices in context. Never interpolate target prices.
    end = _quarter_start(issued_at)
    while end in known:
        end += STEP
    length = settings.get("timestral_context_days", 28) * 96
    begin = max(min(known), end - length * STEP)
    context = []
    cursor = begin
    while cursor < end:
        context.append(known[cursor]["price"] if cursor in known else None)
        cursor += STEP
    if sum(v is not None for v in context) < 192:
        raise ValueError("Timestral heeft minimaal 192 echte kwartieren nodig")
    horizon = min(max_points, max(0, int((_quarter_start(issued_at) + max_points * STEP - end) / STEP)))
    if horizon < 1:
        raise ValueError("Alle kwartieren binnen de horizon zijn al bekend")
    payload = (runner or run_local)("timestral", {"context": context, "prediction_length": horizon}, settings, timeout=180)
    from .timestral_models import model_spec
    spec = model_spec(settings.get("timestral_model", "209M"))
    if payload.get("model") != spec["repo"] or payload.get("revision") != spec["revision"]:
        raise ValueError("Lokale Timestral-module gebruikt niet het ingestelde model")
    arrays = [payload.get(key) for key in ("median", "lower", "upper")]
    if any(not isinstance(a, list) or len(a) != horizon for a in arrays):
        raise ValueError("Lokale Timestral-module levert onjuiste voorspellinglengte")
    points = []
    for i, values in enumerate(zip(*arrays)):
        value, lower, upper = map(float, values)
        if not all(math.isfinite(v) for v in (value, lower, upper)) or not lower <= value <= upper:
            raise ValueError("Ongeldige Timestral-kwantielen")
        start = end + i * STEP
        if start not in known:
            points.append(ForecastPoint(start, start + STEP, value, lower, upper, source="timestral",
                                       reason="Timestral q10–q90; dekking voor dit tarief nog niet gekalibreerd"))
    return ForecastRun(timestral_version(settings), issued_at, points, "voorlopig",
                       [spec["repo"].split("/")[-1] + " op prijshistorie; zonder historische weerscovariaten",
                        "q10–q90 is modelonzekerheid, geen lokaal bewezen 80%-dekking"])
