"""Collect and score a shadow Timestral run against its exact live baseline."""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
import math

from . import ned
from .comparison import HORIZONS, _scores
from .ha_client import parse_dt
from .methods import timestral_forecast, timestral_version
from .storage import utc_iso

MODE = "timestral_ned_ab"
STATUS = "experiment:timestral_ned"
NED_REFRESH = timedelta(hours=3)
PAIR_INTERVAL = timedelta(hours=6)
FAILURE_RETRY = timedelta(minutes=30)


def _age(now, status, field):
    return now - parse_dt(status[field]) if status.get(field) else timedelta.max


def _enabled(settings):
    return (settings["timestral_ned_ab"] and settings["timestral_weather"]
            and settings["forecast_methods"]["timestral"])


def _run_candidate(service, settings, now, statuses):
    status = statuses.get(STATUS, {})
    if (_age(now, status, "last_success") < PAIR_INTERVAL
            or _age(now, status, "last_attempt") < FAILURE_RETRY):
        return
    if not service._lock.acquire(blocking=False):
        return
    try:
        if service._stop.is_set() or service._wake.is_set() or service.get_settings() != settings:
            return
        entity = service._price_identity(settings)
        if not entity:
            return
        baseline, points = service.store.latest_forecast(
            entity, settings["price_field"], settings["tariff_unit"], method="timestral",
            model_version=timestral_version(settings), since=now - timedelta(minutes=10),
        )
        if not baseline or not points:
            return
        issued = parse_dt(baseline["issued_at"])
        if issued > now:
            return
        snapshot = service.store.latest_snapshot(ned.SOURCE, successful_only=True, as_of=issued)
        if not snapshot or issued - parse_dt(snapshot["issued_at"]) > ned.MAX_AGE:
            return
        inputs = baseline["inputs"]
        runtime = {**service._runtime_settings(settings, "timestral"), "_priority": "shadow"}
        cancel = runtime["_cancel"]
        runtime["_cancel"] = lambda: cancel() or service._wake.is_set()
        run = timestral_forecast(inputs["history"], issued, {
            **runtime, "_weather": inputs.get("weather_snapshot"), "_ned_snapshot": snapshot["payload"],
        })
        if runtime["_cancel"]():
            return
        baseline_targets = {point["start_utc"]: point["end_utc"] for point in points}
        matched = [
            {"start_utc": point.start_utc, "end_utc": point.end_utc, "price": point.price,
             "lower": point.lower, "upper": point.upper, "source": point.source, "reason": point.reason}
            for point in run.points if baseline_targets.get(utc_iso(point.start_utc)) == utc_iso(point.end_utc)
        ]
        if not matched:
            raise ValueError("Geen gezamenlijke doelkwartieren met NED-restvraag")
        service.store.save_forecast("ned-ab-" + baseline["id"], issued, run.model_version, run.quality,
                                    run.reasons, {
            "tariff_entity": entity, "price_field": settings["price_field"], "tariff_unit": settings["tariff_unit"],
            "method": "timestral", "evaluation_mode": MODE, "baseline_run_id": baseline["id"],
            "ned_snapshot_id": snapshot["id"],
        }, matched)
        service.store.set_source_status(STATUS, success=True, attempted_at=now,
                                        detail={"issue": baseline["issued_at"], "points": len(matched)})
    except (ValueError, KeyError) as exc:
        service.store.set_source_status(STATUS, success=False, attempted_at=now, error=str(exc))
    finally:
        service._lock.release()


def collect(service, *, archive_only=False, candidate_only=False):
    settings = service.get_settings()
    if (not archive_only and not _enabled(settings)) or service._stop.is_set() or service._wake.is_set():
        return
    key = ned.api_key(service.store.data_dir)
    if not key:
        return
    now = service.clock()
    statuses = service.store.source_statuses()
    if not archive_only:
        _run_candidate(service, settings, now, statuses)
    if candidate_only:
        return
    status = statuses.get(ned.SOURCE, {})
    if (_age(now, status, "last_success") < NED_REFRESH or _age(now, status, "last_attempt") < FAILURE_RETRY
            or service._stop.is_set() or service._wake.is_set()):
        return
    try:
        snapshot = ned.fetch_snapshot(key, service.clock)
        service.store.save_snapshot(parse_dt(snapshot["observed_at"]), ned.SOURCE, snapshot)
        service.store.set_source_status(ned.SOURCE, success=True, attempted_at=now,
                                        detail={"hours": len(snapshot["hourly"])})
    except ValueError as exc:
        service.store.set_source_status(ned.SOURCE, success=False, attempted_at=now, error=str(exc))


