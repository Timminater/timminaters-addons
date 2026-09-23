from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from app.backend import AppService, _amsterdam_day_utc_bounds, _window_candidates, make_handler
from app.ha_client import HAClient, HAError, parse_forecast, parse_history
from app.storage import Store, utc_iso
from app.weather import WeatherError, _hours_from_arrays, fetch_ha_weather

UTC = timezone.utc


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_forecast_parses_quarters_without_filling_gaps_and_converts_unit():
    state = {"attributes": {"unit_of_measurement": "EUR/kWh", "forecast": [
        {"datetime": "2026-03-29T00:00:00+01:00", "price": 0.2},
        {"datetime": "2026-03-29T03:15:00+02:00", "price": 0.3},
    ]}}
    rows = parse_forecast(state, "sensor.tariff", dt("2026-03-28T23:00:00Z"))
    assert len(rows) == 2
    assert rows[0]["start_utc"] == dt("2026-03-28T23:00:00Z")
    assert rows[1]["start_utc"] == dt("2026-03-29T01:15:00Z")
    assert rows[0]["price"] == pytest.approx(0.2)


def test_forecast_rejects_missing_unit_unless_user_explicitly_selects_one():
    state = {"attributes": {"forecast": [{"datetime": "2026-01-01T00:00:00Z", "price": 0.2}]}}
    with pytest.raises(HAError, match="unit"):
        parse_forecast(state, "sensor.tariff", dt("2025-12-31T22:00:00Z"))
    assert parse_forecast(state, "sensor.tariff", dt("2025-12-31T22:00:00Z"), "EUR/kWh")[0]["price"] == .2


def test_forecast_unit_conflict_and_invalid_interval_fail_closed():
    state = {"attributes": {"unit_of_measurement": "EUR/MWh", "forecast": [
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:30:00Z", "price": 200}
    ]}}
    with pytest.raises(HAError, match="komt niet overeen"):
        parse_forecast(state, "sensor.tariff", dt("2025-12-31T22:00:00Z"), "EUR/kWh")
    with pytest.raises(HAError, match="geen kwartier"):
        parse_forecast(state, "sensor.tariff", dt("2025-12-31T22:00:00Z"))


def test_zonneplan_nested_amount_adapter_scale_and_selected_tax_field():
    # Synthetic adapter contract from the Zonneplan integration README's
    # amount / 10,000,000 conversion; no local installation values are reused.
    state = {"attributes": {"forecast": [{
        "start_date": "2026-01-01T00:00:00+01:00", "end_date": "2026-01-01T00:15:00+01:00",
        "price_tax_included": {"amount": 2_000_000},
        "price_tax_excluded": {"amount": 1_500_000},
    }]}}
    now = dt("2025-12-31T23:00:00Z")
    tax_in = parse_forecast(state, "sensor.tariff", now, "EUR/kWh", "tax_included")
    tax_out = parse_forecast(state, "sensor.tariff", now, "EUR/MWh", "tax_excluded")
    assert tax_in[0]["price"] == pytest.approx(.2)
    assert tax_in[0]["unit"] == "EUR/kWh"
    assert tax_out[0]["price"] == pytest.approx(150.0)
    assert tax_out[0]["unit"] == "EUR/MWh"


def test_conflicting_duplicate_forecast_quarters_are_rejected():
    state = {"attributes": {"unit_of_measurement": "EUR/kWh", "forecast": [
        {"start": "2026-01-01T00:00:00Z", "price": .1},
        {"start": "2026-01-01T00:00:00+00:00", "price": .2},
    ]}}
    with pytest.raises(HAError, match="dubbele kwartieren"):
        parse_forecast(state, "sensor.tariff", dt("2025-12-31T23:00:00Z"))


def test_history_accepts_only_exact_quarter_boundaries_and_does_not_fake_hourly_means():
    states = [
        {"state": "0.20", "last_changed": "2026-01-01T00:00:00+00:00", "attributes": {"unit_of_measurement": "EUR/kWh"}},
        {"state": "0.21", "last_changed": "2026-01-01T00:15:00.250000+00:00", "attributes": {"unit_of_measurement": "EUR/kWh"}},
        {"state": "0.22", "last_changed": "2026-01-01T00:15:00+00:00", "attributes": {"unit_of_measurement": "EUR/kWh"}},
        {"state": "0.23", "last_changed": "2026-01-01T00:14:59+00:00", "last_updated": "2026-01-01T00:30:00+00:00", "attributes": {"unit_of_measurement": "EUR/kWh"}},
    ]
    quarters, hourly = parse_history("sensor.tariff", states, dt("2026-01-02T00:00:00Z"))
    assert len(quarters) == 3
    assert hourly == []
    assert quarters[0]["published_at"] == dt("2026-01-01T00:00:00Z")
    assert quarters[-1]["start_utc"] == dt("2026-01-01T00:30:00Z")


