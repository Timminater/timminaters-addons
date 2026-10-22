"""Local weather input adapters for Open-Meteo and HA forecast entities."""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .ha_client import HAClient, HAError, parse_dt

UTC = timezone.utc
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherError(RuntimeError):
    pass


def _num(value: Any, label: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise WeatherError(f"{label} ontbreekt of is geen getal") from exc
    if not math.isfinite(n):
        raise WeatherError(f"{label} is niet eindig")
    return n


def _hours_from_arrays(payload: Mapping[str, Any], solar_key: str, wind_key: str,
                       temp_key: str, observed_at: datetime, source: str, *, coverage_end: datetime | None = None) -> dict[str, Any]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping) or not isinstance(hourly.get("time"), list):
        raise WeatherError("Weerbron bevat geen uurlijkse tijdreeks")
    times = hourly["time"]
    solar, wind, temp = hourly.get(solar_key), hourly.get(wind_key), hourly.get(temp_key)
    if not all(isinstance(a, list) for a in (solar, wind, temp)):
        raise WeatherError("Verwachting moet zon, wind en temperatuur bevatten")
    out: dict[str, Any] = {}
    for i, raw_time in enumerate(times):
        if i >= len(solar) or i >= len(wind) or i >= len(temp):
            continue
        try:
            if source == "open_meteo" and isinstance(raw_time, str):
                dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)  # API requested timezone=UTC
                ts = dt.astimezone(UTC)
            else:
                ts = parse_dt(raw_time)
            values = {"solar_wm2": _num(solar[i], "zon"), "wind_ms": _num(wind[i], "wind"),
                      "temp_c": _num(temp[i], "temperatuur")}
        except (HAError, WeatherError):
            continue
        if ts.minute or ts.second or ts.microsecond:
            continue
        out[ts.isoformat(timespec="seconds").replace("+00:00", "Z")] = values
    if not out:
        raise WeatherError("Weerbron bevat geen bruikbare uurlijkse waarden")
    _validate_coverage(out, observed_at, coverage_end=coverage_end)
    return {"source": source, "observed_at": parse_dt(observed_at).isoformat().replace("+00:00", "Z"), "hourly": out}


