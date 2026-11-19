"""Separate live scores from bounded historical reconstructions of price archives."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from .methods import METHODS, adaptive_forecast, timestral_forecast, epex_forecast, configured_version
from .model import forecast_quarters
from .storage import utc_iso

UTC = timezone.utc
HORIZONS = {f"{low}-{low+24}u": (low, low+24) for low in range(0,168,24)}
HORIZONS["48-168u"] = (48,168)
WEATHER_ARCHIVE_VERSION = ":weather-archive-gfs-1"


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _scores(rows):
    errors = [prediction - actual for prediction, actual in rows]
    return {"n": len(errors), "mae": mean(abs(e) for e in errors) if errors else None,
            "bias": mean(errors) if errors else None}


def comparison(service, *, mode="live", horizon="all", selected=None, days=45, include_details=True):
    if mode not in {"live", "historical"} or horizon not in {"all", *HORIZONS}:
        raise ValueError("Kies live/historisch en een bekende voorspelhorizon")
    if type(days) is not int or not 1 <= days <= 45: raise ValueError("Kies 1 t/m 45 dagen")
    if selected is not None and (not isinstance(selected, list) or any(m not in METHODS for m in selected)):
        raise ValueError("Onbekende vergelijkingsmethode")
    view_mode, lookback_days = mode, days
    settings = service.get_settings()
    entity = service._price_identity(settings)
    now = service.clock().astimezone(UTC)
    since = now - timedelta(days=lookback_days)
    selected = [m for m in METHODS if settings["forecast_methods"].get(m, False)] if selected is None else selected
    truth = (service.store.published_quarters(service._archive_entity(entity, settings), now, since=since)
             if entity and (include_details or selected) else {})
    statuses = service.store.source_statuses()
    result = {"active_method": settings["active_method"], "unit": settings["tariff_unit"],
              "methods": [], "history_job": dict(service._history_job),
              "historical_notice": "Historische reconstructies tonen per methode de laatst opgeslagen modelvariant in deze periode. Die kan afwijken van de actieve instellingen. Epex gebruikt gereconstrueerde historische weerdata.",
              "pairing_notice": "Gepaarde MAE gebruikt dezelfde uitgiftetijd en doelkwartieren van ingeschakelde methoden met resultaten."}
    maps = {name: {} for name in (("live", "historical") if include_details else (view_mode,))}
    versions = {}
    if not include_details and truth:
        for method in selected:
            latest, _ = service.store.latest_forecast(entity, settings["price_field"], settings["tariff_unit"],
                method=method, evaluation_mode=view_mode, since=since, include_history=False, include_points=False,
                model_version=configured_version(method, settings) if view_mode == "live" else None)
            if latest:
                versions[method] = latest["model_version"]
    common = service.store.common_daily_forecasts(entity, since, now, settings["price_field"], settings["tariff_unit"],
                                                   versions, view_mode) if versions else {}
    # If no common issue exists, read separate runs to distinguish unavailable methods from non-overlap.
    use_common = bool(common and all(common.values()))
    for method, label in METHODS.items():
        if method not in settings["forecast_methods"] and method not in selected:
            continue
        if method == "timestral": label = f"Timestral-{settings['timestral_model']}" + (" · weekendinfo" if settings["timestral_calendar"] else " · zonder kalender") + (" · met weer" if settings["timestral_weather"] else " · zonder weer")
        item = {"id": method, "label": label, "enabled": settings["forecast_methods"].get(method, False),
                "status": statuses.get("method:" + method, {}), "historical_supported": True}
        for mode in maps:
            if not include_details and method not in selected:
                continue
            if use_common:
                version = versions.get(method)
                runs = common.get(method, [])
            else:
                latest, _ = service.store.latest_forecast(entity, settings["price_field"], settings["tariff_unit"],
                                                          method=method, evaluation_mode=mode,
                    model_version=configured_version(method, settings) if mode == "live" else None,
                    since=since, include_history=False,
                    include_points=False) if truth else (None, [])
                version = latest["model_version"] if latest else None
                runs = service.store.daily_stored_forecasts(entity, since, now,
                    settings["price_field"], settings["tariff_unit"], method=method,
                    model_version=version, evaluation_mode=mode) if latest else []
            scored, buckets = {}, {key: [] for key in HORIZONS}
            for run in runs:
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
                    for name, (low, high) in HORIZONS.items():
                        if low <= hours < high: buckets[name].append(pair)
            maps[mode][method] = scored
            item[mode] = {**_scores(scored.values()), "version": version, "days": len(runs),
                          "horizons": [{"horizon": k, **_scores(v)} for k, v in buckets.items()],
                          "weather_reconstruction": bool(version and version.endswith(WEATHER_ARCHIVE_VERSION))}
            item[mode]["label"] = _comparison_label(item, mode)
        result["methods"].append(item)
    for mode, methods in maps.items():
        available = [key for key, values in methods.items() if values and settings["forecast_methods"].get(key, False)]
        shared = set.intersection(*(set(methods[key]) for key in available)) if len(available) >= 2 else set()
        for item in result["methods"]:
            if mode not in item:
                continue
            item[mode]["paired"] = (_scores(methods[item["id"]][key] for key in shared)
                                     if item["id"] in available else _scores([]))
    view = comparison_view(result, maps, view_mode, horizon, selected, lookback_days)
    participants = view["available"]
    if entity and len(participants) >= 2 and not use_common:
        versions = {m["id"]: m[view_mode]["version"] for m in result["methods"] if m["id"] in participants}
        cohorts = service.store.common_daily_forecasts(entity, since, now,
            settings["price_field"],settings["tariff_unit"],versions,view_mode)
        chart_maps = {view_mode: {m: {} for m in METHODS}}
        for method, runs in cohorts.items():
            for run in runs:
                for point in run["points"]:
                    start = point["start_utc"]
                    if start in truth and point.get("price") is not None and math.isfinite(point["price"]):
                        chart_maps[view_mode][method][run["issued_at"],start] = (point["price"],truth[start])
        aligned = comparison_view(result,chart_maps,view_mode,horizon,view["selected"],lookback_days)
        if aligned["n"]:
            view = aligned
            maps = chart_maps
        else:
            view.update(n=0,paired=False,points=[],daily=[])
            view["metrics"] = [{**m,**_scores([])} for m in view["metrics"]]
    if view["n"]:
        _decision_metrics(service.store, service._archive_entity(entity, settings), view, maps[view_mode])
    result["view"] = view
    return result


def _decision_metrics(store, archive_entity, view, maps):
    """Compare three-hour choices on identical issue/day grids and the previous local week as known at issue."""
    from collections import defaultdict
    from .backtest import QUARTER
    methods = view["available"]
    low, high = (0, 168) if view["horizon"] == "all" else HORIZONS[view["horizon"]]
    zone = ZoneInfo("Europe/Amsterdam")
    shared = set.intersection(*(set(maps[m]) for m in methods))
    groups = defaultdict(list)
    stamps = set()
    for issue, target in shared:
        stamp = _dt(target)
        if low <= (stamp - _dt(issue)).total_seconds() / 3600 < high:
            groups[issue].append((target, stamp))
            stamps.add(target)
    view.update(issue_days=len(view["daily"]), unique_targets=len(stamps))
    regrets = {m: [] for m in methods}
    weekly_errors = []
    paired_errors = {m: [] for m in methods}
    for issue, targets in groups.items():
        targets.sort()
        week = timedelta(days=7)
        # DST may shift the corresponding UTC quarter by one hour.
        history = store.quarters(archive_entity, targets[0][1]-week-timedelta(hours=2),
                                 targets[-1][1]-week+timedelta(hours=2), as_of=_dt(issue))
        known = {r["start_utc"]: r["price"] for r in history if r["quality"] in {"published", "valid"}}
        days = defaultdict(list)
        for target, stamp in targets:
            local = stamp.astimezone(zone)
            earlier = local - week
            actual = maps[methods[0]][issue, target][1]
            ref = known.get(utc_iso(earlier))
            if ref is not None and earlier.astimezone(UTC).astimezone(zone).hour == local.hour:
                weekly_errors.append(abs(actual-ref))
                for m in methods:
                    paired_errors[m].append(abs(maps[m][issue, target][0]-actual))
            days[local.date()].append((stamp, [actual, *(maps[m][issue,target][0] for m in methods)]))
        for rows in days.values():
            # Rolling totals reset at gaps. Earlier windows win ties.
            windows = []
            totals = [0.0] * (1+len(methods))
            begin = 0
            for i, (stamp, values) in enumerate(rows):
                if i and stamp - rows[i-1][0] != QUARTER:
                    begin = i
                    totals = [0.0] * len(values)
                for j, value in enumerate(values):
                    totals[j] += value
                    if i-begin >= 12:
                        totals[j] -= rows[i-12][1][j]
                if i-begin >= 11:
                    windows.append(tuple(totals))
            if len(windows) >= 2:
                optimum = min(w[0] for w in windows)
                for j, m in enumerate(methods, 1):
                    chosen = min(windows, key=lambda w: round(w[j], 12))
                    regrets[m].append((chosen[0]-optimum)/12)
    reference = sum(weekly_errors)
    for metric in view["metrics"]:
        m = metric["id"]
        metric.update(weekly_pairs=len(weekly_errors),
                      weekly_skill=1-sum(paired_errors[m])/reference if reference else None,
                      window_n=len(regrets[m]), window_regret=mean(regrets[m]) if regrets[m] else None)


def _comparison_label(method, mode):
    score = method[mode]
    version = score["version"] or ""
    label = method["label"]
    if mode == "historical":
        if method["id"] == "timestral" and version.startswith("timestral-local-"):
            size = version.removeprefix("timestral-local-").split("-", 1)[0].upper()
            calendar = " · weekendinfo" if "-calendar-1:" in version else " · zonder kalender"
            weather = " · met weer" if ":weather-1-" in version else " · zonder weer"
            label = f"Timestral-{size}{calendar}{weather}"
        if score.get("weather_reconstruction"):
            label += " · archiefweer"
    return label


def comparison_view(result, maps, mode, horizon, selected, days):
    """Charts only compare identical issue/target pairs, never latest guesses."""
    methods = {m["id"]: m for m in result["methods"]}
    selected = list(dict.fromkeys(selected if selected is not None else [m for m, row in methods.items() if row["enabled"]]))
    limits = {"all": (0,168), **HORIZONS}
    low, high = limits[horizon]
    rows = {m: {key: value for key, value in maps[mode][m].items()
                if low <= (_dt(key[1])-_dt(key[0])).total_seconds()/3600 < high} for m in selected}
    available = [m for m in selected if rows[m]]
    shared = set.intersection(*(set(rows[m]) for m in available)) if available else set()
    metrics = [{"id": m, "label": _comparison_label(methods[m], mode), "version": methods[m][mode]["version"],
                **_scores(rows[m][key] for key in shared)} for m in available]
    daily = {}
    latest = {}
    for key in sorted(shared):
        issued, start = key
        day = _dt(issued).astimezone(ZoneInfo("Europe/Amsterdam")).date().isoformat()
        daily.setdefault(day, {m: [] for m in available})
        for m in available: daily[day][m].append(rows[m][key])
        if start not in latest or issued > latest[start][0]: latest[start] = key
    points = [{"start": start, "issued_at": latest[start][0], "actual": rows[available[0]][latest[start]][1],
               "predictions": {m: rows[m][latest[start]][0] for m in available}}
              for start in sorted(latest)[-192:]] if available else []
    return {"mode": mode, "horizon": horizon, "days": days, "selected": selected,
            "missing": [m for m in selected if m not in available], "available": available,
            "paired": len(available) >= 2 and bool(shared), "n": len(shared), "metrics": metrics, "points": points,
            "daily": [{"date": day, "n": len(next(iter(values.values()))),
                       "mae": {m: _scores(values[m])["mae"] for m in available}} for day, values in sorted(daily.items())],
            "notice": "Dezelfde uitgiftetijd en doelkwartieren per methode; bij meerdere uitgiftes wordt in de prijsgrafiek de nieuwste gezamenlijke gekozen. Overlappende doelen zijn geen onafhankelijke metingen."}


def start_history_test(service, horizon_hours=48, weather_mode="stored"):
    """Single bounded job; up to seven daily issues, 48h or 168h each."""
    if type(horizon_hours) is not int or horizon_hours not in {48,168}:
        raise ValueError("Kies 48 of 168 uur voor de historische test")
    if weather_mode not in {"stored","archive"}: raise ValueError("Kies opgeslagen verwachtingen of weerarchief")
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
        required_days = 14 + horizon_hours//24
        if not rows or _dt(rows[-1]["end_utc"]) - _dt(rows[0]["start_utc"]) < timedelta(days=required_days):
            raise ValueError(f"Minimaal {required_days} dagen echte kwartierhistorie nodig voor {horizon_hours} uur; vernieuw eerst de prijsbron of kies 48 uur")
        service._history_job = {"state": "running", "completed": 0, "total": 0, "errors": [], "skipped": [], "methods": methods, "horizon_hours": horizon_hours, "weather_mode":weather_mode}
        service._history_thread = threading.Thread(target=_history_worker,
            args=(service, settings, entity, rows, now, methods, horizon_hours, weather_mode), daemon=True, name="forecast-history")
        service._history_thread.start()
        return dict(service._history_job)


def _history_worker(service, settings, entity, rows, now, methods, horizon_hours, weather_mode):
    job = service._history_job
    try:
        first = _dt(rows[0]["start_utc"]) + timedelta(days=14)
        last = min(now, _dt(rows[-1]["end_utc"])) - timedelta(hours=horizon_hours)
        last = max(first, last.replace(hour=0, minute=0, second=0, microsecond=0))
        issues = [last - timedelta(days=d) for d in reversed(range(7)) if last - timedelta(days=d) >= first]
        job["total"] = len(issues) * len(methods)
        scale = 1000 if settings["tariff_unit"] == "EUR/kWh" else 1
        needs_archive=weather_mode=="archive" and settings["weather_source"]=="open_meteo" and (
            "timestral" in methods and settings["timestral_weather"])
        weather_context=None
        if needs_archive and issues and "timestral" in methods and settings["timestral_weather"]:
            from .weather import fetch_weather_context
            try:
                weather_context=fetch_weather_context(settings["weather_region"],settings["latitude"],settings["longitude"],
                    issues[0]-timedelta(days=min(35,settings["timestral_context_days"])),issues[-1]-timedelta(hours=12),
                    now,service.store.data_dir,service._stop)
            except Exception as exc:
                job["errors"].append({"method":"historisch weer","message":str(exc)})
        for issue in issues:
            current=service.get_settings()
            if service._stop.is_set() or current["weather_region"]!=settings["weather_region"] or current["weather_source"]!=settings["weather_source"] or not any(current["forecast_methods"].get(m) for m in methods):
                job["state"]="cancelled"
                return
            # Imported prices were first observed today: deliberately labelled
            # reconstruction uses ended intervals, never future target prices.
            history = [{**r, "published_at": r["end_utc"]} for r in rows
                       if issue - timedelta(days=90) <= _dt(r["start_utc"]) and _dt(r["end_utc"]) <= issue]
            archived_weather=service.store.latest_snapshot("weather",successful_only=True,as_of=issue)
            archived_weather=archived_weather["payload"] if archived_weather else None
            if archived_weather:
                observed=_dt(archived_weather["observed_at"])
                if not timedelta(0)<=issue-observed<=timedelta(hours=6) or archived_weather.get("profile")!=settings.get("weather_region"):
                    archived_weather=None
                elif archived_weather.get("profile")=="local" and archived_weather.get("location")!={"latitude":settings["latitude"],"longitude":settings["longitude"]}:
                    archived_weather=None
            weather_error=None
            reconstructed=False
            if not archived_weather and needs_archive:
                from .weather import fetch_archived_weather
                try:
                    archived_weather=fetch_archived_weather(settings["weather_region"],settings["latitude"],settings["longitude"],
                        issue,horizon_hours,now,service.store.data_dir,weather_context,service._stop)
                    reconstructed=True
                except Exception as exc:
                    weather_error=str(exc)
                    job["errors"].append({"method":"historisch weer","issued_at":utc_iso(issue),"message":weather_error})
            from .weather import model_weather
            historical_weather=model_weather(archived_weather,start=issue) if archived_weather else None
            for method in methods:
                current = service.get_settings()
                if (service._stop.is_set() or service._price_identity(current) != entity
                        or current["price_field"] != settings["price_field"] or current["tariff_unit"] != settings["tariff_unit"]):
                    job["state"] = "cancelled"
                    return
                if not current["forecast_methods"].get(method):
                    job["completed"] += 1
                    continue
                if method=="timestral" and settings["timestral_weather"] and not archived_weather:
                    job["skipped"].append({"method":method,"issued_at":utc_iso(issue),
                        "message":weather_error or "Voor deze historische uitgifte is geen destijds opgeslagen Open-Meteo-verwachting voor dit weergebied beschikbaar. Kies historisch weer ophalen om een reconstructie te testen; de huidige broninstelling kan correct zijn."})
                    job["completed"]+=1
                    continue
                try:
                    if method == "epex":
                        run = epex_forecast(history, issue, {**service._runtime_settings(settings, method), "_priority": "history"}, max_points=horizon_hours*4)
                    else:
                        run = timestral_forecast(history, issue, {**service._runtime_settings(settings, method), "_priority": "history", "_weather":archived_weather, "_historical_weather_reconstruction":reconstructed}, max_points=horizon_hours*4)
                    uses_reconstruction = reconstructed and method == "timestral" and settings["timestral_weather"]
                    if uses_reconstruction: run=replace(run,model_version=run.model_version+WEATHER_ARCHIVE_VERSION,
                        reasons=[*run.reasons,"Gereconstrueerd GFS-archiefweer; geselecteerde run minstens 12 uur vóór deze uitgifte. Kan afwijken van oorspronkelijke live bron/modelbeschikbaarheid."])
                    inputs = {"method": method, "evaluation_mode": "historical", "tariff_entity": entity,
                              "tariff_unit": settings["tariff_unit"], "price_field": settings["price_field"],
                              "history": history, "weather_model": historical_weather, "weather_snapshot":archived_weather, "price_scale": scale, "test_horizon_hours": horizon_hours, "weather_region": settings["weather_region"], "weather_reconstruction":uses_reconstruction}
                    identity = json.dumps([inputs, run.model_version, utc_iso(issue)], sort_keys=True, default=str)
                    run_id = "historical-" + hashlib.sha256(identity.encode()).hexdigest()
                    inputs["reconstructed_at"] = utc_iso(now)
                    service.store.save_forecast(run_id, issue, run.model_version, "historische reconstructie",
                        [*run.reasons, "Huidig prijsarchief; oorspronkelijke prijspublicatievintages kunnen ontbreken"], inputs,
                        [asdict(point) for point in run.points])
                except Exception as exc:
                    job["errors"] = [*job["errors"], {"method": method, "issued_at": utc_iso(issue), "message": str(exc)}]
                job["completed"] += 1
        job["state"] = "completed_with_errors" if job["errors"] else "completed_with_skips" if job["skipped"] else "completed"
    except Exception as exc:
        job["state"] = "failed"
        job["errors"] = [*job["errors"], {"message": str(exc)}]
    finally:
        service.store.save_snapshot(now, "history_test", dict(job))
