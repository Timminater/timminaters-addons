from datetime import datetime, timedelta, timezone
import json
import sqlite3

from app.analysis import _nearest_noon, build_analysis
from app.storage import Store


UTC = timezone.utc
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


class FakeStore:
    def __init__(self, runs, actuals):
        self.runs = runs
        self.actuals = actuals
        self.archive_calls = []

    def stored_forecasts(self, entity, since, price_field, unit):
        assert (entity, price_field, unit) == ("sensor.tariff", "tax_excluded", "EUR/MWh")
        return [r for r in self.runs if datetime.fromisoformat(r["issued_at"].replace("Z", "+00:00")) >= since]

    def daily_stored_forecasts(self, entity, since, ended_by, price_field, unit, timezone_name):
        assert (entity, price_field, unit, timezone_name) == (
            "sensor.tariff", "tax_excluded", "EUR/MWh", "Europe/Amsterdam")
        selected = _nearest_noon([r for r in self.runs
            if since <= datetime.fromisoformat(r["issued_at"].replace("Z", "+00:00")) <= ended_by])
        return selected

    def actual_quarters(self, archive_entity, ended_by):
        self.archive_calls.append((archive_entity, ended_by))
        assert archive_entity == "sensor.tariff|price_field=tax_excluded|unit=EUR/MWh"
        return self.actuals


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_run(issued, count=8, value=10.0, horizons=(1,)):
    points = []
    for hours in horizons:
        for index in range(count):
            start = issued + timedelta(hours=hours, minutes=index * 15)
            points.append({"start_utc": iso(start), "end_utc": iso(start + timedelta(minutes=15)),
                           "price": value + 1.0})
    return {"issued_at": iso(issued), "points": points}