def _validate_coverage(hourly: Mapping[str, Any], now: datetime, *, coverage_end: datetime | None = None) -> None:
    now = parse_dt(now)
    starts = sorted(parse_dt(k) for k in hourly)
    if not starts:
        raise WeatherError("Weerreeks is leeg")
    # A forecast must reach seven days with near-hourly coverage; never silently
    # stretch the last known point over a missing range.
    target = (parse_dt(coverage_end) if coverage_end else now + timedelta(days=7)) - timedelta(hours=1)
    if starts[-1] < target:
        raise WeatherError("Weerverwachting dekt geen zeven dagen")
    expected = int((starts[-1] - starts[0]).total_seconds() // 3600) + 1
    if len(starts) < expected * 0.90:
        raise WeatherError("Weerverwachting bevat te veel ontbrekende uren")


def fetch_open_meteo(latitude: float, longitude: float, now: datetime,
                     opener=urllib.request.urlopen) -> dict[str, Any]:
    latitude, longitude = _num(latitude, "breedtegraad"), _num(longitude, "lengtegraad")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise WeatherError("Coördinaten vallen buiten het geldige bereik")
    query = urllib.parse.urlencode({"latitude": latitude, "longitude": longitude,
        "hourly": "shortwave_radiation,wind_speed_10m,temperature_2m", "forecast_days": 8,
        "timezone": "UTC", "wind_speed_unit": "ms"})
    request = urllib.request.Request(f"{OPEN_METEO_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise WeatherError("Open-Meteo response overschrijdt 2 MiB")
        payload = json.loads(raw.decode("utf-8"))
    except WeatherError:
        raise
    except Exception as exc:
        raise WeatherError(f"Open-Meteo niet beschikbaar: {exc}") from exc
    units = payload.get("hourly_units") or {}
    if units.get("shortwave_radiation") not in {"W/m²", "W/m2"} or units.get("wind_speed_10m") not in {"m/s", "ms"} or units.get("temperature_2m") not in {"°C", "°c"}:
        raise WeatherError("Open-Meteo retourneerde onverwachte eenheden")
    return _hours_from_arrays(payload, "shortwave_radiation", "wind_speed_10m", "temperature_2m", now, "open_meteo")


def _forecast_list(state: Mapping[str, Any], key: str) -> tuple[list[str], list[float], str]:
    attrs = state.get("attributes") or {}
    forecasts = attrs.get("forecast")
    if not isinstance(forecasts, list):
        raise WeatherError(f"{key}: attribuut forecast ontbreekt")
    unit = attrs.get("unit_of_measurement")
    times: list[str] = []
    values: list[float] = []
    for item in forecasts:
        if not isinstance(item, Mapping):
            continue
        t = next((item[k] for k in ("datetime", "time", "start", "start_time") if item.get(k)), None)
        value = next((item[k] for k in ("value", key, "temperature", "wind_speed", "shortwave_radiation", "solar_irradiance") if item.get(k) is not None), None)
        if t is None or value is None:
            continue
        times.append(str(t)); values.append(_num(value, key))
    if not times or not unit:
        raise WeatherError(f"{key}: forecastwaarden of eenheid ontbreken")
    return times, values, str(unit)


def _normalize_weather_value(value: float, unit: str, kind: str) -> float:
    u = unit.lower().replace(" ", "")
    if kind == "wind":
        if u in {"m/s", "ms⁻¹", "mps"}: return value
        if u in {"km/h", "kmh"}: return value / 3.6
        if u in {"mph"}: return value * 0.44704
        raise WeatherError("wind-unit moet m/s, km/h of mph zijn")
    if kind == "temperature":
        if u in {"°c", "c", "°celsius"}: return value
        if u in {"°f", "f", "°fahrenheit"}: return (value - 32) * 5 / 9
        raise WeatherError("temperatuurunit moet °C of °F zijn")
    if kind == "solar":
        if u in {"w/m²", "w/m2"}: return value
        if u in {"kw/m²", "kw/m2"}: return value * 1000
        raise WeatherError("zon-unit moet straling in W/m² of kW/m² zijn")
    raise ValueError(kind)


def fetch_ha_weather(client: HAClient, entities: Mapping[str, str], now: datetime) -> dict[str, Any]:
    required = ("solar", "wind", "temperature")
    if any(not entities.get(k) for k in required):
        raise WeatherError("Kies weerentiteiten voor zon, wind en temperatuur")
    rows: dict[str, dict[str, float]] = {}
    for kind, entity_id in (("solar", entities["solar"]), ("wind", entities["wind"]), ("temperature", entities["temperature"])):
        state = client.state(entity_id)
        if state is None:
            raise WeatherError(f"Weerentiteit {entity_id} bestaat niet")
        times, values, unit = _forecast_list(state, kind)
        for raw_t, value in zip(times, values):
            ts = parse_dt(raw_t)
            if ts.minute != 0 or ts.second or ts.microsecond:
                continue
            stamp = ts.isoformat(timespec="seconds").replace("+00:00", "Z")
            rows.setdefault(stamp, {})[kind] = _normalize_weather_value(value, unit, kind)
    hourly = {t: {"solar_wm2": d["solar"], "wind_ms": d["wind"], "temp_c": d["temperature"]}
              for t, d in rows.items() if set(d) == {"solar", "wind", "temperature"}}
    _validate_coverage(hourly, now)
    return {"source": "ha", "observed_at": parse_dt(now).isoformat().replace("+00:00", "Z"), "hourly": hourly}


def model_weather(snapshot: Mapping[str, Any], *, start: datetime | None = None) -> dict[str, Any]:
    """Translate raw units while avoiding made-up climatology ratios.

    solar_ratio is deliberately omitted: a raw radiation forecast cannot be
    compared with the source model's seasonal climatology without a validated
    local normal. The forecast engine must report that factor as unavailable.
    """
    result: dict[str, Any] = {"hourly": {}}
    for stamp, values in (snapshot.get("hourly") or {}).items():
        if (start or snapshot.get("observed_at")) and parse_dt(stamp) < parse_dt(start or snapshot["observed_at"]).replace(minute=0,second=0,microsecond=0): continue
        result["hourly"][stamp] = {"shortwave_radiation": values.get("solar_wm2"),
                                  "wind_ms_10m": values.get("wind_ms"), "temp_c": values.get("temp_c")}
    return result


def fetch_regional_weather(profile, latitude, longitude, now, data_dir):
    """One batch for the grid. 35 past days permit Timestral weather context."""
    from .weather_regions import locations
    from .weather_cache import cached_json
    points=locations(profile,latitude,longitude)
    query=urllib.parse.urlencode({'latitude':','.join(str(p[1]) for p in points),
        'longitude':','.join(str(p[2]) for p in points),'hourly':'shortwave_radiation,wind_speed_10m,temperature_2m',
        'past_days':35,'forecast_days':8,'timezone':'UTC','wind_speed_unit':'ms'})
    data,fetched=cached_json(OPEN_METEO_URL+'?'+query,data_dir)
    return _regional_snapshot(data,points,profile,datetime.fromtimestamp(fetched,UTC),now)


def _regional_snapshot(data,points,profile,observed,now,coverage_end=None):
    payloads=data if isinstance(data,list) else [data]
    if len(payloads)!=len(points): raise WeatherError('Weerbron mist locaties; geen gedeeltelijk gebied gebruikt')
    parsed=[]
    for payload in payloads:
        units=payload.get('hourly_units') or {}
        if units.get('shortwave_radiation') not in {'W/m²','W/m2'} or units.get('wind_speed_10m')!='m/s' or units.get('temperature_2m')!='°C':
            raise WeatherError('Regionale weerbron retourneerde onverwachte eenheden')
        parsed.append(_hours_from_arrays(payload,'shortwave_radiation','wind_speed_10m','temperature_2m',now,'open_meteo',coverage_end=coverage_end)['hourly'])
    shared=set.intersection(*(set(p) for p in parsed))
    hourly={stamp:{key:sum(p[stamp][key] for p in parsed)/len(parsed) for key in ('solar_wm2','wind_ms','temp_c')} for stamp in sorted(shared)}
    _validate_coverage(hourly,now,coverage_end=coverage_end)
    return {'source':'open_meteo','profile':profile,'locations':[{'name':n,'latitude':lat,'longitude':lon} for n,lat,lon in points],
        'observed_at':observed.isoformat().replace('+00:00','Z'),'hourly':hourly,
        'aggregation':'Ongewogen gemiddelde van alle punten; geen geschatte productiegewichten'}


def _archive_json(url,data_dir,stop=None):
    from .weather_cache import cached_json,WeatherMinuteBudget
    for _ in range(3):
        if stop is not None and stop.is_set(): raise WeatherError('Historisch weer ophalen gestopt')
        try: return cached_json(url,data_dir)
        except WeatherMinuteBudget as exc:
            if stop is None: raise
            if stop.wait(min(60,exc.seconds)): raise WeatherError('Historisch weer ophalen gestopt') from None
    raise WeatherError('Weer-minuutbudget nog bezet; probeer de historische test later opnieuw')


def fetch_weather_context(profile,latitude,longitude,start,end,now,data_dir,stop=None):
    """Past-only stitched GFS archive for context, never future target weather."""
    from .weather_regions import locations
    points=locations(profile,latitude,longitude)
    if not start<end<=now: raise WeatherError('Ongeldig historisch weerbereik')
    query=urllib.parse.urlencode({'latitude':','.join(str(p[1]) for p in points),
        'longitude':','.join(str(p[2]) for p in points),'hourly':'shortwave_radiation,wind_speed_10m,temperature_2m',
        'start_date':start.date().isoformat(),'end_date':end.date().isoformat(),
        'models':'gfs_global','timezone':'UTC','wind_speed_unit':'ms'})
    data,fetched=_archive_json('https://historical-forecast-api.open-meteo.com/v1/forecast?'+query,data_dir,stop)
    return _regional_snapshot(data,points,profile,datetime.fromtimestamp(fetched,UTC),now,coverage_end=end)


def fetch_archived_weather(profile,latitude,longitude,issue,horizon_hours,now,data_dir,context=None,stop=None):
    """A specific old GFS run, at least 12h before the simulated issue."""
    from .weather_regions import locations
    points=locations(profile,latitude,longitude)
    run=issue.astimezone(UTC)-timedelta(hours=12)
    run=run.replace(hour=run.hour//6*6,minute=0,second=0,microsecond=0)
    if issue+timedelta(hours=horizon_hours)>now: raise WeatherError('Historische doelhorizon is nog niet verstreken')
    query=urllib.parse.urlencode({'latitude':','.join(str(p[1]) for p in points),
        'longitude':','.join(str(p[2]) for p in points),'hourly':'shortwave_radiation,wind_speed_10m,temperature_2m',
        'run':run.strftime('%Y-%m-%dT%H:%M'),'forecast_days':10,
        'models':'gfs_global','timezone':'UTC','wind_speed_unit':'ms'})
    data,fetched=_archive_json('https://single-runs-api.open-meteo.com/v1/forecast?'+query,data_dir,stop)
    snapshot=_regional_snapshot(data,points,profile,datetime.fromtimestamp(fetched,UTC),issue,coverage_end=issue+timedelta(hours=horizon_hours))
    # Only times before the chosen forecast run may come from stitched history.
    history={t:v for t,v in (context or {}).get('hourly',{}).items() if parse_dt(t)<run}
    history.update({t:v for t,v in snapshot['hourly'].items() if parse_dt(t)>=run})
    snapshot.update(hourly=history,reconstruction=True,archive_kind='single_run_gfs',
        issued_for=issue.isoformat().replace('+00:00','Z'),weather_run=run.isoformat().replace('+00:00','Z'))
    if profile=='local': snapshot['location']={'latitude':float(latitude),'longitude':float(longitude)}
    return snapshot
