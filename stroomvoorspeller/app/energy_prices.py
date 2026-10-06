"""Validated Dutch day-ahead market prices and dated all-in calculations."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
API = "https://api.energy-charts.info/price"
ATTRIBUTION = "Energy-Charts.info (Fraunhofer ISE), CC BY 4.0"
SUPPLIERS = {
    "zonneplan": {"label": "Zonneplan", "fee_incl_vat": 0.02, "valid_from": "2026-01-01", "valid_to": "2026-12-31"},
    "tibber": {"label": "Tibber", "fee_incl_vat": None, "valid_from": "2026-01-01", "valid_to": "2026-12-31"},
    "custom": {"label": "Eigen tarief", "fee_incl_vat": None, "valid_from": None, "valid_to": None},
}


class EnergyPriceError(ValueError):
    pass


def fetch_prices(now: datetime, *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    """Fetch today's and tomorrow's NL market curve; timestamps are interval starts."""
    if now.tzinfo is None:
        raise EnergyPriceError("now moet een tijdzone bevatten")
    local_day = now.astimezone(AMSTERDAM).date()
    end_day = local_day + timedelta(days=1)
    history_day = local_day - timedelta(days=35)
    url = API + "?" + urlencode({"bzn": "NL", "start": history_day.isoformat(), "end": end_day.isoformat()})
    try:
        with opener(Request(url, headers={"User-Agent": "Stroomvoorspeller/1.0"}), timeout=20) as response:
            payload = json.load(response)
    except (OSError, ValueError, TimeoutError) as exc:
        raise EnergyPriceError(f"Energy-Charts kon niet worden gelezen: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("unit") != "EUR / MWh" or "CC BY 4.0" not in str(payload.get("license_info", "")):
        raise EnergyPriceError("Onverwachte eenheid of licentie voor marktprijzen")
    stamps, values = payload.get("unix_seconds"), payload.get("price")
    if not isinstance(stamps, list) or not isinstance(values, list) or len(stamps) != len(values) or not stamps:
        raise EnergyPriceError("Ongeldige marktprijsreeks")
    begin = datetime.combine(history_day, time.min, tzinfo=AMSTERDAM).astimezone(UTC)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=AMSTERDAM).astimezone(UTC)
    rows = []
    for stamp, value in zip(stamps, values):
        if value is None:
            continue
        try:
            start = datetime.fromtimestamp(int(stamp), UTC)
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            raise EnergyPriceError("Ongeldig markttijdstip of bedrag") from None
        if start.second or start.minute % 15 or not math.isfinite(price) or not -1000 <= price <= 5000:
            raise EnergyPriceError("Marktreeks bevat een ongeldige kwartierwaarde")
        if begin <= start < end:
            rows.append({"start_utc": start.isoformat(), "market_eur_mwh": price})
    rows.sort(key=lambda row: row["start_utc"])
    starts = [row["start_utc"] for row in rows]
    if len(rows) < 4 or len(starts) != len(set(starts)):
        raise EnergyPriceError("Onvoldoende unieke marktkwartieren")
    if not any(datetime.fromisoformat(item["start_utc"]).astimezone(AMSTERDAM).date() == local_day
               for item in rows):
        raise EnergyPriceError("Geen marktprijs voor vandaag beschikbaar")
    return {"source": API, "attribution": ATTRIBUTION, "license_info": payload["license_info"],
            "fetched_at": now.astimezone(UTC).isoformat(), "prices": rows}


def all_in_price(market_eur_kwh: float, settings: Mapping[str, Any], interval_start: datetime) -> float:
    """Apply explicitly dated components, failing closed outside their validity."""
    try:
        start = interval_start.astimezone(AMSTERDAM).date()
        valid_from = date.fromisoformat(str(settings["tariff_valid_from"]))
        valid_to = date.fromisoformat(str(settings["tariff_valid_to"]))
        tax = float(settings["energy_tax_eur_kwh"])
        vat = float(settings["vat_percent"])
        fee = float(settings["supplier_fee_eur_kwh_incl_vat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EnergyPriceError("Vul alle all-in componenten en geldigheidsdatums in") from exc
    if valid_to < valid_from or not valid_from <= start <= valid_to:
        raise EnergyPriceError(f"All-in componenten zijn niet geldig op {start.isoformat()}")
    if not all(math.isfinite(v) for v in (tax, vat, fee)) or tax < 0 or not 0 <= vat <= 100 or fee < 0:
        raise EnergyPriceError("Ongeldige all-in componenten")
    return (market_eur_kwh + tax) * (1 + vat / 100) + fee


def price_rows(market: Mapping[str, Any], settings: Mapping[str, Any], identity: str,
               now: datetime) -> list[dict[str, Any]]:
    """Convert the published market curve to source-scoped known quarter rows."""
    fetched_at = datetime.fromisoformat(str(market["fetched_at"]).replace("Z", "+00:00"))
    rows = []
    for row in market.get("prices", []):
        start = datetime.fromisoformat(str(row["start_utc"]).replace("Z", "+00:00")).astimezone(UTC)
        end = start + timedelta(minutes=15)
        value = float(row["market_eur_mwh"]) / 1000
        if settings.get("price_choice") == "all_in":
            try:
                value = all_in_price(value, settings, start)
            except EnergyPriceError:
                continue
        rows.append({"entity_id": identity, "start_utc": start, "end_utc": end,
                     "price": value, "unit": "EUR/kWh", "source": "energy_charts_nl",
                     "observed_at": now, "published_at": fetched_at, "quality": "published"})
    return rows
