"""HTTP API and ingestion loop for the local Home Assistant App."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

try:
    from .ha_client import HAClient, HAError, collect_price_data, parse_dt
    from .storage import Store, price_archive_key, utc_iso
    from .weather import WeatherError, fetch_ha_weather, fetch_open_meteo, model_weather
except ImportError:  # ``python app/backend.py`` in the container entrypoint
    from ha_client import HAClient, HAError, collect_price_data, parse_dt
    from storage import Store, price_archive_key, utc_iso
    from weather import WeatherError, fetch_ha_weather, fetch_open_meteo, model_weather

UTC = timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
LOG = logging.getLogger("stroomvoorspeller")
POLL_SECONDS = 300
WEATHER_MAX_AGE = timedelta(hours=1)
VALID_UNITS = {"EUR/kWh", "EUR/MWh"}
VALID_PRICE_FIELDS = {"tax_included", "tax_excluded"}
VALID_WINDOWS = {1, 2, 3, 5}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping): return obj.get(name, default)
    return getattr(obj, name, default)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime): return utc_iso(value)
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    if isinstance(value, Mapping): return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_json_safe(v) for v in value]
    return str(value)


def _load_forecaster():
    try:
        from .model import forecast_quarters
    except ImportError:
        from model import forecast_quarters
    return forecast_quarters


def _parse_entity_state_price(state: Mapping[str, Any], selected_unit: str) -> float | None:
    if not state or state.get("state") in (None, "unknown", "unavailable", "none", ""):
        return None
    try:
        raw = float(state["state"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw): return None
    attrs = state.get("attributes") or {}
    actual_unit = attrs.get("unit_of_measurement")
    if actual_unit:
        actual_unit = {"€/kWh": "EUR/kWh", "€/MWh": "EUR/MWh"}.get(actual_unit, actual_unit)
        if actual_unit != selected_unit:
            return None
    return raw


def _amsterdam_day_utc_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=AMSTERDAM)
    finish = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=AMSTERDAM)
    return start.astimezone(UTC), finish.astimezone(UTC)


def _window_candidates(slots: list[dict[str, Any]], hours: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    count = hours * 4
    candidates = {"known": [], "mixed": []}
    for i in range(max(0, len(slots) - count + 1)):
        subset = slots[i:i + count]
        if len(subset) != count or any(s["price"] is None or s["status"] == "missing" for s in subset):
            continue
        starts = [parse_dt(s["start"]) for s in subset]
        if any(starts[j] - starts[j - 1] != timedelta(minutes=15) for j in range(1, len(starts))):
            continue
        end = parse_dt(subset[-1]["end"])
        if end - starts[0] != timedelta(hours=hours):
            continue
        known = all(s["status"] == "known" for s in subset)
        category = "known" if known else "mixed"
        candidates[category].append({"start": subset[0]["start"], "end": subset[-1]["end"],
                                     "average_price": round(sum(float(s["price"]) for s in subset) / len(subset), 6),
                                     "slots": count, "status": category})
    for category in candidates:
        candidates[category].sort(key=lambda x: x["average_price"])
    return _non_overlapping(candidates["known"]), _non_overlapping(candidates["mixed"])


def _non_overlapping(items: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in items:
        a, b = parse_dt(item["start"]), parse_dt(item["end"])
        if all(b <= parse_dt(old["start"]) or a >= parse_dt(old["end"]) for old in kept):
            kept.append(item)
            if len(kept) == limit: break
    return kept


class AppService:
    def __init__(self, store: Store | None = None, ha: HAClient | None = None,
                 open_meteo_fetcher=fetch_open_meteo, clock=lambda: datetime.now(UTC)):
        self.store = store or Store()
        self.ha = ha or HAClient()
        self.open_meteo_fetcher = open_meteo_fetcher
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_price_error: str | None = None
        self._last_weather_error: str | None = None
        self._last_model_error: str | None = None
        self._maturity_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    @staticmethod
    def _archive_entity(entity: str, settings: Mapping[str, Any]) -> str:
        """Separate prices that represent different fields or units.

        The selected field is part of the archive identity because a tariff's
        tax-included and tax-excluded values are not interchangeable. A unit
        without an HA unit attribute is also user-selected and must not be
        silently mixed with a later scale choice.
        """
        return price_archive_key(entity, settings.get("price_field", "tax_included"), settings.get("tariff_unit"))

    def get_settings(self) -> dict[str, Any]:
        value = self.store.get_settings()
        value.setdefault("tariff_entity", "")
        value.setdefault("tariff_unit", "")
        value.setdefault("price_field", "tax_included")
        value.setdefault("weather_source", "open_meteo")
        value.setdefault("weather_entities", {"solar": "", "wind": "", "temperature": ""})
        value.setdefault("latitude", None); value.setdefault("longitude", None)
        return value

    def put_settings(self, body: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"tariff_entity", "tariff_unit", "price_field", "weather_source", "weather_entities", "latitude", "longitude"}
        if set(body) - allowed:
            raise ValueError("Onbekende instellingenvelden")
        current = self.get_settings()
        merged = {**current, **body}
        entity = merged.get("tariff_entity") or ""
        if entity and not re.fullmatch(r"sensor\.[a-z0-9_]+", entity):
            raise ValueError("Kies een sensor-entiteit uit Home Assistant")
        unit = merged.get("tariff_unit") or ""
        if unit and unit not in VALID_UNITS:
            raise ValueError("Tariefunit moet EUR/kWh of EUR/MWh zijn")
        price_field = merged.get("price_field")
        if price_field not in VALID_PRICE_FIELDS:
            raise ValueError("Kies inclusief of exclusief belasting voor de tariefreeks")
        source = merged.get("weather_source")
        if source not in {"open_meteo", "ha"}:
            raise ValueError("Weerbron moet Open-Meteo of Home Assistant zijn")
        entities = merged.get("weather_entities") or {}
        if not isinstance(entities, Mapping):
            raise ValueError("Weerentiteiten hebben een ongeldig formaat")
        cleaned_entities = {}
        for key in ("solar", "wind", "temperature"):
            value = str(entities.get(key) or "")
            if value and not re.fullmatch(r"sensor\.[a-z0-9_]+", value):
                raise ValueError(f"{key}: kies een sensor-entiteit")
            cleaned_entities[key] = value
        lat, lon = merged.get("latitude"), merged.get("longitude")
        if lat in ("", None) and lon in ("", None):
            lat = lon = None
        else:
            try: lat, lon = float(lat), float(lon)
            except (TypeError, ValueError) as exc: raise ValueError("Vul breedtegraad en lengtegraad in") from exc
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Coördinaten vallen buiten het geldige bereik")
        new = {"tariff_entity": entity, "tariff_unit": unit, "price_field": price_field,
               "weather_source": source, "weather_entities": cleaned_entities, "latitude": lat, "longitude": lon}
        if any(new[key] != current.get(key) for key in ("tariff_entity", "price_field", "tariff_unit")):
            new["history_cursor_utc"] = None
        return self.store.set_settings(new)

    def entity_list(self) -> dict[str, Any]:
        try:
            entities = self.ha.entities()
            return {"entities": entities, "error": None}
        except Exception as exc:
            return {"entities": [], "error": str(exc)}

    def _suggested_coordinates(self) -> dict[str, Any] | None:
        try:
            config = self.ha.config()
            lat, lon = config.get("latitude"), config.get("longitude")
            if lat is not None and lon is not None:
                lat, lon = float(lat), float(lon)
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return {"latitude": lat, "longitude": lon, "source": "Home Assistant-configuratie; alleen voorstel"}
        except Exception:
            pass
        return None

    def status(self) -> dict[str, Any]:
        settings = self.get_settings()
        entity = settings.get("tariff_entity") or ""
        price_field = settings.get("price_field", "tax_included")
        archive_entity = self._archive_entity(entity, settings) if entity else ""
        sources = self.store.source_statuses()
        run, _ = self.store.latest_forecast(entity if entity else None, price_field if entity else None,
                                           settings.get("tariff_unit") if entity else None)
        archive = self.store.archive_summary(archive_entity) if entity else {"quarter_count": 0, "first_start": None, "last_end": None, "coverage_days": 0}
        errors = []
        for source, error in (("price", self._last_price_error), ("weather", self._last_weather_error), ("model", self._last_model_error)):
            if error is None:
                error = sources.get(source, {}).get("error")
            if error: errors.append({"source": source, "message": error})
        ps = sources.get("price", {}); ws = sources.get("weather", {})
        price_detail = ps.get("detail") or {}
        price_status_matches = (price_detail.get("entity_id") == entity
                                and price_detail.get("price_field", "tax_included") == price_field
                                and price_detail.get("tariff_unit") == settings.get("tariff_unit"))
        price_last_success = ps.get("last_success") if price_status_matches else None
        stale = not price_last_success or self.clock().astimezone(UTC) - parse_dt(price_last_success) > timedelta(minutes=20)
        missing_inputs = []
        if not ws.get("last_success"): missing_inputs.append("verwachte zon, wind en temperatuur")
        if not settings.get("tariff_unit"): missing_inputs.append("tariefunit in instellingen")
        maturity = self._maturity(entity, self.clock().astimezone(UTC), archive, price_field,
                                  archive_entity, settings.get("tariff_unit")) if entity else {
            "ready": False, "archive_ready": False, "mature_daily_runs": 0, "required_daily_runs": 7,
            "metrics": None, "daily": [], "reasons": ["Kies eerst een tariefentiteit"]}
        reasons = list(run.get("reasons", [])) if run else ["Nog geen modelrun"]
        reasons.extend(maturity["reasons"])
        reasons.append("Kwartieronzekerheidsband is niet gekalibreerd")
        quality = {"label": "geëvalueerd" if maturity["ready"] else "voorlopig",
                   "reasons": sorted(set(reasons)),
                   "missing_inputs": missing_inputs, "coverage": archive.get("coverage_days", 0),
                   "coverage_ratio": archive.get("coverage_ratio", 0), "maturity": maturity}
        return {"ok": True, "configured": bool(entity), "stale": stale, "errors": errors,
                "last_price_update": price_last_success, "last_weather_update": ws.get("last_success"),
                "last_model_update": run.get("issued_at") if run else None,
                "model_version": run.get("model_version") if run else None,
                "quality": quality, "archive": archive,
                "sources": {"tariff_entity": entity or None, "weather_source": settings.get("weather_source")},
                "location_suggestion": self._suggested_coordinates()}

    def _maturity(self, entity: str, now: datetime, archive: Mapping[str, Any],
                  price_field: str = "tax_included", archive_entity: str | None = None,
                  tariff_unit: str | None = None) -> dict[str, Any]:
        """Score saved forecasts against later actuals without changing their inputs.

        A daily comparison is usable only with at least 92 of the first 96
        forecast quarters realized and matched to causal seasonal-baseline
        points. MAE/bias are descriptive;
        this gate does not claim the model wins or that an uncertainty band is
        calibrated.
        """
        archive_entity = archive_entity or entity
        latest_run, _ = self.store.latest_forecast(entity, price_field, tariff_unit)
        cache_key = (archive.get("last_end"), latest_run.get("id") if latest_run else None,
                     now.astimezone(AMSTERDAM).date().isoformat())
        cache_scope = f"{entity}|{price_field}|{tariff_unit}|{archive_entity}"
        cached = self._maturity_cache.get(cache_scope)
        if cached and cached[0] == cache_key:
            return cached[1]

        from .backtest import seasonal_quarter_baseline

        actuals = self.store.actual_quarters(archive_entity, now)
        actual_by_dt = {parse_dt(k): v for k, v in actuals.items()}
        candidates = self.store.stored_forecasts(entity, now - timedelta(days=30), price_field, tariff_unit)
        by_day: dict[str, dict[str, Any]] = {}
        for snapshot in reversed(candidates):
            issued = parse_dt(snapshot["issued_at"])
            local_day = issued.astimezone(AMSTERDAM).date().isoformat()
            if local_day in by_day:
                continue
            point_starts = [parse_dt(point["start_utc"]) for point in snapshot.get("points", [])]
            if not point_starts:
                continue
            horizon_start = min(point_starts)
            horizon_end = horizon_start + timedelta(hours=24)
            if now < horizon_end:
                continue
            predictions: dict[datetime, float] = {}
            for point in snapshot.get("points", []):
                start = parse_dt(point["start_utc"])
                end = parse_dt(point["end_utc"])
                try: price = float(point["price"])
                except (TypeError, ValueError): continue
                if (horizon_start <= start < horizon_end and end <= horizon_end
                        and math.isfinite(price) and start in actual_by_dt):
                    predictions[start] = price
            if not predictions:
                continue
            baseline = seasonal_quarter_baseline(snapshot.get("history", []), issued, predictions)
            pairs = [(start, prediction, actual_by_dt[start]) for start, prediction in predictions.items()
                     if start in baseline]
            if len(pairs) < 92:
                continue
            errors = [prediction - actual for _, prediction, actual in pairs]
            baseline_errors = [baseline[start] - actual for start, _, actual in pairs]
            by_day[local_day] = {
                "date": local_day, "issued_at": utc_iso(issued), "n": len(pairs),
                "mae": sum(abs(error) for error in errors) / len(errors),
                "bias": sum(errors) / len(errors),
                "baseline_mae": sum(abs(error) for error in baseline_errors) / len(baseline_errors),
                "band_coverage": None, "band_n": 0,
            }

        daily = sorted(by_day.values(), key=lambda row: row["date"])[-7:]
        archive_ready = (float(archive.get("coverage_days", 0) or 0) >= 35
                         and float(archive.get("coverage_ratio", 0) or 0) >= .95)
        ready = archive_ready and len(daily) >= 7
        reasons = []
        if not archive_ready:
            reasons.append("Minimaal 35 dagen met ten minste 95% kwartierdekking ontbreken")
        if len(daily) < 7:
            reasons.append(f"{7-len(daily)} van 7 volwassen dagelijkse vergelijkingen ontbreken")
        if daily:
            n = sum(row["n"] for row in daily)
            metrics = {"daily_runs": len(daily), "n": n,
                       "mae": sum(row["mae"] * row["n"] for row in daily) / n,
                       "bias": sum(row["bias"] * row["n"] for row in daily) / n,
                       "baseline_mae": sum(row["baseline_mae"] * row["n"] for row in daily) / n,
                       "band_coverage": None, "band_n": 0,
                       "interpretation": "beschrijvende vergelijking; geen nauwkeurigheids- of banddekkingsclaim"}
        else:
            metrics = None
        result = {"ready": ready, "archive_ready": archive_ready,
                  "mature_daily_runs": len(daily), "required_daily_runs": 7,
                  "metrics": metrics, "daily": daily, "reasons": reasons}
        self._maturity_cache[cache_scope] = (cache_key, result)
        return result

    def _weather_snapshot(self, settings: Mapping[str, Any], now: datetime, force: bool = False) -> dict[str, Any] | None:
        old = self.store.latest_snapshot("weather", successful_only=True)
        expected_source = settings.get("weather_source")
        if old:
            old_payload = old["payload"]
            matches = old_payload.get("source") == expected_source
            if expected_source == "open_meteo":
                matches = matches and old_payload.get("location") == {"latitude": settings.get("latitude"),
                                                                         "longitude": settings.get("longitude")}
            else:
                matches = matches and old_payload.get("entities") == settings.get("weather_entities")
            if not matches:
                # Source changes are local configuration changes and require an
                # immediate new input snapshot; old data must not mask them.
                old = None
        if expected_source == "open_meteo":
            attempt_record = self.store.latest_snapshot("weather_attempt:open_meteo")
            attempted = parse_dt(attempt_record["issued_at"]) if attempt_record else None
            # Only the external Open-Meteo adapter is capped at one request per hour.
            if attempted and now - attempted < WEATHER_MAX_AGE:
                return old["payload"] if old else None
            if old and now - parse_dt(old["issued_at"]) < WEATHER_MAX_AGE:
                return old["payload"]
            lat, lon = settings.get("latitude"), settings.get("longitude")
            if lat is None or lon is None:
                suggestion = self._suggested_coordinates()
                if suggestion:
                    raise WeatherError("Bevestig de voorgestelde HA-coördinaten in Instellingen voor Open-Meteo")
                raise WeatherError("Vul coördinaten in om Open-Meteo te gebruiken")
            # Persist provider-specific throttling before the network request so
            # switching HA/Open-Meteo or restarting cannot bypass the hourly cap.
            self.store.save_snapshot(now, "weather_attempt:open_meteo",
                                     {"location": {"latitude": lat, "longitude": lon}})
            snapshot = self.open_meteo_fetcher(lat, lon, now)
            snapshot["location"] = {"latitude": lat, "longitude": lon}
            return snapshot
        snapshot = fetch_ha_weather(self.ha, settings.get("weather_entities") or {}, now)
        snapshot["entities"] = dict(settings.get("weather_entities") or {})
        return snapshot

    def refresh(self, force_weather: bool = False) -> dict[str, Any]:
        with self._lock:
            now = self.clock().astimezone(UTC)
            settings = self.get_settings()
            entity = settings.get("tariff_entity")
            if not entity:
                return {"ok": False, "message": "Kies eerst een tariefentiteit in Instellingen"}
            price_status = self.store.source_statuses().get("price", {})
            price_detail = price_status.get("detail") or {}
            same_price_source = (price_detail.get("entity_id") == entity and
                                 price_detail.get("price_field", "tax_included") == settings.get("price_field", "tax_included") and
                                 price_detail.get("tariff_unit") == settings.get("tariff_unit"))
            last_attempt = price_status.get("last_attempt") if same_price_source else None
            if last_attempt and now - parse_dt(last_attempt) < timedelta(minutes=1):
                return {"ok": True, "throttled": True, "message": "De prijsbron is zojuist bijgewerkt"}
            cursor_raw = settings.get("history_cursor_utc")
            cursor = parse_dt(cursor_raw) - timedelta(hours=1) if cursor_raw else now - timedelta(days=35)
            try:
                quarter_rows, hourly_rows, observed = collect_price_data(
                    self.ha, entity, now, history_days=35, tariff_unit=settings.get("tariff_unit") or None,
                    price_field=settings.get("price_field", "tax_included"), history_start=cursor)
                # HA history state semantics are not guaranteed to match the
                # selected tax-exclusive forecast field. Retain only the
                # explicitly selected published forecast values in that mode.
                if settings.get("price_field", "tax_included") == "tax_excluded":
                    quarter_rows = [row for row in quarter_rows if row.get("source") == "ha_forecast"]
                    hourly_rows = []
                archive_entity = self._archive_entity(entity, settings)
                for row in quarter_rows:
                    row["entity_id"] = archive_entity
                for row in hourly_rows:
                    row["entity_id"] = archive_entity
                # The compound key prevents entity, tax field, or scale changes
                # from mixing price values in one archive.
                self.store.upsert_quarters(quarter_rows)
                self.store.upsert_hourly(hourly_rows)
                settings["history_cursor_utc"] = utc_iso(now)
                self.store.set_settings({"history_cursor_utc": settings["history_cursor_utc"]})
                self.store.set_source_status("price", success=True, attempted_at=now,
                                             detail={"entity_id": entity, "price_field": settings.get("price_field", "tax_included"),
                                                     "tariff_unit": settings.get("tariff_unit"),
                                                     "archive_entity_id": archive_entity,
                                                     "rows": len(quarter_rows), "cursor": utc_iso(now)})
                self._last_price_error = None
                self.store.save_snapshot(now, "price", {"entity_id": entity, "count": len(quarter_rows),
                                                         "last_known_start": max((utc_iso(r["start_utc"]) for r in quarter_rows), default=None)})
            except Exception as exc:
                self._last_price_error = str(exc)
                self.store.set_source_status("price", success=False, attempted_at=now, error=str(exc))
                self.store.save_snapshot(now, "price", {}, error=str(exc))
                return {"ok": False, "message": str(exc)}

            weather: dict[str, Any] | None = None
            try:
                prior_weather = self.store.latest_snapshot("weather", successful_only=True)
                weather = self._weather_snapshot(settings, now, force_weather)
                weather_is_new = bool(weather) and (not prior_weather or
                    weather.get("observed_at") != prior_weather["payload"].get("observed_at"))
                if weather_is_new:
                    self.store.save_snapshot(now, "weather", weather)
                    self.store.set_source_status("weather", success=True, attempted_at=now,
                                                 detail={"source": weather.get("source"), "hour_count": len(weather.get("hourly", {}))})
                self._last_weather_error = None
            except Exception as exc:
                self._last_weather_error = str(exc)
                self.store.set_source_status("weather", success=False, attempted_at=now, error=str(exc),
                                             detail={"source": settings.get("weather_source")})
                old = self.store.latest_snapshot("weather", successful_only=True)
                if old:
                    weather = old["payload"]

            archive_entity = self._archive_entity(entity, settings)
            history = self.store.quarters(archive_entity, as_of=now)
            if not history:
                return {"ok": False, "message": "Er zijn nog geen historische kwartierprijzen om het model op te starten"}
            try:
                used_weather = model_weather(weather) if weather else None
                # Persist the exact point-in-time input rows used by this run.
                input_rows = [{"start_utc": r["start_utc"], "end_utc": r["end_utc"], "price": r["price"],
                               "published_at": r.get("published_at")} for r in history]
                signature_payload = {"tariff_entity": entity, "tariff_unit": settings.get("tariff_unit"),
                                     "price_field": settings.get("price_field"), "history": input_rows,
                                     "weather": weather}
                signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, separators=(",", ":"),
                                             allow_nan=False, default=str).encode("utf-8")).hexdigest()
                previous_run, _ = self.store.latest_forecast(entity, settings.get("price_field", "tax_included"),
                                                              settings.get("tariff_unit"))
                if previous_run and previous_run.get("inputs", {}).get("signature") == signature:
                    self.store.set_source_status("model", success=True, attempted_at=now,
                                                 detail={"model_version": previous_run["model_version"],
                                                         "unchanged_inputs": True})
                    self._last_model_error = None
                    self.store.prune(now)
                    return {"ok": True, "unchanged": True, "model_version": previous_run["model_version"],
                            "quality": previous_run["quality"]}
                model_unit = settings.get("tariff_unit") or ((history[-1].get("unit") or "EUR/MWh").replace("€/", "EUR/"))
                price_scale = 1000.0 if model_unit == "EUR/kWh" else 1.0
                run = _load_forecaster()(input_rows, issued_at=now, weather=used_weather,
                                         max_points=672, price_scale=price_scale)
                points = list(_field(run, "points", []))
                model_version = str(_field(run, "model_version", "unknown"))
                quality = str(_field(run, "quality", "provisional"))
                reasons = list(_field(run, "reasons", []))
                run_id = str(uuid.uuid4())
                saved_inputs = {"tariff_entity": entity, "tariff_unit": settings.get("tariff_unit"),
                                "price_field": settings.get("price_field"), "weather_snapshot": weather,
                                "weather_model": used_weather,
                                "price_scale": price_scale,
                                "history": input_rows, "history_cursor": settings.get("history_cursor_utc"),
                                "signature": signature}
                self.store.save_forecast(run_id, now, model_version, quality, reasons, saved_inputs,
                                         [{"start_utc": _field(p, "start_utc"), "end_utc": _field(p, "end_utc"),
                                           "price": _field(p, "price"), "lower": _field(p, "lower"),
                                           "upper": _field(p, "upper"), "source": _field(p, "source", "model"),
                                           "quality": _field(p, "quality", quality), "reason": _field(p, "reason")} for p in points])
                self.store.set_source_status("model", success=True, attempted_at=now,
                                             detail={"model_version": model_version, "points": len(points)})
                self._last_model_error = None
                self.store.prune(now)
                return {"ok": True, "model_version": model_version, "points": len(points), "quality": quality}
            except Exception as exc:
                LOG.exception("Forecastberekening mislukt")
                self._last_model_error = str(exc)
                self.store.set_source_status("model", success=False, attempted_at=now, error=str(exc))
                return {"ok": False, "message": str(exc)}

    def dashboard(self, day: date, window_hours: int) -> dict[str, Any]:
        if window_hours not in VALID_WINDOWS:
            raise ValueError("window_hours moet 1, 2, 3 of 5 zijn")
        settings = self.get_settings(); entity = settings.get("tariff_entity") or ""
        price_field = settings.get("price_field", "tax_included")
        if not entity:
            return {"current": None, "slots": [], "windows": {"known": [], "mixed": []},
                    "quality": {"label": "not_ready", "reasons": ["Kies een tariefentiteit"]},
                    "updated_at": None, "model_version": None}
        start, end = _amsterdam_day_utc_bounds(day)
        archive_entity = self._archive_entity(entity, settings)
        known_rows = self.store.quarters(archive_entity, start, end)
        known_map = {r["start_utc"]: r for r in known_rows}
        run, forecast_points = self.store.latest_forecast(entity, price_field, settings.get("tariff_unit"))
        pred_map = {r["start_utc"]: r for r in forecast_points}
        slots: list[dict[str, Any]] = []
        cursor = start
        now = self.clock().astimezone(UTC)
        while cursor < end:
            start_s = utc_iso(cursor); end_dt = cursor + timedelta(minutes=15); end_s = utc_iso(end_dt)
            k = known_map.get(start_s)
            p = pred_map.get(start_s)
            if k:
                slot = {"start": start_s, "end": end_s, "price": k["price"], "unit": k["unit"],
                        "status": "known", "source": k["source"], "lower": None, "upper": None, "reason": None}
            elif p:
                unit = settings.get("tariff_unit") or "EUR/kWh"
                slot = {"start": start_s, "end": end_s, "price": p["price"], "unit": unit,
                        "status": "predicted", "source": p["source"], "lower": p["lower_price"],
                        "upper": p["upper_price"], "reason": p["reason"]}
            else:
                slot = {"start": start_s, "end": end_s, "price": None,
                        "unit": settings.get("tariff_unit") or None, "status": "missing", "source": None,
                        "lower": None, "upper": None, "reason": "Geen bekende prijs of voorspelling"}
            slots.append(slot); cursor = end_dt
        known_windows, mixed_windows = _window_candidates(slots, window_hours)
        current_start = utc_iso(datetime.fromtimestamp((now.timestamp() // 900) * 900, UTC))
        current = next((s for s in slots if s["start"] == current_start), None)
        archive = self.store.archive_summary(archive_entity)
        maturity = self._maturity(entity, now, archive, price_field, archive_entity, settings.get("tariff_unit"))
        quality_reasons = list(run["reasons"]) if run else ["Nog geen voorspelling"]
        quality_reasons.extend(maturity["reasons"])
        quality_reasons.append("Kwartieronzekerheidsband is niet gekalibreerd")
        forecast_quality = {"label": "geëvalueerd" if maturity["ready"] else "voorlopig",
                            "reasons": sorted(set(quality_reasons)),
                            "uncertainty": "Kwartieronzekerheidsband is niet gekalibreerd",
                            "maturity": maturity}
        return {"current": current, "slots": slots,
                "windows": {"known": known_windows, "mixed": mixed_windows},
                "quality": forecast_quality,
                "updated_at": run["issued_at"] if run else None,
                "model_version": run["model_version"] if run else None,
                "archive": archive,
                "source": {"tariff_entity": entity, "price_field": settings.get("price_field"),
                           "weather_source": settings.get("weather_source")}}

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="price-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread: self._thread.join(timeout=5)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try: self.refresh()
            except Exception: LOG.exception("Onverwachte fout in updatecyclus")
            self._stop.wait(POLL_SECONDS)


def make_handler(service: AppService, static_dir: str | None = None):
    from pathlib import Path
    from urllib.parse import unquote
    root = Path(static_dir) if static_dir else Path(__file__).resolve().parent / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "Stroomvoorspeller/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s %s", self.address_string(), fmt % args)

        def _path(self) -> tuple[str, dict[str, list[str]]]:
            split = urlsplit(self.path)
            path = split.path
            marker = path.find("/api/")
            if marker >= 0: path = path[marker:]
            elif path.endswith("/api"): path = "/api"
            return path, parse_qs(split.query)

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                raw = json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
            else: raw = payload
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw))); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(raw)

        def do_GET(self) -> None:
            path, query = self._path()
            try:
                if path == "/api/health": return self._send(200, {"ok": True, "service": "stroomvoorspeller"})
                if path == "/api/status": return self._send(200, service.status())
                if path == "/api/entities": return self._send(200, service.entity_list())
                if path == "/api/settings":
                    result = service.get_settings(); result["location_suggestion"] = service._suggested_coordinates()
                    return self._send(200, result)
                if path == "/api/dashboard":
                    day_raw = (query.get("date") or [datetime.now(AMSTERDAM).date().isoformat()])[0]
                    try: day = date.fromisoformat(day_raw)
                    except ValueError: return self._send(400, {"error": "date moet YYYY-MM-DD zijn"})
                    hours = int((query.get("window_hours") or ["1"])[0])
                    return self._send(200, service.dashboard(day, hours))
                if path == "/api/refresh": return self._send(200, service.refresh())
                if path in {"/", "/index.html"}:
                    index = root / "index.html"
                    if index.is_file(): return self._send(200, index.read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/") and "/" not in path[1:]:
                    candidate = (root / unquote(path.lstrip("/"))).resolve()
                    if root.resolve() in candidate.parents and candidate.is_file():
                        typ = "text/plain; charset=utf-8"
                        if candidate.suffix == ".js": typ = "application/javascript; charset=utf-8"
                        elif candidate.suffix == ".css": typ = "text/css; charset=utf-8"
                        elif candidate.suffix == ".svg": typ = "image/svg+xml"
                        return self._send(200, candidate.read_bytes(), typ)
                return self._send(404, {"error": "not found"})
            except (ValueError, TypeError) as exc: return self._send(400, {"error": str(exc)})
            except Exception as exc:
                LOG.exception("GET %s mislukt", path)
                return self._send(503, {"error": str(exc)})

        def do_PUT(self) -> None:
            path, _ = self._path()
            if path != "/api/settings": return self._send(404, {"error": "not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 64 * 1024: return self._send(413, {"error": "request body te groot of leeg"})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict): return self._send(400, {"error": "JSON-object verwacht"})
                return self._send(200, service.put_settings(body))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                LOG.exception("Settings opslaan mislukt")
                return self._send(503, {"error": str(exc)})

        def do_POST(self) -> None:
            return self._send(405, {"error": "alleen PUT /api/settings is beschikbaar; HA wordt uitsluitend gelezen"})

        def do_DELETE(self) -> None:
            return self._send(405, {"error": "method not allowed"})

        def do_PATCH(self) -> None:
            return self._send(405, {"error": "method not allowed"})

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8099) -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app = AppService(); app.start()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    LOG.info("Listening on %s:%d (Ingress only; no host port binding)", host, port)
    try: server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt: pass
    finally:
        server.shutdown(); server.server_close(); app.stop()


if __name__ == "__main__":
    serve(port=int(os.environ.get("PORT", "8099")))