def report(service):
    settings = service.get_settings()
    now = service.clock()
    since = utc_iso(now - timedelta(days=45))
    archive = service._archive_entity(service._price_identity(settings), settings)
    version = timestral_version(settings)
    statuses = service.store.source_statuses()
    result = {"enabled": _enabled(settings), "configured": settings["timestral_ned_ab"],
              "key_configured": bool(ned.api_key(service.store.data_dir)),
              "unit": settings["tariff_unit"], "baseline_version": version, "issues": 0,
              "targets": 0, "last_issue": None, "scores": [],
              "source": statuses.get(ned.SOURCE, {}), "experiment": statuses.get(STATUS, {})}
    with closing(service.store._connect()) as db:
        candidates = db.execute("""
            SELECT b.id AS baseline, r.id AS candidate, r.issued_at
              FROM forecast_runs r JOIN forecast_runs b ON b.id=json_extract(r.inputs_json,'$.baseline_run_id')
             WHERE r.model_version=? AND r.issued_at>=? AND r.issued_at<=? AND b.issued_at=r.issued_at
               AND b.model_version=?
               AND json_extract(r.inputs_json,'$.evaluation_mode')=?
               AND json_extract(r.inputs_json,'$.tariff_entity')=?
               AND json_extract(r.inputs_json,'$.price_field')=? AND json_extract(r.inputs_json,'$.tariff_unit')=?
             ORDER BY r.issued_at
        """, (version + ned.VERSION_SUFFIX, since, utc_iso(now), version, MODE,
              service._price_identity(settings), settings["price_field"], settings["tariff_unit"])).fetchall()
        if not candidates:
            return result
        buckets = {label: [] for label in ("all", *HORIZONS)}
        targets = set()
        scored_issues = set()
        for run in candidates:
            issue = parse_dt(run["issued_at"])
            rows = db.execute("""
                SELECT a.start_utc,a.price,b.price,q.price
                  FROM forecast_points a JOIN forecast_points b
                    ON b.run_id=? AND a.start_utc=b.start_utc AND a.end_utc=b.end_utc
                  JOIN quarter_prices q ON q.entity_id=? AND q.start_utc=a.start_utc AND q.end_utc=a.end_utc
                 WHERE a.run_id=? AND q.quality IN ('published','valid') AND q.observed_at<=?
                   AND (q.published_at IS NULL OR q.published_at<=?)
                   AND (q.observed_at>? OR q.published_at>?)
                   AND NOT EXISTS (
                     SELECT 1 FROM quarter_price_revisions rev
                      WHERE rev.entity_id=q.entity_id AND rev.start_utc=q.start_utc
                        AND rev.quality IN ('published','valid') AND rev.observed_at<=?
                        AND (rev.published_at IS NULL OR rev.published_at<=?)
                   )
            """, (run["candidate"], archive, run["baseline"], utc_iso(now), utc_iso(now),
                  run["issued_at"], run["issued_at"], run["issued_at"], run["issued_at"]))
            for target, baseline, candidate, actual in rows:
                lead = (parse_dt(target) - issue).total_seconds() / 3600
                if not 0 <= lead < 168 or not all(v is not None and math.isfinite(v)
                                                  for v in (baseline, candidate, actual)):
                    continue
                targets.add(target)
                scored_issues.add(run["issued_at"])
                score = (baseline, candidate, actual)
                buckets["all"].append(score)
                for horizon, (low, high) in HORIZONS.items():
                    if low <= lead < high:
                        buckets[horizon].append(score)
    result.update(issues=len(scored_issues), targets=len(targets), last_issue=candidates[-1]["issued_at"],
                  scores=[{"horizon": horizon, "baseline": _scores((a, truth) for a, b, truth in rows),
                           "candidate": _scores((b, truth) for a, b, truth in rows)}
                          for horizon, rows in buckets.items()])
    return result
