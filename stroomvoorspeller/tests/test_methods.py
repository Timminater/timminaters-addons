from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import math
import threading
from urllib.request import Request, urlopen

import pytest

from app.backend import AppService, make_handler
from app.comparison import comparison, start_history_test
from app.methods import adaptive_forecast, epex_forecast, timestral_forecast, TIMESTRAL_REVISION
from app.model import ForecastPoint, ForecastRun, forecast_quarters
from app.storage import MethodStore, Store, utc_iso

UTC = timezone.utc
STEP = timedelta(minutes=15)
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def history(days=28, end=NOW):
    rows = []
    for i in range(days * 96):
        start = end - timedelta(days=days) + i * STEP
        # Repeating intraday shape and a recent level shift.
        value = .12 + .04 * math.sin(start.hour / 24 * 2 * math.pi) + (.06 if start >= end - timedelta(days=3) else 0)
        rows.append({"start_utc": utc_iso(start), "end_utc": utc_iso(start + STEP),
                     "published_at": utc_iso(start + STEP), "observed_at": utc_iso(end),
                     "price": value, "unit": "EUR/kWh", "source": "ha_history", "quality": "valid"})
    return rows


def service(tmp_path):
    app = AppService(store=Store(tmp_path), clock=lambda: NOW)
    app.put_settings({"tariff_entity": "sensor.prices", "tariff_unit": "EUR/kWh"})
    return app


def test_adaptive_uses_only_past_prices_and_preserves_units():
    rows = history()
    run = adaptive_forecast(rows, NOW, max_points=8, price_scale=1000, base=forecast_quarters)
    assert len(run.points) == 8
    future = {**rows[-1], "start_utc": utc_iso(NOW + timedelta(days=2)),
              "end_utc": utc_iso(NOW + timedelta(days=2) + STEP),
              "published_at": utc_iso(NOW + STEP), "price": 9999}
    mutated = adaptive_forecast([*rows, future], NOW, max_points=8, price_scale=1000, base=forecast_quarters)
    assert run == mutated
    mwh = [{**r, "price": r["price"] * 1000} for r in rows]
    scaled = adaptive_forecast(mwh, NOW, max_points=8, base=forecast_quarters)
    assert [p.price for p in run.points] == pytest.approx([p.price / 1000 for p in scaled.points])
    assert all(p.source == "local-adaptive" for p in run.points)


def test_adaptive_reduces_error_on_synthetic_recent_level_shift():
    rows = history()
    original = forecast_quarters(rows, NOW, max_points=96, price_scale=1000)
    adapted = adaptive_forecast(rows, NOW, max_points=96, price_scale=1000, base=forecast_quarters)
    def error(run):
        return sum(abs(p.price - (.18 + .04 * math.sin(p.start_utc.hour / 24 * 2 * math.pi))) for p in run.points) / len(run.points)
    assert error(adapted) < error(original)


def test_epex_excludes_published_prices_and_converts_explicit_tariff():
    payload = {"knownUntil": utc_iso(NOW + STEP), "prices": [
        {"startsAt": utc_iso(NOW + i * STEP), "total": 100} for i in range(4)]}
    settings = {"epex_tariff_confirmed": True, "epex_multiplier": 1.21,
                "epex_offset_eur_kwh": .1, "tariff_unit": "EUR/kWh"}
    calls = []
    def fetch(url):
        calls.append(url)
        return payload
    run = epex_forecast([], NOW, settings, fetch=fetch)
    assert len(run.points) == 2
    assert run.points[0].start_utc == NOW + 2 * STEP
    assert run.points[0].price == pytest.approx(.221)
    assert "region=NL" in calls[0] and "EUR_PER_MWH" in calls[0]
    # Unconfirmed conversion must fail before any request.
    settings["epex_tariff_confirmed"] = False
    with pytest.raises(ValueError, match="Bevestig"):
        epex_forecast([], NOW, settings, fetch=lambda *_: pytest.fail("unexpected network"))


def test_epex_rejects_duplicate_and_nonfinite_values():
    row = {"startsAt": utc_iso(NOW + STEP), "total": 100}
    settings = {"price_source": "energy_charts_nl", "tariff_unit": "EUR/kWh"}
    with pytest.raises(ValueError, match="Dubbele"):
        epex_forecast([], NOW, settings, fetch=lambda _: {"knownUntil": utc_iso(NOW), "prices": [row, row]})
    with pytest.raises(ValueError, match="niet-eindige"):
        epex_forecast([], NOW, settings, fetch=lambda _: {"knownUntil": utc_iso(NOW), "prices": [{**row, "total": float('nan')}]})