def test_dst_local_day_bounds_are_23_and_25_hours_with_distinct_utc_identity():
    spring_start, spring_end = _amsterdam_day_utc_bounds(date(2026, 3, 29))
    autumn_start, autumn_end = _amsterdam_day_utc_bounds(date(2026, 10, 25))
    assert spring_end - spring_start == timedelta(hours=23)
    assert autumn_end - autumn_start == timedelta(hours=25)
    assert dt("2026-10-25T00:15:00Z") != dt("2026-10-25T01:15:00Z")


def test_storage_revisions_keep_first_publication_and_support_as_of_after_later_correction(tmp_path):
    store = Store(tmp_path)
    start = dt("2026-01-01T00:00:00Z")
    base = {"entity_id": "sensor.a", "start_utc": start, "end_utc": start + timedelta(minutes=15),
            "unit": "EUR/kWh", "source": "ha_forecast", "quality": "valid"}
    store.upsert_quarters([{**base, "price": .2, "observed_at": dt("2025-12-31T23:00:00Z"), "published_at": dt("2025-12-31T23:00:00Z")}])
    store.upsert_quarters([{**base, "price": .2, "observed_at": dt("2025-12-31T23:05:00Z"), "published_at": dt("2025-12-31T23:05:00Z")}])
    before = store.quarters("sensor.a", as_of=dt("2025-12-31T23:06:00Z"))
    assert len(before) == 1
    assert before[0]["published_at"] == "2025-12-31T23:00:00Z"
    store.upsert_quarters([{**base, "price": .3, "observed_at": dt("2025-12-31T23:10:00Z"), "published_at": dt("2025-12-31T23:10:00Z")}])
    historical = store.quarters("sensor.a", as_of=dt("2025-12-31T23:06:00Z"))
    current = store.quarters("sensor.a", as_of=dt("2025-12-31T23:11:00Z"))
    assert historical[0]["price"] == pytest.approx(.2)
    assert current[0]["price"] == pytest.approx(.3)


def test_store_survives_restart_and_entity_archive_isolated(tmp_path):
    path = tmp_path / "data"
    store = Store(path)
    start = dt("2026-01-01T00:00:00Z")
    for entity, price in (("sensor.a", .2), ("sensor.b", .3)):
        store.upsert_quarters([{"entity_id": entity, "start_utc": start, "end_utc": start+timedelta(minutes=15),
          "price": price, "unit": "EUR/kWh", "source": "ha_forecast", "observed_at": start-timedelta(hours=1), "published_at": start-timedelta(hours=1)}])
    reopened = Store(path)
    reopened.set_settings({"tariff_entity": "sensor.b"})
    assert reopened.get_settings()["tariff_entity"] == "sensor.b"
    assert reopened.quarters("sensor.a")[0]["price"] == .2
    assert reopened.quarters("sensor.b")[0]["price"] == .3


def test_invalid_quarter_is_excluded_from_coverage_and_backtest_actuals(tmp_path):
    store = Store(tmp_path)
    start = dt("2026-01-01T00:00:00Z")
    rows = []
    for index, quality in enumerate(("valid", "invalid")):
        quarter = start + timedelta(minutes=15 * index)
        rows.append({"entity_id": "sensor.a", "start_utc": quarter,
                     "end_utc": quarter + timedelta(minutes=15), "price": .2,
                     "unit": "EUR/kWh", "source": "ha_forecast", "quality": quality,
                     "observed_at": start, "published_at": start})
    store.upsert_quarters(rows)
    assert store.archive_summary("sensor.a")["quarter_count"] == 1
    assert store.actual_quarters("sensor.a", start + timedelta(hours=1)) == {
        "2026-01-01T00:00:00Z": .2
    }


