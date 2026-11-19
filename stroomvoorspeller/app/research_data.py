"""Archive NL day-ahead fundamentals independently of the active model."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ha_client import parse_dt
from .storage import Store, utc_iso

SERIES = ("load", "solar", "wind_onshore", "wind_offshore")
SOURCE = "energy_charts_nl_forecast:"
ENDPOINT = "https://api.energy-charts.info/v2/public_power_forecast"


def fetch(kind, clock, opener=urlopen):
    url = ENDPOINT + "?" + urlencode({"country": "nl", "production_type": kind, "forecast_type": "day-ahead"})
    with opener(Request(url, headers={"Accept": "application/json", "User-Agent": "Stroomvoorspeller"}), timeout=15) as response:
        raw = response.read(1_000_001)
    observed = clock()
    if len(raw) > 1_000_000:
        raise ValueError("Te groot antwoord van Energy-Charts")
    body = json.loads(raw)
    attrs = body.get("attributes", {})
    if (body.get("country") != "nl" or body.get("unit") != "MW" or attrs.get("production_type") != kind
            or attrs.get("forecast_type") != "day-ahead" or body.get("interval_minutes") not in (15, 30, 60)):
        raise ValueError("Onverwachte reeks of eenheid van Energy-Charts")
    values = {}
    for point in body["data"]:
        value = point["values"].get(kind)
        if type(value) in (float, int) and math.isfinite(value):
            values[utc_iso(parse_dt(point["timestamp"]))] = value
    if not values:
        raise ValueError("Energy-Charts gaf geen bruikbare waarden")
    return {"source": url, "license": body.get("license"), "unit": "MW", "kind": kind,
            "forecast_type": "day-ahead", "interval_minutes": body["interval_minutes"],
            "observed_at": utc_iso(observed), "provider_generated_at": body.get("generated_at"), "values": values}


def collect(store, clock, stopped=lambda: False):
    statuses = store.source_statuses()
    for kind in SERIES:
        if stopped():
            break
        source = SOURCE + kind
        status = statuses.get(source, {})
        now = clock()
        if any(status.get(field) and now - parse_dt(status[field]) < delay for field, delay in (
                ("last_success", timedelta(hours=6)), ("last_attempt", timedelta(hours=1)))):
            continue
        try:
            snap = fetch(kind, clock)
            store.save_snapshot(parse_dt(snap["observed_at"]), source, snap)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            store.set_source_status(source, success=False, attempted_at=now, error=str(exc))
        else:
            store.set_source_status(source, success=True, attempted_at=now, detail={"points": len(snap["values"])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    store = Store(args.directory, database_dir=args.directory)
    collect(store, lambda: datetime.now(timezone.utc))
    for name, status in store.source_statuses().items():
        if name.startswith(SOURCE):
            print(name, status["error"] or status["detail"])


if __name__ == "__main__":
    main()