def test_timestral_regular_grid_preserves_gaps_and_rejects_bad_response():
    rows = history(3)
    del rows[30]
    sent = []
    def fetch(url, body, **kwargs):
        sent.append(body)
        return {"model": "mistralai/Timestral-209M", "revision": TIMESTRAL_REVISION, "median": [-.02] * 4,
                "lower": [-.05] * 4, "upper": [.01] * 4}
    run = timestral_forecast(rows, NOW, {"timestral_url": "http://worker", "timestral_context_days": 3}, 4, fetch=fetch)
    assert sent[0]["context"][30] is None
    assert len(sent[0]["context"]) == 288
    assert run.points[0].price == -.02  # Negative electricity prices are valid.
    assert run.points[0].start_utc == NOW
    with pytest.raises(ValueError, match="lengte"):
        timestral_forecast(rows, NOW, {"timestral_url": "http://worker"}, 4,
            fetch=lambda *a, **k: {"model": "mistralai/Timestral-209M", "revision": TIMESTRAL_REVISION, "median": []})


def save(store, run_id, method, mode="live", issue=NOW, version="v1"):
    store.save_forecast(run_id, issue, version, "voorlopig", [], {
        "tariff_entity": "sensor.prices", "tariff_unit": "EUR/kWh", "price_field": "tax_included",
        "method": method, "evaluation_mode": mode},
        [{"start_utc": issue + STEP, "end_utc": issue + 2 * STEP, "price": .2}])


def test_store_separates_methods_modes_and_versions(tmp_path):
    store = Store(tmp_path)
    save(store, "local", "local")
    save(store, "remote", "epex", issue=NOW + STEP)
    save(store, "replay", "local", "historical", issue=NOW + 2 * STEP)
    assert store.latest_forecast(method="local")[0]["id"] == "local"
    assert store.latest_forecast(method="local", evaluation_mode="historical")[0]["id"] == "replay"
    daily = MethodStore(store, "local", "v1").daily_stored_forecasts(
        "sensor.prices", NOW - STEP, NOW + timedelta(days=1), "tax_included", "EUR/kWh")
    assert [row["id"] for row in daily] == ["local"]
    assert not MethodStore(store, "local", "other-version").daily_stored_forecasts(
        "sensor.prices", NOW - STEP, NOW + timedelta(days=1), "tax_included", "EUR/kWh")


def test_settings_require_enabled_active_method_allow_all_off(tmp_path):
    app = service(tmp_path)
    with pytest.raises(ValueError, match="actieve"):
        app.put_settings({"forecast_methods": {"local": False, "v4": False, "epex": True, "timestral": False}})
    app.put_settings({"forecast_methods": dict.fromkeys(["local", "v4", "epex", "timestral"], False)})
    assert app._latest_forecast() == (None, [])
    with pytest.raises(ValueError):
        app.put_settings({"timestral_context_days": 1000})


def test_disabled_methods_never_called_and_failure_isolated(tmp_path, monkeypatch):
    app = service(tmp_path)
    monkeypatch.setattr('app.methods.epex_forecast', lambda *a, **k: pytest.fail("disabled network"))
    monkeypatch.setattr('app.methods.timestral_forecast', lambda *a, **k: pytest.fail("disabled inference"))
    run = ForecastRun("stub", NOW, [], "voorlopig", [])
    monkeypatch.setattr('app.methods.adaptive_forecast', lambda *a, **k: run)
    result = app._run_methods(history(2), NOW, None, None, app.get_settings(), "sensor.prices", False, [])
    assert result["ok"] and set(result["methods"]) == {"local"}
    app.put_settings({"forecast_methods": {"local": True, "v4": False, "epex": True, "timestral": False}})
    def fail(*a, **k): raise ValueError("offline")
    monkeypatch.setattr('app.methods.epex_forecast', fail)
    result = app._run_methods(history(2), NOW, None, None, app.get_settings(), "sensor.prices", True, [])
    assert result["ok"] and not result["methods"]["epex"]["ok"]
    assert result["methods"]["epex"]["message"] == "offline"
    assert app.store.latest_forecast(method="local")[0]


def test_historical_job_runs_existing_data_without_polluting_live(tmp_path):
    app = service(tmp_path)
    app.put_settings({"forecast_methods": {"local": False, "v4": True, "epex": True, "timestral": False},
                      "active_method": "v4"})
    archive = app._archive_entity("sensor.prices", app.get_settings())
    app.store.upsert_quarters([{**r, "entity_id": archive} for r in history(18)])
    job = start_history_test(app)
    app._history_thread.join(timeout=30)
    assert not app._history_thread.is_alive()
    assert app._history_job["state"] == "completed"
    assert app._history_job["completed"] <= 7
    assert app.store.latest_forecast()[0] is None
    report = comparison(app)
    v4 = next(m for m in report["methods"] if m["id"] == "v4")
    assert v4["historical"]["n"] > 0 and v4["live"]["n"] == 0
    assert not any(k.startswith("method:epex") for k in app.store.source_statuses())
    initial = app.store.forecast_snapshots(evaluation_mode="historical")
    start_history_test(app)
    app._history_thread.join(timeout=30)
    assert len(initial) == len(app.store.forecast_snapshots(evaluation_mode="historical"))