def store_for(runs):
    actuals = {}
    for run in runs:
        for point in run["points"]:
            start = datetime.fromisoformat(point["start_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(point["end_utc"].replace("Z", "+00:00"))
            if end <= NOW:
                actuals[point["start_utc"]] = 10.0
    return FakeStore(runs, actuals)


def call(store):
    return build_analysis(store, "sensor.tariff",
                          "sensor.tariff|price_field=tax_excluded|unit=EUR/MWh",
                          "tax_excluded", "EUR/MWh", NOW)


def test_uses_one_nearest_noon_run_per_amsterdam_calendar_day_and_archive_namespace():
    day = datetime(2026, 9, 23, 0, tzinfo=UTC)
    runs = [make_run(day + timedelta(hours=10), value=10),
            make_run(day + timedelta(hours=11), value=20),
            make_run(day + timedelta(hours=13), value=30)]
    store = store_for(runs)
    result = call(store)
    issued = {row["issued_at"] for row in result["comparisons"]}
    # Noon in Amsterdam is 10:00 UTC on this date, so that run represents the day.
    assert issued == {iso(day + timedelta(hours=10))}
    assert store.archive_calls == [("sensor.tariff|price_field=tax_excluded|unit=EUR/MWh", NOW)]


def test_calibration_gate_uses_only_strictly_earlier_days_and_reports_reason():
    runs = [make_run(datetime(2026, 9, 23, 10, tzinfo=UTC))]
    result = call(store_for(runs))
    assert result["calibration"]["ready"] is False
    assert "eerdere dagen" in result["calibration"]["reason"]
    assert result["calibration"]["bands"][0]["half_width"] is None
    # The previous local day can calibrate, but today's issue never can.
    assert result["calibration"]["bands"][0]["calibration_days"] == 1


def test_missing_truth_is_not_filled_or_counted():
    run = make_run(datetime(2026, 9, 23, 10, tzinfo=UTC))
    store = FakeStore([run], {})
    result = call(store)
    assert result["comparisons"] == []
    assert result["summary"]["points"] == 0
    assert all(item["n"] == 0 for item in result["horizons"])


def test_calibrated_half_width_and_holdout_coverage_use_earlier_days_only():
    runs = []
    # Thirty fully observed, distinct earlier issue dates provide enough
    # independent days and points; the last date is the holdout evaluation.
    for offset in range(30, 0, -1):
        issued = datetime(2026, 9, 24, 10, tzinfo=UTC) - timedelta(days=offset)
        runs.append(make_run(issued, count=16, value=10.0, horizons=(1, 30, 72)))
    eval_issue = datetime(2026, 9, 24, 10, tzinfo=UTC)
    runs.append(make_run(eval_issue, count=16, value=10.0, horizons=(1, 30, 72)))
    result = call(store_for(runs))
    band = result["calibration"]["bands"][0]
    assert band["half_width"] == 1.0
    assert band["calibration_days"] == 30
    assert band["calibration_points"] >= 400
    assert band["holdout_points"] >= 100
    assert band["holdout_days"] >= 7
    assert band["holdout_coverage"] == 1.0
    assert result["calibration"]["ready"] is True, result["calibration"]["bands"]


def test_future_run_cannot_change_calibration_fit_for_as_of_request():
    earlier = []
    for offset in range(25, 0, -1):
        issued = datetime(2026, 9, 24, 10, tzinfo=UTC) - timedelta(days=offset)
        earlier.append(make_run(issued, count=16, value=10.0, horizons=(1, 30, 72)))
    latest = make_run(datetime(2026, 9, 24, 10, tzinfo=UTC), count=16, value=10.0,
                      horizons=(1, 30, 72))
    baseline = call(store_for(earlier + [latest]))["calibration"]["bands"][0]["half_width"]
    future_run = make_run(datetime(2026, 9, 25, 10, tzinfo=UTC), count=20, value=10_000.0,
                          horizons=(1, 30, 72))
    with_future = call(store_for(earlier + [latest, future_run]))["calibration"]["bands"][0]["half_width"]
    assert baseline == with_future == 1.0


def test_truth_first_seen_after_holdout_issue_is_not_used_for_calibration():
    prior = make_run(datetime(2026, 9, 22, 10, tzinfo=UTC), count=8)
    latest = make_run(datetime(2026, 9, 23, 10, tzinfo=UTC), count=8)

    class RevisedArchiveStore(FakeStore):
        def __init__(self, runs, actuals):
            super().__init__(runs, actuals)
            self.as_of_calls = []

        def quarters(self, archive_entity, start, end, *, as_of):
            self.as_of_calls.append(as_of)
            # The full current archive has these prices, but suppose their
            # revisions were first observed after these historical issue times.
            return []

    store = RevisedArchiveStore([prior, latest], store_for([prior, latest]).actuals)
    result = call(store)
    assert result["comparisons"]
    assert all(row["lower"] is None and row["upper"] is None for row in result["comparisons"])
    assert result["calibration"]["ready"] is False
    assert store.as_of_calls and max(store.as_of_calls) <= NOW


def test_store_daily_selector_filters_namespace_and_does_not_return_all_runs():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE forecast_runs(id TEXT, issued_at TEXT, inputs_json TEXT);
      CREATE TABLE forecast_points(run_id TEXT, start_utc TEXT, end_utc TEXT, price REAL,
                                   lower_price REAL, upper_price REAL);
    """)
    store = Store.__new__(Store)
    store._connect = lambda: db
    day = datetime(2026, 9, 23, 0, tzinfo=UTC)
    for run_id, issued, entity, field, unit in [
        ("early", day + timedelta(hours=8), "sensor.tariff", "tax_excluded", "EUR/MWh"),
        ("noon", day + timedelta(hours=10), "sensor.tariff", "tax_excluded", "EUR/MWh"),
        ("late", day + timedelta(hours=14), "sensor.tariff", "tax_excluded", "EUR/MWh"),
        ("other-unit", day + timedelta(hours=10, minutes=1), "sensor.tariff", "tax_excluded", "EUR/kWh"),
    ]:
        db.execute("INSERT INTO forecast_runs VALUES(?,?,?)", (run_id, iso(issued), json.dumps({
            "tariff_entity": entity, "price_field": field, "tariff_unit": unit,
            "history_zlib_b64": "large-compressed-history",
        })))
        db.execute("INSERT INTO forecast_points VALUES(?,?,?,?,?,?)", (run_id,
            iso(issued + timedelta(hours=1)), iso(issued + timedelta(hours=1, minutes=15)),
            0.5, None, None))
    db.commit()
    try:
        selected = store.daily_stored_forecasts(
            "sensor.tariff", day, day + timedelta(days=1), "tax_excluded", "EUR/MWh")
        assert [run["id"] for run in selected] == ["noon"]
        assert len(selected[0]["points"]) == 1
        assert "history" not in selected[0]
    finally:
        db.close()
