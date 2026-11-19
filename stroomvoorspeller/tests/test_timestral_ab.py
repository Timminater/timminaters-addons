from datetime import datetime, timedelta, timezone

import pytest

from app import ned, timestral_ab
from app.backend import AppService
from app.local_worker import validate_timestral
from app.methods import timestral_forecast, timestral_version
from app.storage import Store, utc_iso
from app.timestral_models import model_spec

NOW = datetime(2026, 11, 19, 13, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)


def test_modal_key_replaces_existing_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("NED_API_KEY", raising=False)
    (tmp_path / "options.json").write_text('{"ned_api_key":"existing"}')
    assert ned.api_key(tmp_path) == "existing"
    ned.migrate_key(tmp_path)
    (tmp_path / "options.json").write_text("{}")
    assert ned.api_key(tmp_path) == "existing"
    app = AppService(store=Store(tmp_path), clock=lambda: NOW)
    saved = app.put_settings({"price_source": "energy_charts_nl", "ned_data_enabled": True,
                              "ned_api_key": " new-key "})
    assert saved["ned_data_enabled"] and "new-key" not in str(saved)
    assert ned.api_key(tmp_path) == "new-key"
    app.put_settings({"ned_data_enabled": False})
    app._wake.clear()
    monkeypatch.setattr(ned, "fetch_snapshot", lambda *args: pytest.fail("NED-dataverzameling staat uit"))
    timestral_ab.collect(app, archive_only=True)
    assert ned.api_key(tmp_path) == "new-key"
    with pytest.raises(ValueError):
        ned.save_key(tmp_path, "bad\r\nkey")
    assert ned.api_key(tmp_path) == "new-key"


def test_ned_residual_load_reads_same_utc_hour_and_ignores_later_versions():
    calls = []
    capacity = {59: 16000000, 2: 2000000, 1: 3000000, 51: 4000000}

    def request(params, key, timeout):
        calls.append(params)
        assert key == "test-key" and 0 < timeout <= 5
        assert (params["granularity"], params["granularitytimezone"], params["point"]) == (5, 0, 0)
        stamp = NOW - ned.HOUR if params["classification"] == 2 else NOW
        row = {"validfrom": utc_iso(stamp), "validto": utc_iso(stamp + ned.HOUR),
               "lastupdate": utc_iso(NOW - STEP), "capacity": capacity[params["type"]]}
        if params["page"] == 1:
            return {"hydra:member": [row], "hydra:view": {"hydra:next": "?page=2"}}
        # A mid-download update may never leak into the saved vintage.
        return {"hydra:member": [{**row, "lastupdate": utc_iso(NOW + STEP), "capacity": 1},
                                 {**row, "lastupdate": "invalid"}]}

    data = ned.fetch_snapshot("test-key", lambda: NOW, request=request)
    assert len(calls) == 16
    assert {(c["type"], c["activity"]) for c in calls} == {(59, 2), (2, 1), (1, 1), (51, 1)}
    assert data["hourly"][utc_iso(NOW)][0] == 7000
    assert data["forecast_from"] == utc_iso(NOW)
    assert "test-key" not in str(data)


def case_data():
    start = NOW - timedelta(days=2)
    history = [{"start_utc": utc_iso(start + i * STEP), "end_utc": utc_iso(start + (i + 1) * STEP),
                "price": .25} for i in range(192)]
    stamps = [utc_iso(start + timedelta(hours=i)) for i in range(48 + 168)]
    weather = {"source": "open_meteo", "profile": "nl", "observed_at": utc_iso(NOW - STEP),
               "hourly": {s: {"solar_wm2": 40, "wind_ms": 4, "temp_c": 8} for s in stamps}}
    observed = utc_iso(NOW - STEP)
    snapshot = {"observed_at": observed, "forecast_from": utc_iso(NOW - ned.HOUR),
                "hourly": {s: [7000, observed, observed, observed, observed] for s in stamps}}
    return history, weather, snapshot


