"""Separate live scores from bounded historical reconstructions of price archives."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from .methods import METHODS, adaptive_forecast, timestral_forecast, epex_forecast, configured_version
from .model import forecast_quarters
from .storage import utc_iso

UTC = timezone.utc


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _scores(rows):
    errors = [prediction - actual for prediction, actual in rows]
    return {"n": len(errors), "mae": mean(abs(e) for e in errors) if errors else None,
            "bias": mean(errors) if errors else None}


def comparison(service):
    settings = service.get_settings()
    entity = service._price_identity(settings)
    now = service.clock().astimezone(UTC)
    truth = service.store.published_quarters(service._archive_entity(entity, settings), now) if entity else {}
    statuses = service.store.source_statuses()
    result = {"active_method": settings["active_method"], "unit": settings["tariff_unit"],
              "methods": [], "history_job": dict(service._history_job),
              "historical_notice": "Historische reconstructie met het huidige prijsarchief, zonder de oorspronkelijke bronversies. Epex gebruikt gereconstrueerde historische weerdata. Geen live prestatiebewijs.",
              "pairing_notice": "Gepaarde MAE gebruikt dezelfde uitgiftetijd en doelkwartieren van ingeschakelde methoden met resultaten."}
    maps = {"live": {}, "historical": {}}
    for method, label in METHODS.items():
        if method == "timestral": label = f"Timestral-{settings['timestral_model']} (lokaal)"
        item = {"id": method, "label": label, "enabled": settings["forecast_methods"][method],
                "status": statuses.get("method:" + method, {}), "historical_supported": True}
        for mode in maps:
            latest, _ = service.store.latest_forecast(entity, settings["price_field"], settings["tariff_unit"],
                                                      method=method, evaluation_mode=mode,
                model_version=configured_version(method, settings)) if entity else (None, [])
            version = latest["model_version"] if latest else None
            runs = service.store.daily_stored_forecasts(entity, now - timedelta(days=45), now,
                settings["price_field"], settings["tariff_unit"], method=method,
                model_version=version, evaluation_mode=mode) if latest else []
            days, zone = {}, ZoneInfo("Europe/Amsterdam")
            for run in runs:
                day = _dt(run["issued_at"]).astimezone(zone).date()
                noon = datetime.combine(day, datetime.min.time(), zone) + timedelta(hours=12)
                key = (abs((_dt(run["issued_at"]) - noon).total_seconds()), run["issued_at"])
                if day not in days or key < days[day][0]:
                    days[day] = key, run
            scored, buckets = {}, {"0-24u": [], "24-48u": [], "48-168u": []}
            for _, run in days.values():
                issue = _dt(run["issued_at"])
                for point in run["points"]:
                    start = point["start_utc"]
                    if start not in truth or point.get("price") is None:
                        continue
                    hours = (_dt(start) - issue).total_seconds() / 3600
                    if not 0 <= hours < 168:
                        continue
                    pair = float(point["price"]), truth[start]
                    if not all(math.isfinite(v) for v in pair):
                        continue
                    scored[(run["issued_at"], start)] = pair
                    buckets["0-24u" if hours < 24 else "24-48u" if hours < 48 else "48-168u"].append(pair)
            maps[mode][method] = scored
            item[mode] = {**_scores(scored.values()), "version": version, "days": len(days),
                          "horizons": [{"horizon": k, **_scores(v)} for k, v in buckets.items()]}
        result["methods"].append(item)
    for mode, methods in maps.items():
        available = [key for key, values in methods.items() if values and settings["forecast_methods"][key]]
        shared = set.intersection(*(set(methods[key]) for key in available)) if len(available) >= 2 else set()
        for item in result["methods"]:
            item[mode]["paired"] = (_scores(methods[item["id"]][key] for key in shared)
                                     if item["id"] in available else _scores([]))
    return result


def start_history_test(service):
    """Single background job; at most seven daily issues and 48h each."""
    with service._history_job_lock:
        if service._history_thread and service._history_thread.is_alive():
            return dict(service._history_job)
        settings = service.get_settings()
        entity = service._price_identity(settings)
        methods = [m for m, enabled in settings["forecast_methods"].items() if enabled]
        if not methods:
            raise ValueError("Schakel een rekenmethode in voor historische tests")
        if not entity:
            raise ValueError("Kies eerst een prijsbron")
        now = service.clock().astimezone(UTC)
        rows = service.store.quarters(service._archive_entity(entity, settings), end=now)
        rows = [r for r in rows if r.get("quality") in {"valid", "published"} and _dt(r["end_utc"]) <= now]
        if not rows or _dt(rows[-1]["end_utc"]) - _dt(rows[0]["start_utc"]) < timedelta(days=16):
            raise ValueError("Minimaal 16 dagen echte kwartierhistorie nodig; vernieuw eerst de prijsbron")
        service._history_job = {"state": "running", "completed": 0, "total": 0, "errors": [], "methods": methods}
        service._history_thread = threading.Thread(target=_history_worker,
            args=(service, settings, entity, rows, now, methods), daemon=True, name="forecast-history")
        service._history_thread.start()
        return dict(service._history_job)


def _history_worker(service, settings, entity, rows, now, methods):
    job = service._history_job
    try:
        first = _dt(rows[0]["start_utc"]) + timedelta(days=14)
        last = min(now, _dt(rows[-1]["end_utc"])) - timedelta(days=1)
        last = last.replace(hour=0, minute=0, second=0, microsecond=0)
        issues = [last - timedelta(days=d) for d in reversed(range(7)) if last - timedelta(days=d) >= first]
        job["total"] = len(issues) * len(methods)
        scale = 1000 if settings["tariff_unit"] == "EUR/kWh" else 1
        for issue in issues:
            # Imported prices were first observed today: deliberately labelled
            # reconstruction uses ended intervals, never future target prices.
            history = [{**r, "published_at": r["end_utc"]} for r in rows
                       if issue - timedelta(days=90) <= _dt(r["start_utc"]) and _dt(r["end_utc"]) <= issue]
            for method in methods:
                current = service.get_settings()
                if (service._stop.is_set() or service._price_identity(current) != entity
                        or current["price_field"] != settings["price_field"] or current["tariff_unit"] != settings["tariff_unit"]):
                    job["state"] = "cancelled"
                    return
                if not current["forecast_methods"].get(method):
                    job["completed"] += 1
                    continue
                try:
                    if method == "local":
                        run = adaptive_forecast(history, issue, max_points=192, price_scale=scale, base=forecast_quarters)
                    elif method == "v4":
                        run = forecast_quarters(history, issue, max_points=192, price_scale=scale)
                    elif method == "epex":
                        run = epex_forecast(history, issue, service._runtime_settings(settings, method), max_points=192)
                    else:
                        run = timestral_forecast(history, issue, service._runtime_settings(settings, method), max_points=192)
                    inputs = {"method": method, "evaluation_mode": "historical", "tariff_entity": entity,
                              "tariff_unit": settings["tariff_unit"], "price_field": settings["price_field"],
                              "history": history, "weather_model": None, "price_scale": scale, "test_horizon_hours": 48}
                    identity = json.dumps([inputs, run.model_version, utc_iso(issue)], sort_keys=True, default=str)
                    run_id = "historical-" + hashlib.sha256(identity.encode()).hexdigest()
                    inputs["reconstructed_at"] = utc_iso(now)
                    service.store.save_forecast(run_id, issue, run.model_version, "historische reconstructie",
                        [*run.reasons, "Huidig prijsarchief; geen historische weersverwachting/publicatievintage"], inputs,
                        [asdict(point) for point in run.points])
                except Exception as exc:
                    job["errors"] = [*job["errors"], {"method": method, "issued_at": utc_iso(issue), "message": str(exc)}]
                job["completed"] += 1
        job["state"] = "completed" if not job["errors"] else "completed_with_errors"
    except Exception as exc:
        job["state"] = "failed"
        job["errors"] = [*job["errors"], {"message": str(exc)}]
    finally:
        service.store.save_snapshot(now, "history_test", dict(job))
