"""Read-only client for the Home Assistant Supervisor proxy."""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

UTC = timezone.utc
SUPERVISOR_BASE = "http://supervisor/core/api"
PRICE_UNITS = {"EUR/kWh", "€/kWh", "EUR/MWh", "€/MWh", "ct/kWh", "c€/kWh"}


def _canonical_unit(value: str | None) -> str | None:
    return {"€/kWh": "EUR/kWh", "€/MWh": "EUR/MWh", "c€/kWh": "ct/kWh"}.get(value, value)


def _convert_price(value: float, source_unit: str, target_unit: str) -> float:
    source_unit, target_unit = _canonical_unit(source_unit), _canonical_unit(target_unit)
    to_eur_kwh = {"EUR/kWh": 1.0, "EUR/MWh": 0.001, "ct/kWh": 0.01}
    if source_unit not in to_eur_kwh or target_unit not in to_eur_kwh:
        raise HAError("Prijsunit ontbreekt of wordt niet ondersteund")
    converted = value * to_eur_kwh[source_unit] / to_eur_kwh[target_unit]
    if not math.isfinite(converted) or abs(converted) > 100_000:
        raise HAError("Omgerekende tariefprijs buiten bereik")
    return converted


class HAError(RuntimeError):
    pass


def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise HAError(f"Ongeldig tijdstip: {value!r}") from exc
    else:
        raise HAError("Tijdstip ontbreekt")
    if dt.tzinfo is None:
        raise HAError("Tijdstip zonder tijdzone geweigerd")
    return dt.astimezone(UTC)


