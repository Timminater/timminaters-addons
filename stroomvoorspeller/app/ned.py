"""Point-in-time Dutch residual-load data from NED (hourly capacity in MW)."""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .ha_client import HAError, parse_dt
from .storage import utc_iso

SOURCE = "ned_residual_load"
VERSION_SUFFIX = ":ned-residual-load-1"
HOUR = timedelta(hours=1)
QUARTER = timedelta(minutes=15)
MAX_AGE = timedelta(hours=6)
SERIES = ((59, 2), (2, 1), (1, 1), (51, 1))


def api_key(data_dir: Path) -> str:
    try:
        key = (data_dir / "ned-api-key").read_text(encoding="ascii")
    except FileNotFoundError:
        key = os.environ.get("NED_API_KEY")
    if key is None:
        try:
            options = json.loads((data_dir / "options.json").read_text(encoding="utf-8"))
            key = options.get("ned_api_key") if isinstance(options, dict) else None
        except (OSError, ValueError):
            return ""
    key = key.strip() if isinstance(key, str) else ""
    return key if key.isascii() and key.isprintable() else ""


def validate_key(value):
    key = value.strip() if isinstance(value, str) else ""
    if not (0 < len(key) <= 1024 and key.isascii() and key.isprintable() and " " not in key):
        raise ValueError("Vul een geldige NED API-sleutel in.")
    return key


def save_key(data_dir: Path, key: str):
    key = validate_key(key)
    with tempfile.NamedTemporaryFile(dir=data_dir, mode="w", encoding="ascii", delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(key)
            out.close()
            temp.replace(data_dir / "ned-api-key")
        finally:
            temp.unlink(missing_ok=True)


def migrate_key(data_dir: Path):
    if not (data_dir / "ned-api-key").exists():
        key = api_key(data_dir)
        if key:
            save_key(data_dir, key)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(params, key, timeout):
    request = Request("https://api.ned.nl/v1/utilizations?" + urlencode(params),
                      headers={"Accept": "application/ld+json", "X-AUTH-TOKEN": key})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("NED-antwoord is te groot")
        return json.loads(body)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise ValueError(f"NED gaf HTTP {code}") from None
    except (URLError, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("NED-aanvraag mislukt of gaf ongeldige JSON") from None


def _series(key, data_type, activity, classification, start, end, cutoff, deadline, request):
    rows = {}
    for page in range(1, 9):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("NED-aanvragen duurden te lang")
        data = request({
            "point": 0, "type": data_type, "activity": activity, "classification": classification,
            "granularity": 5, "granularitytimezone": 0, "itemsPerPage": 200, "page": page,
            "validfrom[after]": utc_iso(start), "validfrom[strictly_before]": utc_iso(end),
        }, key, min(5, remaining))
        members = data.get("hydra:member", data.get("member")) if isinstance(data, dict) else None
        if not isinstance(members, list):
            raise ValueError("Onverwacht formaat van NED-reeks")
        for row in members:
            try:
                stamp = parse_dt(row["validfrom"])
                updated = parse_dt(row["lastupdate"])
                stop = parse_dt(row["validto"])
                capacity = row["capacity"]
            except (HAError, KeyError, TypeError, ValueError):
                continue
            if (start <= stamp < end and not stamp.minute and not stamp.second
                    and stop - stamp == HOUR and updated <= cutoff
                    and type(capacity) in (int, float) and math.isfinite(capacity) and capacity >= 0):
                stamp = utc_iso(stamp)
                if stamp not in rows or rows[stamp][1] < updated:
                    rows[stamp] = capacity / 1000, updated
        view = data.get("hydra:view", data.get("view")) or {}
        if not isinstance(view, dict):
            raise ValueError("Onverwachte NED-paginering")
        if not (view.get("hydra:next") or view.get("next")):
            return rows
    raise ValueError("NED-reeks heeft meer pagina's dan verwacht")


def fetch_snapshot(key, clock, *, request=_request):
    started = parse_dt(clock())
    hour = started.replace(minute=0, second=0, microsecond=0)
    deadline = time.monotonic() + 45
    values = {}
    components = {}
    for classification, start, end in ((2, hour - timedelta(days=14), hour),
                                       (1, hour, hour + timedelta(days=8))):
        load, solar, onshore, offshore = (
            _series(key, kind, activity, classification, start, end, started, deadline, request)
            for kind, activity in SERIES
        )
        for label, series in zip(("load", "solar", "wind_onshore", "wind_offshore"), (load, solar, onshore, offshore)):
            dest = components.setdefault(label, {})
            for stamp, (mw, updated) in series.items():
                dest[stamp] = [mw, utc_iso(updated)]
        common = load.keys() & solar.keys() & onshore.keys() & offshore.keys()
        if not common:
            raise ValueError("NED heeft geen gezamenlijke uren voor verbruik, zon en beide windreeksen")
        for stamp in sorted(common):
            values[stamp] = [load[stamp][0] - solar[stamp][0] - onshore[stamp][0] - offshore[stamp][0],
                             *[utc_iso(series[stamp][1]) for series in (load, solar, onshore, offshore)]]
    return {"observed_at": utc_iso(clock()), "forecast_from": utc_iso(hour), "hourly": values, "components_mw": components}


def validate_covariate(row, context, horizon):
    if (not isinstance(row, list) or len(row) != context + horizon
            or any(v is not None and (type(v) not in (float, int) or not math.isfinite(v)) for v in row)):
        raise ValueError("Ongeldige Timestral-restvraagreeks")
    future = row[context:]
    covered = next((i for i, v in enumerate(future) if v is None), horizon)
    if (sum(v is not None for v in row[:context]) < 192 or covered < max(min(96, horizon), horizon - 96)
            or any(v is not None for v in future[covered:])):
        raise ValueError("NED-restvraag heeft onvoldoende historie of onvolledige verwachte uren")


def covariate(snapshot, begin, context, horizon, issued_at):
    observed = parse_dt(snapshot["observed_at"])
    if not timedelta(0) <= issued_at - observed <= MAX_AGE:
        raise ValueError("Geen tijdig opgeslagen NED-verwachting voor deze uitgifte")
    forecast_from = parse_dt(snapshot["forecast_from"])
    future_start = begin + context * QUARTER
    if future_start < forecast_from:
        raise ValueError("NED-verwachting begint na het eerste doelkwartier")
    hourly = snapshot["hourly"]
    row = []
    for i in range(context + horizon):
        stamp = utc_iso((begin + i * QUARTER).replace(minute=0, second=0, microsecond=0))
        value = hourly.get(stamp)
        row.append(value[0] if value and all(parse_dt(t) <= observed for t in value[1:]) else None)
    validate_covariate(row, context, horizon)
    return row