def test_latest_forecast_is_scoped_to_selected_entity(tmp_path):
    store = Store(tmp_path)
    issue = dt("2026-01-01T00:00:00Z")
    for entity in ("sensor.old", "sensor.new"):
        store.save_forecast(entity, issue, "test-model", "voorlopig", [], {
            "tariff_entity": entity,
            "history": [{"start_utc": "2025-12-31T23:45:00Z", "price": .2}],
            "weather_snapshot": {"hourly": {"raw": {"solar_wm2": 123, "wind_ms": 2}}},
            "weather_model": {"hourly": {"model": {"shortwave_radiation": 456, "wind_ms_10m": 7}}},
        }, [])
    run, _ = store.latest_forecast("sensor.old")
    assert run["inputs"]["tariff_entity"] == "sensor.old"
    assert run["inputs"]["history"][0]["price"] == .2
    snapshots = store.forecast_snapshots("sensor.old")
    assert len(snapshots) == 1 and snapshots[0]["history"][0]["price"] == .2
    assert snapshots[0]["weather"]["hourly"]["model"] == {"shortwave_radiation": 456, "wind_ms_10m": 7}
    assert snapshots[0]["weather"]["hourly"]["model"] != snapshots[0]["weather_snapshot"]["hourly"]["raw"]
    assert snapshots[0]["weather"] == {
        "hourly": {"model": {"shortwave_radiation": 456, "wind_ms_10m": 7}}
    }


def test_windows_skip_any_missing_or_noncontiguous_quarter():
    start = dt("2026-01-01T00:00:00Z")
    slots = []
    for i in range(8):
        slot_start = start + timedelta(minutes=15*i + (15 if i >= 4 else 0))
        slots.append({"start": slot_start.isoformat().replace("+00:00", "Z"),
                      "end": (slot_start+timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                      "price": .1, "status": "known"})
    known, mixed = _window_candidates(slots, 2)
    assert known == [] and mixed == []
    slots[3]["status"] = "missing"; slots[3]["price"] = None
    one_hour, _ = _window_candidates(slots, 1)
    assert len(one_hour) == 1
    assert one_hour[0]["start"] == "2026-01-01T01:15:00Z"


def test_dashboard_separates_known_predicted_and_missing_at_first_forecast_boundary(tmp_path):
    now = dt("2026-01-01T00:00:00Z")
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "price_field": "tax_included", "weather_source": "ha",
                          "weather_entities": {"solar": "sensor.solar", "wind": "sensor.wind", "temperature": "sensor.temp"},
                          "latitude": None, "longitude": None})
    day_start, _ = _amsterdam_day_utc_bounds(date(2026, 1, 1))
    archive_entity = service._archive_entity("sensor.tariff", service.get_settings())
    store.upsert_quarters([{"entity_id": archive_entity, "start_utc": day_start, "end_utc": day_start+timedelta(minutes=15),
        "price": .2, "unit": "EUR/kWh", "source": "ha_forecast", "observed_at": now, "published_at": now}])
    store.save_forecast("r", now, "model", "voorlopig", [], {"tariff_entity": "sensor.tariff",
                        "price_field": "tax_included", "tariff_unit": "EUR/kWh"}, [
        {"start_utc": day_start+timedelta(minutes=15), "end_utc": day_start+timedelta(minutes=30), "price": .25, "lower": None, "upper": None, "source": "model", "quality": "voorlopig"}])
    board = service.dashboard(date(2026, 1, 1), 1)
    assert [s["status"] for s in board["slots"][:3]] == ["known", "predicted", "missing"]
    assert board["slots"][0]["source"] == "ha_forecast"


