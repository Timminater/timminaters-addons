from datetime import datetime, timedelta, timezone

from app import research_data
from app.storage import Store, utc_iso


def test_research_vintages_retry_only_failed_series_and_keep_original(tmp_path, monkeypatch):
    store = Store(tmp_path)
    clock = [datetime(2026, 11, 19, tzinfo=timezone.utc)]
    calls = []
    def fetch(kind, now):
        calls.append(kind)
        if len(calls) == 2:
            raise ValueError("unavailable")
        return {"observed_at": utc_iso(now()), "values": {"target":len(calls)}}
    monkeypatch.setattr(research_data, "fetch", fetch)
    for advance in (0, 0, 1, 5):
        clock[0] += timedelta(hours=advance)
        research_data.collect(store, lambda: clock[0])
    assert calls.count("solar") == 2 and calls.count("load") == 2
    source = research_data.SOURCE + "load"
    assert store.latest_snapshot(source, as_of=clock[0]-timedelta(minutes=1))["payload"]["values"]["target"] == 1
    assert store.latest_snapshot(source)["payload"]["values"]["target"] > 1