def test_paired_scores_only_match_same_issue_and_target(tmp_path):
    app = service(tmp_path)
    app.put_settings({"forecast_methods": {"local": True, "v4": True, "epex": False, "timestral": False}})
    issue = NOW - timedelta(days=1)
    for method in ("local", "v4"):
        save(app.store, method, method, issue=issue)
    archive = app._archive_entity("sensor.prices", app.get_settings())
    app.store.upsert_quarters([{"entity_id": archive, "start_utc": issue + STEP,
        "end_utc": issue + 2 * STEP, "price": .25, "unit": "EUR/kWh", "source": "market",
        "observed_at": NOW, "published_at": NOW, "quality": "published"}])
    report = comparison(app)
    for method in report["methods"][:2]:
        assert method["live"]["paired"]["n"] == 1
        assert method["live"]["paired"]["mae"] == pytest.approx(.05)
    save(app.store, "v4later", "v4", issue=issue + STEP)
    report = comparison(app)
    # Nearest-noon representative changes; different issue must not pair.
    assert report["methods"][0]["live"]["paired"]["n"] == 0


def test_method_http_endpoints(tmp_path):
    app = service(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/methods") as response:
            report = json.load(response)
        assert len(report["methods"]) == 4
        assert report["active_method"] == "local"
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_already_published_market_targets_are_not_stored_as_predictions(tmp_path, monkeypatch):
    app = service(tmp_path)
    archive = app._archive_entity("sensor.prices", app.get_settings())
    app.store.upsert_quarters([{"entity_id": archive, "start_utc": NOW + STEP,
        "end_utc": NOW + 2 * STEP, "price": .25, "unit": "EUR/kWh", "source": "market",
        "observed_at": NOW, "published_at": NOW, "quality": "published"}])
    run = ForecastRun("synthetic", NOW, [ForecastPoint(NOW + STEP, NOW + 2 * STEP, .1),
                      ForecastPoint(NOW + 2 * STEP, NOW + 3 * STEP, .2)], "voorlopig", [])
    monkeypatch.setattr('app.methods.adaptive_forecast', lambda *a, **k: run)
    app._run_methods(history(2), NOW, None, None, app.get_settings(), "sensor.prices", True, [])
    _, points = app.store.latest_forecast(method="local")
    assert [p['start_utc'] for p in points] == [utc_iso(NOW + 2 * STEP)]


def test_changing_external_config_starts_new_score_series(tmp_path):
    from app.methods import configured_version
    app = service(tmp_path)
    settings = app.get_settings()
    original = configured_version("epex", settings)
    assert original != configured_version("epex", {**settings, "epex_offset_eur_kwh": .123})
    original = configured_version("timestral", settings)
    assert original != configured_version("timestral", {**settings, "timestral_context_days": 14})


def test_remote_failed_attempt_is_throttled_until_interval(tmp_path, monkeypatch):
    app = service(tmp_path)
    app.put_settings({"forecast_methods": {"local": False, "v4": False, "epex": True, "timestral": False},
                      "active_method": "epex"})
    attempts = []
    def offline(*args, **kwargs):
        attempts.append(1)
        raise ValueError("offline")
    monkeypatch.setattr('app.methods.epex_forecast', offline)
    settings = app.get_settings()
    app._run_methods(history(2), NOW, None, None, settings, "sensor.prices", False, [])
    result = app._run_methods(history(2), NOW + timedelta(minutes=5), None, None, settings, "sensor.prices", False, [])
    assert result['throttled'] and len(attempts) == 1
    app._run_methods(history(2), NOW + timedelta(minutes=6), None, None, settings, "sensor.prices", True, [])
    assert len(attempts) == 2


def test_worker_rejects_invalid_payload_without_importing_model():
    from timestral_service.server import validate
    with pytest.raises(ValueError):
        validate({'context': [None] * 192, 'prediction_length': 96})
    with pytest.raises(ValueError):
        validate({'context': [.1] * 192, 'prediction_length': 1000})
    with pytest.raises(ValueError):
        validate({'context': [float('nan')] * 192, 'prediction_length': 96})
    context, horizon = validate({'context': [-.1] * 192, 'prediction_length': 96})
    assert len(context) == 192 and horizon == 96


def test_epex_mapping_change_does_not_reuse_old_calibration(tmp_path):
    from app.methods import epex_version
    app = service(tmp_path)
    app.put_settings({'forecast_methods': {'local': False, 'v4': False, 'epex': True, 'timestral': False},
                      'active_method': 'epex'})
    settings = app.get_settings()
    save(app.store, 'old-config', 'epex', version=epex_version(settings))
    app.put_settings({'epex_offset_eur_kwh': .1})
    assert app._latest_forecast('sensor.prices', 'tax_included', 'EUR/kWh') == (None, [])
    assert app._method_store().daily_stored_forecasts('sensor.prices', NOW - STEP,
                NOW + timedelta(days=1), 'tax_included', 'EUR/kWh') == []