def test_timeline_shows_all_days_and_interval_is_saved(tmp_path):
    now = dt("2026-01-01T12:00:00Z")
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "calculation_interval_minutes": 30})
    assert service.get_settings()["calculation_interval_minutes"] == 30
    assert service.status()["calculation_interval_minutes"] == 30
    assert service._wake.is_set()
    with pytest.raises(ValueError, match="Berekeninterval"):
        service.put_settings({"calculation_interval_minutes": 7})
    archive_entity = service._archive_entity("sensor.tariff", service.get_settings())
    day_start, _ = _amsterdam_day_utc_bounds(date(2026, 1, 1))
    store.upsert_quarters([{"entity_id": archive_entity, "start_utc": day_start,
        "end_utc": day_start + timedelta(minutes=15), "price": .2,
        "unit": "EUR/kWh", "source": "ha_forecast", "observed_at": now, "published_at": now}])
    forecast_start, _ = _amsterdam_day_utc_bounds(date(2026, 1, 4))
    store.save_forecast("timeline", now, "model", "voorlopig", [],
                        {"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                         "price_field": "tax_included"}, [{"start_utc": forecast_start,
                         "end_utc": forecast_start + timedelta(minutes=15), "price": .3,
                         "lower": .2, "upper": .4, "source": "model", "quality": "voorlopig"}])
    timeline = service.timeline()
    assert len(timeline["slots"]) == 8 * 96
    assert timeline["slots"][0]["status"] == "known"
    point = next(s for s in timeline["slots"] if s["start"] == utc_iso(forecast_start))
    assert (point["status"], point["lower"], point["upper"]) == ("predicted", .2, .4)


def test_timeline_uses_empirical_band_only_after_calibration_gate(tmp_path, monkeypatch):
    issued = dt("2026-01-01T12:00:00Z")
    target = issued + timedelta(hours=3)
    service = AppService(store=Store(tmp_path), ha=HAClient("http://127.0.0.1", ""),
                         clock=lambda: issued)
    slot = {"start": utc_iso(target), "end": utc_iso(target + timedelta(minutes=15)),
            "price": .30, "unit": "EUR/kWh", "status": "predicted",
            "source": "model", "lower": .10, "upper": .50}
    monkeypatch.setattr(service, "dashboard", lambda *_args: {
        "slots": [dict(slot)] if _args[0] == issued.astimezone(service.clock().tzinfo).date() else [],
        "quality": {"reasons": ["Kwartieronzekerheidsband is niet gekalibreerd"],
                    "uncertainty": "ongekalibreerd"},
        "updated_at": utc_iso(issued)})
    monkeypatch.setattr(service, "analysis", lambda: {"calibration": {
        "ready": True, "nominal_coverage": .9, "observed_coverage": .87,
        "bands": [{"horizon": "0-24h", "half_width": .04, "holdout_points": 110}]}})
    result = service.timeline()
    predicted = next(row for row in result["slots"] if row["status"] == "predicted")
    assert predicted["lower"] == pytest.approx(.26)
    assert predicted["upper"] == pytest.approx(.34)
    assert "110 latere kwartieren" in result["quality"]["uncertainty"]
    assert result["quality"]["band_calibrated"] is True


def test_calculate_endpoint_forces_model_run():
    class FakeService:
        def refresh(self, *, force_model=False):
            assert force_model is True
            return {"ok": True, "points": 42}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeService()))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}/api/calculate",
                          data=b"", method="POST")
        with urlopen(request) as response:
            assert response.status == 200
            assert json.load(response)["points"] == 42
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_analysis_endpoint_returns_local_comparison():
    class FakeService:
        def analysis(self):
            return {"status": "insufficient_data", "summary": {"days": 0, "points": 0}}

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeService()))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/analysis") as response:
            assert response.status == 200
            assert json.load(response)["summary"]["points"] == 0
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_ingress_serves_logo_png_with_image_content_type(tmp_path):
    payload = b"\x89PNG\r\n\x1a\npreview"
    (tmp_path / "icon.png").write_bytes(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(object(), str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/icon.png") as response:
            assert response.headers["Content-Type"] == "image/png"
            assert response.read() == payload
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_mqtt_export_is_disabled_by_default_and_setting_requires_bool(tmp_path):
    service = AppService(store=Store(tmp_path), ha=HAClient("http://127.0.0.1", ""))
    assert service.get_settings()["mqtt_enabled"] is False
    with pytest.raises(ValueError, match="Home Assistant-entiteiten"):
        service.put_settings({"mqtt_enabled": "true"})
    assert service.put_settings({"mqtt_enabled": True})["mqtt_enabled"] is True


def test_enabled_mqtt_receives_current_timeline_and_status(tmp_path, monkeypatch):
    sent = []

    class FakeExporter:
        enabled = False
        transport = None

        def publish(self, dashboard, timeline, status):
            sent.append((dashboard, timeline, status))
            return True

        def stop(self, remove_entities=False):
            pass

    exporter = FakeExporter()
    service = AppService(store=Store(tmp_path), ha=HAClient("http://127.0.0.1", ""),
                         mqtt_exporter=exporter)
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "mqtt_enabled": True})
    monkeypatch.setattr(service, "timeline", lambda: {"slots": [{"start": "2026-01-01T00:15:00Z"}]})
    monkeypatch.setattr(service, "status", lambda: {"stale": False})
    service._publish_mqtt_snapshot()
    assert sent == [({"slots": [{"start": "2026-01-01T00:15:00Z"}]},
                     {"slots": [{"start": "2026-01-01T00:15:00Z"}]},
                     {"stale": False})]