def _quarter_floor(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def _numeric(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HAError(f"{label} is geen getal") from exc
    if not math.isfinite(result) or abs(result) > 100_000:
        raise HAError(f"{label} is buiten het geldige bereik")
    return result


class HAClient:
    """HTTP client which exposes GET operations only; no write method exists."""
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = 20):
        self.base_url = (base_url or os.environ.get("HA_SUPERVISOR_API", SUPERVISOR_BASE)).rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.timeout = timeout

    def get(self, path: str, query: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API path must be relative to the HA API")
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise HAError("HA API response exceeded 8 MiB limit")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HAError(f"Home Assistant gaf HTTP {exc.code} voor {path}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise HAError(f"Home Assistant API niet beschikbaar: {exc}") from exc

    def states(self) -> list[dict[str, Any]]:
        result = self.get("/states")
        if not isinstance(result, list):
            raise HAError("HA states-response heeft een onverwacht formaat")
        return [s for s in result if isinstance(s, dict) and isinstance(s.get("entity_id"), str)]

    def entities(self) -> list[dict[str, Any]]:
        result = []
        for state in self.states():
            entity_id = state["entity_id"]
            if not entity_id.startswith("sensor."):
                continue
            attrs = state.get("attributes") or {}
            result.append({"entity_id": entity_id, "state": state.get("state"),
                           "unit": attrs.get("unit_of_measurement"),
                           "friendly_name": attrs.get("friendly_name", entity_id), "domain": "sensor"})
        return result

    def state(self, entity_id: str) -> dict[str, Any] | None:
        for state in self.states():
            if state["entity_id"] == entity_id:
                return state
        return None

    def config(self) -> dict[str, Any]:
        result = self.get("/config")
        return result if isinstance(result, dict) else {}

    def history(self, entity_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Fetch HA history in <=24h chunks, preserving timezone offsets."""
        if end <= start:
            return []
        cursor = start.astimezone(UTC)
        all_states: list[dict[str, Any]] = []
        while cursor < end:
            chunk_end = min(cursor + timedelta(hours=24), end.astimezone(UTC))
            path = "/history/period/" + urllib.parse.quote(cursor.isoformat().replace("+00:00", "Z"), safe="")
            payload = self.get(path, {"filter_entity_id": entity_id,
                                      "end_time": chunk_end.isoformat().replace("+00:00", "Z"),
                                      "significant_changes_only": "0", "minimal_response": "0"})
            if isinstance(payload, list):
                for series in payload:
                    if isinstance(series, list):
                        all_states.extend(s for s in series if isinstance(s, dict))
            cursor = chunk_end
        return all_states


def parse_forecast(state: Mapping[str, Any], entity_id: str, observed_at: datetime,
                   tariff_unit: str | None = None, price_field: str = "tax_included") -> list[dict[str, Any]]:
    """Parse actual published quarter prices from a sensor's forecast attribute.

    Key aliases accommodate common HA tariff integrations, while values and
    timestamps remain strict. Prices are stored in the entity's declared unit.
    """
    attrs = state.get("attributes") or {}
    if price_field not in {"tax_included", "tax_excluded"}:
        raise HAError("price_field moet tax_included of tax_excluded zijn")
    declared_unit = attrs.get("unit_of_measurement")
    unit = _canonical_unit(declared_unit or tariff_unit)
    if declared_unit and tariff_unit and _canonical_unit(declared_unit) != _canonical_unit(tariff_unit):
        raise HAError("Gekozen tariefunit komt niet overeen met de HA-entiteit")
    if unit not in PRICE_UNITS:
        raise HAError(f"Tariefentiteit {entity_id} mist een bruikbare unit; kies de unit in Instellingen")
    raw = attrs.get("forecast")
    if not isinstance(raw, list) or not raw:
        raise HAError(f"Tariefentiteit {entity_id} mist een bruikbare forecast-lijst")
    observed = parse_dt(observed_at)
    rows: dict[datetime, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        start_raw = next((item[k] for k in ("start", "datetime", "time", "period_start", "start_time", "start_date") if item.get(k) is not None), None)
        end_keys = ("end", "period_end", "end_time", "end_date")
        end_raw = next((item[k] for k in end_keys if item.get(k) is not None), None)
        nested_key = "price_tax_included" if price_field == "tax_included" else "price_tax_excluded"
        nested_amount = item.get(nested_key)
        if isinstance(nested_amount, Mapping) and nested_amount.get("amount") is not None:
            # Zonneplan HA integration README decodes this field as
            # amount / 10_000_000 EUR/kWh (example template on its README).
            # https://github.com/fsaris/home-assistant-zonneplan-one/blob/main/README.md?plain=1
            # The source sensor may omit unit_of_measurement; target unit selection
            # is still explicit in App settings and this schema adapter is recorded.
            try: amount = float(nested_amount["amount"])
            except (TypeError, ValueError) as exc: raise HAError("Zonneplan amount is geen getal") from exc
            if not math.isfinite(amount) or abs(amount) > 1_000_000_000_000:
                raise HAError("Zonneplan amount valt buiten het geldige bereik")
            price_raw = amount / 10_000_000
            source_unit = "EUR/kWh"
        else:
            price_raw = next((item[k] for k in ("price", "value", "tariff", "electricity_price", "total_price") if item.get(k) is not None), None)
            source_unit = unit
        if start_raw is None or price_raw is None:
            continue
        start = parse_dt(start_raw)
        if start.minute % 15 or start.second or start.microsecond:
            raise HAError("Forecast bevat een starttijd buiten kwartiergrenzen")
        price = _convert_price(_numeric(price_raw, "forecast-prijs"), source_unit, unit)
        end = parse_dt(end_raw) if end_raw is not None else start + timedelta(minutes=15)
        if end - start != timedelta(minutes=15):
            raise HAError("Forecast bevat een interval dat geen kwartier is")
        row = {"entity_id": entity_id, "start_utc": start, "end_utc": end, "price": price,
               "unit": unit, "source": "ha_forecast", "observed_at": observed,
               "published_at": observed, "quality": "valid"}
        previous = rows.get(start)
        if previous and (previous["price"] != price or previous["end_utc"] != end):
            raise HAError("Forecast bevat dubbele kwartieren met verschillende waarden")
        rows[start] = row
    if not rows:
        raise HAError(f"Forecast van {entity_id} bevat geen geldige kwartierprijzen")
    ordered = [rows[k] for k in sorted(rows)]
    return ordered


def parse_history(entity_id: str, states: list[Mapping[str, Any]], observed_at: datetime,
                  tariff_unit: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only actual quarter-aligned state changes as quarter observations.

    The REST history endpoint does not identify a value as an hourly average.
    Off-boundary changes are therefore not relabelled as hourly averages and
    are ignored here. A separate statistics adapter can populate hourly_history
    when the source statistic metadata proves the aggregation period.
    """
    quarters: dict[datetime, dict[str, Any]] = {}
    hours: dict[datetime, dict[str, Any]] = {}
    observed = parse_dt(observed_at)
    for state in states:
        attrs = state.get("attributes") or {}
        unit = _canonical_unit(attrs.get("unit_of_measurement") or tariff_unit)
        if unit not in PRICE_UNITS:
            continue
        raw = state.get("state")
        try:
            price = _numeric(raw, "historische prijs")
        except HAError:
            continue
        candidates = []
        for field in ("last_changed", "last_updated"):
            if state.get(field):
                try: candidates.append(parse_dt(state[field]))
                except HAError: pass
        # HA may update forecast attributes without changing the state value.
        # Use an exact quarter boundary from either timestamp; never round an
        # arbitrary observation into an apparently measured quarter.
        stamp = next((ts for ts in candidates if ts.minute % 15 == 0 and ts.second == 0 and ts.microsecond == 0), None)
        if stamp is not None:
            row = {"entity_id": entity_id, "start_utc": stamp, "end_utc": stamp + timedelta(minutes=15),
                   "price": price, "unit": unit, "source": "ha_history", "observed_at": observed,
                   "published_at": stamp, "quality": "valid"}
            quarters[stamp] = row
        else:
            continue
    return [quarters[k] for k in sorted(quarters)], [hours[k] for k in sorted(hours)]


def collect_price_data(client: HAClient, entity_id: str, now: datetime,
                       history_days: int = 35, tariff_unit: str | None = None,
                       price_field: str = "tax_included", history_start: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], datetime]:
    """Read entity forecast plus bounded history; makes no HA write calls."""
    now = parse_dt(now)
    state = client.state(entity_id)
    if state is None:
        raise HAError(f"Tariefentiteit {entity_id} bestaat niet of is niet beschikbaar")
    forecast_rows = parse_forecast(state, entity_id, now, tariff_unit, price_field)
    # Forecast publications for past intervals represent known tariff prices;
    # current/future forecast rows remain known inputs with their observed time.
    history = client.history(entity_id, history_start or (now - timedelta(days=history_days)), now + timedelta(seconds=1))
    history_quarters, history_hours = parse_history(entity_id, history, now, tariff_unit or (state.get("attributes") or {}).get("unit_of_measurement"))
    by_start = {r["start_utc"]: r for r in history_quarters}
    by_start.update({r["start_utc"]: r for r in forecast_rows})
    quarters = [by_start[t] for t in sorted(by_start)]
    return quarters, history_hours, now