def test_adapter_changes_only_the_residual_load_input():
    history, weather, snapshot = case_data()
    settings = {"_weather": weather, "timestral_weather": True, "timestral_calendar": True,
                "timestral_model": "123M", "timestral_context_days": 2}
    requests = []
    spec = model_spec("123M")

    def runner(method, request, settings, **kwargs):
        requests.append(dict(request))
        context, horizon = validate_timestral(request)
        assert len(context) == 192 and horizon == 4
        return {"model": spec["repo"], "revision": spec["revision"], "calendar": True,
                "weather": True, "residual_load": "residual_load" in request,
                "median": [.2] * horizon, "lower": [.1] * horizon, "upper": [.3] * horizon}

    a = timestral_forecast(history, NOW, settings, 4, runner=runner)
    b = timestral_forecast(history, NOW, {**settings, "_ned_snapshot": snapshot}, 4, runner=runner)
    assert requests[1].pop("residual_load") == [7000] * 196
    assert requests[0] == requests[1]
    assert b.model_version == a.model_version + ned.VERSION_SUFFIX
    assert a.points == b.points
    with pytest.raises(ValueError, match="tijdig"):
        timestral_forecast(history, NOW, {**settings, "_ned_snapshot": {**snapshot,
                           "observed_at": utc_iso(NOW + STEP)}}, 4, runner=runner)


def test_stored_shadow_pair_scores_only_later_prices_and_leaves_live_alone(tmp_path, monkeypatch):
    clock = [NOW + STEP]
    app = AppService(store=Store(tmp_path), clock=lambda: clock[0])
    settings = {"tariff_entity": "sensor.prices", "tariff_unit": "EUR/kWh",
                "active_method": "timestral", "forecast_methods": {
                    "local": False, "v4": False, "epex": False, "timestral": True},
                "timestral_weather": True, "timestral_ned_ab": True,
                "timestral_model": "123M", "timestral_context_days": 2}
    app.store.set_settings(settings)
    assert app.get_settings()["active_method"] == "timestral"
    history, weather, snapshot = case_data()
    version = timestral_version(app.get_settings())
    baseline = [{"start_utc": NOW + i * STEP, "end_utc": NOW + (i + 1) * STEP,
                 "price": .30} for i in range(2)]
    app.store.save_forecast("live-id", NOW, version, "voorlopig", [], {
        "method": "timestral", "tariff_entity": "sensor.prices", "tariff_unit": "EUR/kWh",
        "history": history, "weather_snapshot": weather,
    }, baseline)
    app.store.save_snapshot(NOW + STEP, ned.SOURCE, {**snapshot, "observed_at": utc_iso(NOW + STEP)})
    monkeypatch.setenv("NED_API_KEY", "secret-for-test")
    monkeypatch.setattr(ned, "fetch_snapshot", lambda *a: snapshot)
    calls = []
    spec = model_spec("123M")

    def infer(method, request, settings, **kwargs):
        calls.append(request)
        assert "residual_load" in request
        horizon = request["prediction_length"]
        return {"model": spec["repo"], "revision": spec["revision"], "calendar": True,
                "weather": True, "residual_load": True,
                "median": [.23] * horizon, "lower": [.12] * horizon, "upper": [.4] * horizon}

    monkeypatch.setattr("app.local_runtime.run_local", infer)
    # A future snapshot cannot be selected. The fresh baseline is within ten minutes.
    clock[0] = NOW + timedelta(minutes=2)
    app.store.set_source_status(ned.SOURCE, success=True, attempted_at=NOW)
    timestral_ab.collect(app)
    assert calls == []
    # The retained vintage covers the rest of the original seven-day input.
    app.store.save_snapshot(NOW - STEP, ned.SOURCE, snapshot)
    timestral_ab.collect(app)
    timestral_ab.collect(app)
    assert len(calls) == 1
    assert app.store.latest_forecast(method="timestral")[0]["id"] == "live-id"
    assert app.store.latest_forecast(evaluation_mode=timestral_ab.MODE)[0]["inputs"]["baseline_run_id"] == "live-id"
    archive = app._archive_entity("sensor.prices", app.get_settings())
    clock[0] = NOW + timedelta(days=1)
    app.store.upsert_quarters([
        {**row, "entity_id": archive, "price": .20, "observed_at": seen}
        for row, seen in zip(baseline, (NOW - STEP, NOW + STEP))
    ])
    result = timestral_ab.report(app)
    assert result["key_configured"] and "secret-for-test" not in str(result)
    assert (result["issues"], result["targets"]) == (1, 1)
    assert result["scores"][0]["baseline"]["mae"] == pytest.approx(.10)
    assert result["scores"][0]["candidate"]["mae"] == pytest.approx(.03)