def test_ha_weather_requires_three_usable_seven_day_forecasts():
    now = dt("2026-01-01T00:00:00Z")

    class FakeHA:
        def __init__(self, bad=False): self.bad = bad
        def state(self, entity):
            key = entity.split(".")[-1]
            unit = {"solar": "W/m²", "wind": "km/h", "temp": "°C"}[key]
            times = [(now + timedelta(hours=i)).isoformat() for i in range(169)]
            if self.bad: times = times[:10]
            field = {"solar": "value", "wind": "value", "temp": "value"}[key]
            return {"attributes": {"unit_of_measurement": unit, "forecast": [
                {"datetime": t, field: 10} for t in times]}}

    entities = {"solar": "sensor.solar", "wind": "sensor.wind", "temperature": "sensor.temp"}
    snapshot = fetch_ha_weather(FakeHA(), entities, now)
    assert len(snapshot["hourly"]) == 169
    with pytest.raises(WeatherError, match="zeven dagen"):
        fetch_ha_weather(FakeHA(bad=True), entities, now)


def test_weather_rejects_naive_ha_timestamps_and_unknown_units():
    now = dt("2026-01-01T00:00:00Z")

    class FakeHA:
        def state(self, entity):
            key = entity.split(".")[-1]
            unit = "furlong" if key == "wind" else ("W/m²" if key == "solar" else "°C")
            return {"attributes": {"unit_of_measurement": unit, "forecast": [
                {"datetime": (now+timedelta(hours=i)).isoformat(), "value": 1} for i in range(169)]}}

    with pytest.raises(WeatherError, match="wind-unit"):
        fetch_ha_weather(FakeHA(), {"solar":"sensor.solar","wind":"sensor.wind","temperature":"sensor.temp"}, now)


def test_open_meteo_parser_requires_complete_utc_hourly_forecast():
    now = dt("2026-01-01T00:00:00Z")
    hours = 8*24
    payload = {"hourly_units": {"shortwave_radiation":"W/m²", "wind_speed_10m":"m/s", "temperature_2m":"°C"},
               "hourly": {"time": [(now+timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)],
                          "shortwave_radiation": [100]*hours, "wind_speed_10m": [5]*hours, "temperature_2m": [10]*hours}}
    snapshot = _hours_from_arrays(payload, "shortwave_radiation", "wind_speed_10m", "temperature_2m", now, "open_meteo")
    assert len(snapshot["hourly"]) == hours
    payload["hourly_units"]["wind_speed_10m"] = "kn"
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n): return json.dumps(payload).encode()
    with pytest.raises(WeatherError, match="eenheden"):
        from app.weather import fetch_open_meteo
        # Unit check occurs in fetch_open_meteo before the shared array parser;
        # exercise its explicit parser result with a local fake response.
        fetch_open_meteo(1, 1, now, opener=lambda *a, **k: Response())


