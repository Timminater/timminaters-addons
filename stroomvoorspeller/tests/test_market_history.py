from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json

import pytest

from app.backend import AppService
from app.ha_client import HAClient
from app.market_history import MarketHistoryError, SOURCE, derive_zonneplan_history, fetch_market_history
from app.model import forecast_quarters
from app.storage import Store

UTC = timezone.utc


def history():
    issue = datetime(2026, 9, 24, 20, 6, tzinfo=UTC)
    first = datetime(2026, 9, 22, 22, tzinfo=UTC)
    market = []
    known = []
    for i in range(9 * 96):
        start = first - timedelta(days=7) + timedelta(minutes=15 * i)
        local_hour = (start + timedelta(hours=2)).hour
        weekend = (start + timedelta(hours=2)).weekday() >= 5
        price = (.015 if weekend else .12) + local_hour / 200
        market.append({"start_utc": start.isoformat(), "market_eur_mwh": price * 1000})
        if start >= first:
            known.append({"start_utc": start.isoformat(), "end_utc": (start + timedelta(minutes=15)).isoformat(),
                          "price": 1.21 * price + .130848, "unit": "EUR/kWh", "source": "ha_forecast",
                          "published_at": issue.isoformat()})
    return issue, known, {"prices": market}


def test_backfill_is_calibrated_and_changes_weekend_model_basis():
    issue, known, market = history()
    rows, calibration = derive_zonneplan_history(known, market, issue)
    assert calibration["overlap"] >= 96
    assert calibration["max_residual"] < 1e-10
    assert calibration["slope"] == pytest.approx(1.21)
    assert calibration["offset"] == pytest.approx(.130848)
    assert rows and all(row["source"] == SOURCE and row["start_utc"] < known[0]["start_utc"] for row in rows)
    without = forecast_quarters(known, issue, max_points=240, price_scale=1000)
    with_backfill = forecast_quarters(known + rows, issue, max_points=240, price_scale=1000)
    saturday = datetime(2026, 9, 26, 11, tzinfo=UTC)
    old = next(p for p in without.points if p.start_utc == saturday)
    new = next(p for p in with_backfill.points if p.start_utc == saturday)
    assert new.price < old.price - .05
    assert new.source == "quarter-v4-market-backfill"


def test_backfill_rejects_an_unrelated_tariff():
    issue, known, market = history()
    known[30]["price"] += .01
    with pytest.raises(MarketHistoryError, match="sluiten niet nauwkeurig"):
        derive_zonneplan_history(known, market, issue)


def test_backfill_requires_real_overlap_and_never_uses_another_year():
    issue, known, market = history()
    with pytest.raises(MarketHistoryError, match="96 overlappende"):
        derive_zonneplan_history(known[:20], market, issue)
    rows, _ = derive_zonneplan_history(known, market, issue)
    assert all(datetime.fromisoformat(r["start_utc"]).year == 2026 for r in rows)


def test_market_fetch_validates_licence_unit_and_quarter_grid():
    now = datetime(2026, 9, 24, 20, 6, tzinfo=UTC)
    start = datetime(2026, 9, 23, 20, tzinfo=UTC)
    payload = {"unit": "EUR / MWh", "license_info": "CC BY 4.0 from Energy-Charts",
               "unix_seconds": [int((start + timedelta(minutes=15 * i)).timestamp()) for i in range(96)],
               "price": [100.0] * 96}
    result = fetch_market_history(now, opener=lambda *_args, **_kwargs: BytesIO(json.dumps(payload).encode()))
    assert len(result["prices"]) == 96
    assert result["prices"][0]["market_eur_mwh"] == 100
    payload["unit"] = "EUR/kWh"
    with pytest.raises(MarketHistoryError, match="eenheid"):
        fetch_market_history(now, opener=lambda *_args, **_kwargs: BytesIO(json.dumps(payload).encode()))


def test_service_keeps_derived_prices_out_of_actual_archive(monkeypatch, tmp_path):
    issue, known, market = history()
    market["fetched_at"] = issue.isoformat()
    calls = []
    def fake_fetch(now):
        calls.append(now)
        return market
    monkeypatch.setattr("app.backend.fetch_market_history", fake_fetch)
    store = Store(tmp_path)
    service = AppService(store=store, ha=HAClient("http://127.0.0.1", ""), clock=lambda: issue)
    settings = {"price_field": "tax_included", "tariff_unit": "EUR/kWh"}
    first = service._market_model_history("sensor.zonneplan_test", settings, known, issue)
    second = service._market_model_history("sensor.zonneplan_test", settings, known, issue + timedelta(minutes=5))
    assert first == second and first
    assert calls == [issue]
    assert store.quarters("sensor.zonneplan_test") == []
    assert service._market_model_history("sensor.other", settings, known, issue) == []
