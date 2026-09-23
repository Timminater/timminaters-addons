from datetime import datetime, timedelta, timezone

from app.backtest import cheapest_window, seasonal_quarter_baseline, walk_forward, evaluate_archive


UTC = timezone.utc
T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def test_cheapest_window_requires_contiguous_quarters():
    prices = {T0: 1.0, T0 + timedelta(minutes=30): 1.0}
    assert cheapest_window(prices, 2) is None
    prices[T0 + timedelta(minutes=15)] = 2.0
    assert cheapest_window(prices, 2) == (T0, 1.5)


def test_baseline_excludes_prices_published_after_issue():
    issued = T0 + timedelta(days=8)
    history = [
        {"start_utc": T0 + timedelta(days=2), "price": 1.0,
         "published_at": T0 + timedelta(days=2)},
        {"start_utc": T0 + timedelta(days=3), "price": 999.0,
         "published_at": issued + timedelta(minutes=1)},
    ]
    target = T0 + timedelta(days=9)
    assert seasonal_quarter_baseline(history, issued, [target])[target] == 1.0


def test_walk_forward_keeps_truth_out_of_forecast_input(monkeypatch):
    import app.backtest as bt

    observed = []

    class Point:
        def __init__(self, start):
            self.start_utc = start
            self.price = 4.0
            self.lower = None
            self.upper = None

    class Run:
        def __init__(self, start):
            self.points = [Point(start)]

    def fake_forecast(history, issued, weather, *, price_scale):
        observed.append((list(history), issued, weather, price_scale))
        return Run(issued + timedelta(minutes=15))

    monkeypatch.setattr(bt, "forecast_quarters", fake_forecast)
    issued = T0
    actual = issued + timedelta(minutes=15)
    metrics = walk_forward([{"issued_at": issued, "history": [], "weather": {}}],
                           {actual: 5.0})
    assert observed == [([], issued, {}, 1.0)]
    assert metrics[0].mae == 1.0
    assert metrics[0].bias == -1.0
    assert metrics[0].paired_n == 0
    assert metrics[0].paired_model_mae is None
    assert metrics[0].band_coverage is None


def test_evaluate_archive_scores_only_elapsed_valid_quarters(monkeypatch):
    import app.backtest as bt

    captured = {}
    monkeypatch.setattr(bt, "walk_forward", lambda snapshots, actuals: captured.update(
        snapshots=snapshots, actuals=actuals) or [])

    class FakeStore:
        def forecast_snapshots(self, entity_id, *, price_field, tariff_unit):
            assert entity_id == "sensor.test"
            assert (price_field, tariff_unit) == ("tax_included", "EUR/kWh")
            return [{"issued_at": T0.isoformat(), "history": [], "weather": {}}]

        def actual_quarters(self, archive_key, ended_by):
            assert archive_key == "sensor.test|price_field=tax_included|unit=EUR/kWh"
            assert ended_by == T0 + timedelta(minutes=30)
            return {T0: 1.0}

    assert evaluate_archive(FakeStore(), "sensor.test", "tax_included", "EUR/kWh",
                            T0 + timedelta(minutes=30)) == []
    assert captured["actuals"] == {T0: 1.0}


def test_window_backtest_compares_model_and_baseline_on_same_quarters(monkeypatch):
    import app.backtest as bt

    issued = T0
    starts = [issued + timedelta(minutes=15 * (index + 1)) for index in range(4)]
    model_prices = [1.0, 1.0, 3.0, 3.0]
    actual_prices = [3.0, 3.0, 1.0, 1.0]

    class Point:
        def __init__(self, start, price):
            self.start_utc, self.price = start, price
            self.lower = self.upper = None

    class Run:
        points = [Point(start, price) for start, price in zip(starts, model_prices)]

    monkeypatch.setattr(bt, "forecast_quarters", lambda *args, **kwargs: Run())
    history = [{"start_utc": start - timedelta(days=1), "price": price,
                "published_at": issued - timedelta(days=1)}
               for start, price in zip(starts, actual_prices)]
    metrics = walk_forward([{"issued_at": issued, "history": history, "weather": {}}],
                           dict(zip(starts, actual_prices)), window_quarters=2)
    first_day = metrics[0]
    assert first_day.window_regret == 2.0
    assert first_day.baseline_window_regret == 0.0
    assert first_day.paired_n == 4
    assert first_day.paired_model_mae == 2.0
    assert first_day.baseline_mae == 0.0


def test_model_baseline_mae_comparison_exposes_paired_sample(monkeypatch):
    import app.backtest as bt

    starts = [T0 + timedelta(minutes=15), T0 + timedelta(minutes=30)]

    class Point:
        def __init__(self, start, price):
            self.start_utc, self.price = start, price
            self.lower = self.upper = None

    class Run:
        points = [Point(starts[0], 10.0), Point(starts[1], 100.0)]

    monkeypatch.setattr(bt, "forecast_quarters", lambda *args, **kwargs: Run())
    history = [{"start_utc": starts[0] - timedelta(days=1), "price": 1.0,
                "published_at": T0 - timedelta(days=1)}]
    result = walk_forward([{"issued_at": T0, "history": history, "weather": {}}],
                          {starts[0]: 1.0, starts[1]: 1.0}, window_quarters=1)[0]
    assert result.n == 2
    assert result.mae == 54.0
    assert result.paired_n == 1
    assert result.paired_model_mae == 9.0
    assert result.baseline_mae == 0.0