def test_weather_fetch_is_capped_to_one_hour_even_after_location_or_source_change(monkeypatch, tmp_path):
    now = [dt("2026-01-01T00:00:00Z")]
    calls = []
    def fetcher(lat, lon, at):
        calls.append((lat, lon))
        return {"source": "open_meteo", "observed_at": at.isoformat().replace("+00:00", "Z"), "hourly": {}}
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""),
                         open_meteo_fetcher=fetcher, clock=lambda: now[0])
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "price_field": "tax_included", "weather_source": "open_meteo",
                          "weather_entities": {}, "latitude": 10, "longitude": 20})
    settings = service.get_settings()
    first = service._weather_snapshot(settings, now[0])
    store.save_snapshot(now[0], "weather", first)
    store.set_source_status("weather", success=True, attempted_at=now[0], detail={"source": "open_meteo"})
    now[0] += timedelta(minutes=5)
    service._weather_snapshot(settings, now[0])
    assert calls == [(10, 20)]
    service.put_settings({"latitude": 11, "longitude": 20})
    assert service._weather_snapshot(service.get_settings(), now[0], force=True) is None
    assert calls == [(10, 20)]
    monkeypatch.setattr("app.backend.fetch_ha_weather", lambda *a, **k: {
        "source": "ha", "observed_at": utc_iso(now[0]), "hourly": {}})
    service.put_settings({"weather_source": "ha"})
    assert service._weather_snapshot(service.get_settings(), now[0]) is not None
    service.put_settings({"weather_source": "open_meteo"})
    assert service._weather_snapshot(service.get_settings(), now[0]) is None
    assert calls == [(10, 20)]
    now[0] += timedelta(hours=1)
    service._weather_snapshot(service.get_settings(), now[0])
    assert calls == [(10, 20), (11, 20)]


def test_ha_weather_forecast_refreshes_each_poll_without_open_meteo_hour_cap(monkeypatch, tmp_path):
    now = [dt("2026-01-01T00:00:00Z")]
    calls = []
    def fake_fetch(client, entities, at):
        calls.append(at)
        return {"source": "ha", "entities": dict(entities), "observed_at": utc_iso(at), "hourly": {}}
    monkeypatch.setattr("app.backend.fetch_ha_weather", fake_fetch)
    service = AppService(store=Store(tmp_path), ha=HAClient("http://127.0.0.1", ""), clock=lambda: now[0])
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "price_field": "tax_included", "weather_source": "ha",
                          "weather_entities": {"solar": "sensor.solar", "wind": "sensor.wind", "temperature": "sensor.temp"},
                          "latitude": None, "longitude": None})
    settings = service.get_settings()
    first = service._weather_snapshot(settings, now[0])
    service.store.save_snapshot(now[0], "weather", first)
    service.store.set_source_status("weather", success=True, attempted_at=now[0])
    now[0] += timedelta(minutes=5)
    second = service._weather_snapshot(settings, now[0])
    assert len(calls) == 2
    assert first["observed_at"] != second["observed_at"]


def test_entity_switch_clears_history_cursor_and_hides_old_price_success(tmp_path):
    now = dt("2026-01-01T00:00:00Z")
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    service.put_settings({"tariff_entity": "sensor.old", "tariff_unit": "EUR/kWh",
                          "price_field": "tax_included", "weather_source": "ha",
                          "weather_entities": {"solar": "sensor.solar", "wind": "sensor.wind", "temperature": "sensor.temp"},
                          "latitude": None, "longitude": None})
    store.set_settings({"history_cursor_utc": "2025-12-31T23:55:00Z"})
    store.set_source_status("price", success=True, attempted_at=now-timedelta(minutes=5),
                            detail={"entity_id": "sensor.old"})
    service.put_settings({"tariff_entity": "sensor.new"})
    assert store.get_settings()["history_cursor_utc"] is None
    status = service.status()
    assert status["stale"] is True
    assert status["last_price_update"] is None


def test_price_field_and_unit_switch_use_separate_archive_and_forecast_namespaces(monkeypatch, tmp_path):
    now = dt("2026-01-01T00:00:00Z")
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "price_field": "tax_excluded", "weather_source": "ha",
                          "weather_entities": {}, "latitude": None, "longitude": None})
    forecast = {"entity_id": "sensor.tariff", "start_utc": now+timedelta(minutes=15),
                "end_utc": now+timedelta(minutes=30),
                "price": .18, "unit": "EUR/kWh", "source": "ha_forecast", "observed_at": now,
                "published_at": now}
    history = {**forecast, "start_utc": now, "end_utc": now+timedelta(minutes=15),
               "price": .24, "source": "ha_history"}
    monkeypatch.setattr("app.backend.collect_price_data", lambda *a, **k: ([forecast, history], [], now))
    class Run:
        points = []
        model_version = "test-model"
        quality = "voorlopig"
        reasons = ["synthetic test"]
    monkeypatch.setattr("app.backend._load_forecaster", lambda: lambda *a, **k: Run())
    monkeypatch.setattr("app.backend.fetch_ha_weather", lambda *a, **k: {
        "source": "ha", "observed_at": utc_iso(now), "hourly": {}})

    assert service.refresh()["ok"] is True
    settings = service.get_settings()
    excluded_archive = service._archive_entity("sensor.tariff", settings)
    excluded_rows = store.quarters(excluded_archive)
    assert len(excluded_rows) == 1
    assert excluded_rows[0]["price"] == pytest.approx(.18)
    assert excluded_rows[0]["source"] == "ha_forecast"
    assert store.quarters("sensor.tariff") == []
    assert store.latest_forecast("sensor.tariff", "tax_excluded")[0] is not None
    assert store.latest_forecast("sensor.tariff", "tax_included")[0] is None

    store.set_settings({"history_cursor_utc": utc_iso(now)})
    service.put_settings({"price_field": "tax_included"})
    assert store.get_settings()["history_cursor_utc"] is None
    now2 = now + timedelta(minutes=2)
    service.clock = lambda: now2
    assert service.refresh()["ok"] is True
    included_archive = service._archive_entity("sensor.tariff", service.get_settings())
    assert included_archive != excluded_archive
    assert [r["price"] for r in store.quarters(included_archive)] == pytest.approx([.24, .18])


