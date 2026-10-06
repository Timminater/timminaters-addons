from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json

import pytest

from app.backend import AppService
from app.energy_prices import EnergyPriceError, all_in_price, fetch_prices
from app.ha_client import HAClient
from app.storage import Store

UTC = timezone.utc


def test_fetch_accepts_spring_dst_quarter_day_and_validates_source_metadata():
    now = datetime(2026, 3, 29, 12, tzinfo=UTC)
    start = datetime(2026, 3, 28, 23, tzinfo=UTC)
    stamps = [int((start + timedelta(minutes=15 * i)).timestamp()) for i in range(92)]
    payload = {"unit": "EUR / MWh", "license_info": "CC BY 4.0 from Energy-Charts",
               "unix_seconds": stamps, "price": [-20.0] * len(stamps)}
    result = fetch_prices(now, opener=lambda *_a, **_k: BytesIO(json.dumps(payload).encode()))
    assert len(result["prices"]) == 92
    assert result["fetched_at"] == now.isoformat()
    payload["unit"] = "EUR/kWh"
    with pytest.raises(EnergyPriceError, match="eenheid of licentie"):
        fetch_prices(now, opener=lambda *_a, **_k: BytesIO(json.dumps(payload).encode()))


def test_all_in_handles_negative_market_and_fails_outside_validity():
    settings = {"tariff_valid_from": "2026-01-01", "tariff_valid_to": "2026-12-31",
                "energy_tax_eur_kwh": .09161, "vat_percent": 21,
                "supplier_fee_eur_kwh_incl_vat": .02}
    assert all_in_price(-.05, settings, datetime(2026, 10, 1, tzinfo=UTC)) == pytest.approx((-.05 + .09161) * 1.21 + .02)
    with pytest.raises(EnergyPriceError, match="niet geldig"):
        all_in_price(.1, settings, datetime(2027, 1, 1, tzinfo=UTC))


def test_tibber_requires_explicit_fee_and_confirmation_for_all_in(tmp_path):
    service = AppService(store=Store(tmp_path), ha=HAClient("http://127.0.0.1", ""))
    with pytest.raises(ValueError, match="bevestig"):
        service.put_settings({"price_source": "energy_charts_nl", "price_choice": "all_in", "supplier": "tibber",
                              "supplier_fee_confirmed": False})
    saved = service.put_settings({"price_source": "energy_charts_nl", "price_choice": "all_in", "supplier": "tibber",
                                  "supplier_fee_eur_kwh_incl_vat": .02178, "supplier_fee_confirmed": True})
    assert saved["supplier_fee_eur_kwh_incl_vat"] == pytest.approx(.02178)


def test_market_source_refreshes_without_ha_and_scopes_components(monkeypatch, tmp_path):
    now = datetime(2026, 10, 6, 12, 7, tzinfo=UTC)
    current_start = now.replace(minute=0, second=0, microsecond=0)
    starts = [current_start - timedelta(minutes=15 * (3456 - 1 - 95)) + timedelta(minutes=15 * i)
              for i in range(3456)]
    payload = {"fetched_at": now.isoformat(), "source": "https://api.energy-charts.info/price",
               "license_info": "CC BY 4.0", "prices": [{"start_utc": s.isoformat(), "market_eur_mwh": 100.0} for s in starts]}
    monkeypatch.setattr("app.backend.fetch_prices", lambda _now: payload)
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    service._weather_snapshot = lambda *_a, **_k: None
    service.put_settings({"price_source": "energy_charts_nl", "price_choice": "bare", "supplier": "zonneplan",
                          "energy_tax_eur_kwh": .09161, "vat_percent": 21,
                          "supplier_fee_eur_kwh_incl_vat": .02, "tariff_valid_from": "2026-01-01",
                          "tariff_valid_to": "2026-12-31", "calculation_interval_minutes": 5})
    first_identity = service._price_identity(service.get_settings())
    result = service.refresh()
    assert result["ok"] is True
    archive = service._archive_entity(first_identity, service.get_settings())
    rows = store.quarters(archive)
    assert len(rows) == 3456
    run, _points = store.latest_forecast(first_identity, "tax_included", "")
    assert run is not None
    assert len(run["inputs"]["history"]) == 3360
    assert all(datetime.fromisoformat(row["end_utc"].replace("Z", "+00:00")) <= now
               for row in run["inputs"]["history"])
    snapshot = store.latest_snapshot("energy_charts_market", successful_only=True)["payload"]
    assert len(snapshot["prices"]) == 1
    assert datetime.fromisoformat(snapshot["prices"][0]["start_utc"]) == current_start
    assert snapshot["license_info"] == "CC BY 4.0" and snapshot["fetched_at"] == now.isoformat()
    current = service.current_market_prices()
    assert current["current_market_price"]["price"] == pytest.approx(.1)
    assert current["current_all_in_price"]["price"] == pytest.approx((.1 + .09161) * 1.21 + .02)
    next_start = current_start + timedelta(minutes=15)
    later = now + timedelta(minutes=15)
    payload = {**payload, "fetched_at": later.isoformat(),
               "prices": [row for row in payload["prices"] if datetime.fromisoformat(row["start_utc"]) != next_start]}
    service.clock = lambda: later
    assert service.refresh(force_model=True)["ok"] is True
    missing_snapshot = store.latest_snapshot("energy_charts_market", successful_only=True)["payload"]
    assert missing_snapshot["prices"] == []
    assert service.current_market_prices() == {}
    old_archive = archive
    service.put_settings({"supplier": "zonneplan", "supplier_fee_eur_kwh_incl_vat": .025})
    assert service._price_identity(service.get_settings()) != first_identity
    assert service._archive_entity(service._price_identity(service.get_settings()), service.get_settings()) != old_archive


def test_current_market_sensors_require_exact_fresh_published_interval(tmp_path):
    now = datetime(2026, 10, 6, 12, 7, tzinfo=UTC)
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: now)
    service.put_settings({"price_source": "energy_charts_nl", "price_choice": "bare", "supplier": "zonneplan",
                          "energy_tax_eur_kwh": .09161, "vat_percent": 21,
                          "supplier_fee_eur_kwh_incl_vat": .02, "tariff_valid_from": "2026-01-01",
                          "tariff_valid_to": "2026-12-31"})
    start = now.replace(minute=0, second=0, microsecond=0)
    payload = {"fetched_at": now.isoformat(), "prices": [{"start_utc": start.isoformat(), "market_eur_mwh": -50.0}]}
    store.save_snapshot(now, "energy_charts_market", payload)
    prices = service.current_market_prices()
    assert prices["current_market_price"]["price"] == -.05
    assert prices["current_all_in_price"]["price"] == pytest.approx((-.05 + .09161) * 1.21 + .02)
    service.put_settings({"price_choice": "bare", "tariff_valid_from": "", "tariff_valid_to": ""})
    raw_only = service.current_market_prices()
    assert raw_only["current_market_price"]["price"] == -.05 and "current_all_in_price" not in raw_only
    store.save_snapshot(now, "energy_charts_market", {**payload, "prices": []})
    assert service.current_market_prices() == {}
    store.save_snapshot(now, "energy_charts_market", payload)
    store.set_source_status("price", success=False, attempted_at=now, error="feed unavailable")
    assert service.current_market_prices() == {}
    store.set_source_status("price", success=True, attempted_at=now, detail={})
    store.save_snapshot(now, "energy_charts_market", {**payload, "fetched_at": (now - timedelta(minutes=21)).isoformat()})
    assert service.current_market_prices() == {}
