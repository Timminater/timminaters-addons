from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from app import forecast_core, model

UTC = timezone.utc
AMS = ZoneInfo("Europe/Amsterdam")


def row(start: datetime, price: float, published: datetime | None = None) -> dict:
    value = {
        "start_utc": start.isoformat(),
        "end_utc": (start + timedelta(minutes=15)).isoformat(),
        "price": price,
    }
    if published is not None:
        value["published_at"] = published.isoformat()
    return value


def quarter_history(issue: datetime, days: int = 28) -> list[dict]:
    begin = issue.replace(minute=(issue.minute // 15) * 15, second=0, microsecond=0) - timedelta(days=days)
    rows = []
    current = begin
    while current < issue:
        local = current.astimezone(AMS)
        rows.append(row(current, 80.0 + local.hour / 10 + local.weekday(), current + timedelta(minutes=1)))
        current += timedelta(minutes=15)
    return rows


class ModelTests(unittest.TestCase):
    def test_local_core_matches_recorded_v4_normal_case(self):
        target = datetime(2026, 9, 23, 12, tzinfo=AMS)
        history = [
            {"time": (target - timedelta(days=day, hours=hour)).isoformat(), "price": 100.0 + day}
            for day in range(1, 29)
            for hour in range(24)
        ]
        forecast = forecast_core.forecast_one(
            target_dt=target,
            history=history,
            shortwave_ratio=0.45,
            wind_ms=3.0,
            temp_c=7.0,
            ttf_ratio=1.2,
            days_ahead=2,
        )
        self.assertIsNotNone(forecast)
        self.assertAlmostEqual(forecast.baseline, 109.46, places=2)
        self.assertEqual(forecast.total_points, 11)
        self.assertAlmostEqual(forecast.predicted, 127.52, places=2)
        self.assertAlmostEqual(forecast.band_half, 48.8805, places=3)
        self.assertEqual(forecast.factor_points["scarcity"], 7)

    def test_local_core_regimes_match_recorded_v4_outputs(self):
        cases = [
            ("2026-06-20T13:00:00+02:00", 2.2, 17, 25, 1.0, 114.71, -7, 102.66, "oversupply", "nonlinear", -3),
            ("2026-01-15T20:00:00+01:00", 0.3, 2, -2, 1.2, 107.87, 22, 143.47, "schaarste", "scarcity", 18),
            ("2026-07-20T20:00:00+02:00", 1.1, 3, 27, 1.1, 110.19, 15, 134.99, "zomerschaarste", "zomerschaarste", 12),
            ("2026-12-25T12:00:00+01:00", 0.8, 9, 4, 1.0, 113.24, -2, 109.85, "normaal", "dagtype", -2),
            ("2026-05-10T13:00:00+02:00", 2.8, 20, 29, 1.0, -20.06, -11, -16.75, "oversupply", "nonlinear", -3),
        ]
        for iso, solar, wind, temperature, gas, base, points, price, regime, factor, factor_points in cases:
            with self.subTest(iso=iso):
                target = datetime.fromisoformat(iso)
                history = [
                    {"time": (target - timedelta(days=day, hours=hour)).isoformat(),
                     "price": (-30 if price < 0 else 100) + day}
                    for day in range(1, 29) for hour in range(24)
                ]
                result = forecast_core.forecast_one(target, history, solar, wind, temperature, gas, 2)
                self.assertIsNotNone(result)
                self.assertEqual((result.baseline, result.total_points, result.predicted, result.regime),
                                 (base, points, price, regime))
                self.assertEqual(result.factor_points[factor], factor_points)

    def test_local_core_missing_and_future_prices(self):
        target = datetime(2026, 9, 23, 12, tzinfo=AMS)
        self.assertIsNone(forecast_core.forecast_one(target, [], 1, 8, 15, 1, 1))
        history = [{"time": (target - timedelta(days=day)).isoformat(), "price": 100.0}
                   for day in range(1, 29)]
        expected = forecast_core.forecast_one(target, history, 1, 8, 15, 1, 1)
        future = history + [{"time": target.isoformat(), "price": 9999.0}]
        self.assertEqual(forecast_core.forecast_one(target, future, 1, 8, 15, 1, 1), expected)

    def test_quarter_adapter_starts_after_known_chain_and_filters_late_publication(self):
        issue = datetime(2026, 9, 23, 12, 10, tzinfo=UTC)
        history = quarter_history(issue)
        history.extend(
            [
                row(datetime(2026, 9, 23, 12, 0, tzinfo=UTC), 0.31, issue - timedelta(minutes=3)),
                row(datetime(2026, 9, 23, 12, 15, tzinfo=UTC), 0.32, issue - timedelta(minutes=2)),
                row(datetime(2026, 9, 23, 12, 30, tzinfo=UTC), 9.99, issue + timedelta(minutes=1)),
            ]
        )
        weather = {
            "hourly": {
                "2026-09-23T12:00:00+00:00": {
                    "shortwave_radiation": 180.0,
                    "wind_ms_10m": 6.0,
                    "temp_c": 16.0,
                }
            }
        }
        run = model.forecast_quarters(history, issue, weather, max_points=4)
        self.assertEqual(run.quality, "voorlopig")
        self.assertEqual(run.points[0].start_utc, datetime(2026, 9, 23, 12, 30, tzinfo=UTC))
        self.assertEqual(run.points[0].source, "quarter-v4")
        self.assertTrue(all(p.lower < p.price < p.upper for p in run.points))
        self.assertIn("weerinput ontbreekt: ttf_ratio", run.reasons)
        without_future = model.forecast_quarters(history[:-1], issue, weather, max_points=4)
        self.assertEqual([p.price for p in run.points], [p.price for p in without_future.points])

    def test_malformed_history_rows_are_ignored_with_provisional_empty_result(self):
        run = model.forecast_quarters(
            [{"start_utc": "not-a-time", "price": 20}, {"start_utc": "2026-09-22T12:07:00Z", "price": 30}],
            datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(run.points, [])
        self.assertIn("geen bruikbare kwartierhistorie beschikbaar", run.reasons)

    def test_short_archive_uses_complete_observed_hour_and_flat_quarters(self):
        issue = datetime(2026, 9, 23, 12, 2, tzinfo=UTC)
        history = []
        previous_hour = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
        for index in range(4):
            history.append(row(previous_hour + timedelta(minutes=15 * index), 0.2 + index / 100))
        history.append(row(datetime(2026, 9, 23, 12, 0, tzinfo=UTC), 0.27, issue - timedelta(minutes=1)))
        weather = {
            "hourly": {
                "2026-09-23T12:00:00+00:00": {
                    "solar_ratio": 1.0,
                    "wind_ms": 8.0,
                    "temp_c": 14.0,
                    "ttf_ratio": 1.0,
                }
            }
        }
        run = model.forecast_quarters(history, issue, weather, max_points=3)
        self.assertEqual([p.start_utc.minute for p in run.points], [15, 30, 45])
        self.assertTrue(all(p.source == "hour-v4-flat-quarter" for p in run.points))
        self.assertEqual(len({p.price for p in run.points}), 1)

    def test_repeated_autumn_hour_history_keeps_offsets_separate(self):
        first = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
        second = datetime(2026, 10, 25, 1, 15, tzinfo=UTC)
        target = first.astimezone(AMS)
        samples = model._quarter_samples(
            target,
            [{"start": first, "price": 1}, {"start": second, "price": 2}],
        )
        self.assertEqual([x["price"] for x in samples], [1])
        self.assertEqual(first.astimezone(AMS).hour, second.astimezone(AMS).hour)
        self.assertNotEqual(first.astimezone(AMS).utcoffset(), second.astimezone(AMS).utcoffset())

    def test_spring_dst_sequence_uses_utc_interval_identity(self):
        issue = datetime(2026, 3, 29, 0, 50, tzinfo=UTC)
        history = quarter_history(issue, days=18)
        history.append(row(datetime(2026, 3, 29, 0, 45, tzinfo=UTC), 0.21, issue - timedelta(minutes=1)))
        weather = {
            "hourly": {
                "2026-03-29T01:00:00+00:00": {
                    "solar_ratio": 1,
                    "wind_ms": 8,
                    "temp_c": 10,
                    "ttf_ratio": 1,
                }
            }
        }
        run = model.forecast_quarters(history, issue, weather, max_points=2)
        self.assertEqual(run.points[0].start_utc, datetime(2026, 3, 29, 1, 0, tzinfo=UTC))
        self.assertEqual(run.points[0].start_utc.astimezone(AMS).hour, 3)
        self.assertEqual(run.points[0].end_utc - run.points[0].start_utc, timedelta(minutes=15))

    def test_raw_weather_conversion_matches_runner_units(self):
        target = datetime(2026, 6, 15, 12, tzinfo=AMS)
        hour = target.astimezone(UTC).replace(minute=0)
        values, missing = model._weather_for(
            target,
            {hour: {"shortwave_radiation": model._hourly_solar_norm_wh(target) / 2, "wind_ms_10m": 6, "temp_c": 20}},
        )
        self.assertAlmostEqual(values["solar_ratio"], 0.5, places=6)
        self.assertAlmostEqual(values["wind_ms"], 8.28, places=2)
        self.assertIn("ttf_ratio", missing)

    def test_price_scale_uses_eur_per_mwh_internally_and_returns_sensor_unit(self):
        issue = datetime(2026, 9, 23, 12, 2, tzinfo=UTC)
        history = quarter_history(issue, days=10)
        history.append(row(datetime(2026, 9, 23, 12, 0, tzinfo=UTC), 85.0, issue - timedelta(minutes=1)))
        weather = {
            "hourly": {
                "2026-09-23T12:00:00+00:00": {
                    "solar_ratio": 1,
                    "wind_ms": 8,
                    "temp_c": 15,
                    "ttf_ratio": 1,
                }
            }
        }
        mwh_run = model.forecast_quarters(history, issue, weather, max_points=2, price_scale=1)
        kwh_history = [dict(entry, price=entry["price"] / 1000) for entry in history]
        kwh_run = model.forecast_quarters(kwh_history, issue, weather, max_points=2, price_scale=1000)
        for mwh_point, kwh_point in zip(mwh_run.points, kwh_run.points):
            self.assertAlmostEqual(mwh_point.price / 1000, kwh_point.price, places=8)


if __name__ == "__main__":
    unittest.main()