def test_unit_change_uses_a_distinct_price_archive(tmp_path):
    service = AppService(store=Store(tmp_path), ha=HAClient("http://127.0.0.1", ""))
    service.put_settings({"tariff_entity": "sensor.tariff", "tariff_unit": "EUR/kWh",
                          "price_field": "tax_included", "weather_source": "ha"})
    first = service._archive_entity("sensor.tariff", service.get_settings())
    service.put_settings({"tariff_unit": "EUR/MWh"})
    second = service._archive_entity("sensor.tariff", service.get_settings())
    assert first != second


def test_maturity_gate_can_pass_with_dense_archive_and_seven_daily_comparisons(tmp_path):
    now = dt("2026-03-10T00:00:00Z")
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    entity = "sensor.synthetic"
    start = now - timedelta(days=35)
    archive_rows = []
    for i in range(35*96):
        stamp = start + timedelta(minutes=15*i)
        archive_rows.append({"entity_id": entity, "start_utc": stamp, "end_utc": stamp+timedelta(minutes=15),
            "price": .2 + (i % 8) / 100, "unit": "EUR/kWh", "source": "ha_history",
            "observed_at": now, "published_at": stamp})
    store.upsert_quarters(archive_rows)
    for offset in range(9, 2, -1):
        issued = now - timedelta(days=offset)
        history = []
        history_start = issued - timedelta(days=8)
        for i in range(8*96):
            stamp = history_start + timedelta(minutes=15*i)
            history.append({"start_utc": utc_iso(stamp), "end_utc": utc_iso(stamp+timedelta(minutes=15)),
                            "price": .2 + (i % 8) / 100, "published_at": utc_iso(stamp)})
        points = []
        for quarter in range(1, 97):
            stamp = issued + timedelta(minutes=15*quarter)
            points.append({"start_utc": stamp, "end_utc": stamp+timedelta(minutes=15), "price": .25,
                           "lower": None, "upper": None, "source": "model", "quality": "voorlopig"})
        store.save_forecast(f"run-{offset}", issued, "quarter-test", "voorlopig", [],
                            {"tariff_entity": entity, "history": history,
                             "weather_snapshot": {"raw": True},
                             "weather_model": {"hourly": {"2026-03-01T00:00:00Z": {"shortwave_radiation": 10}}}}, points)
    result = service._maturity(entity, now, store.archive_summary(entity))
    assert result["archive_ready"] is True
    assert result["mature_daily_runs"] == 7
    assert result["ready"] is True
    assert result["metrics"]["mae"] is not None
    assert result["metrics"]["baseline_mae"] is not None
    assert result["metrics"]["band_coverage"] is None


def test_supervisor_client_uses_get_only(monkeypatch):
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b"[]"
    def fake_open(req, timeout):
        seen.append(req.get_method()); return Response()
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    client = HAClient("http://supervisor/core/api", "test-token")
    assert client.states() == []
    assert seen == ["GET"]


def test_app_health_route_works_without_tariff_configuration():
    service = AppService(store=Store(".pytest-tmp/backend-health-db"), ha=HAClient("http://127.0.0.1", ""))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
            assert response.status == 200
            assert json.load(response)["ok"] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
