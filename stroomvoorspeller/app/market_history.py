"""Optional Dutch market-price history for a provisional Zonneplan model prior.

These derived prices are model inputs only. They must never enter the measured
Home Assistant tariff archive or the actuals used for backtesting.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta, timezone
from statistics import mean
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
SOURCE = "energy-charts-market-derived"
API = "https://api.energy-charts.info/price"
REASON = "historische marktprijzen van Energy-Charts zijn naar Zonneplan-all-in herleid; geen gemeten Zonneplan-tarieven"


class MarketHistoryError(ValueError):
    pass


def _oldest_quarter(now: datetime) -> datetime:
    day = now.astimezone(AMSTERDAM).date() - timedelta(days=35)
    return datetime.combine(day, time.min, tzinfo=AMSTERDAM).astimezone(UTC)


def fetch_market_history(now: datetime, *, opener=urlopen) -> dict[str, Any]:
    """Read 35 past days of NL day-ahead quarters from Fraunhofer Energy-Charts."""
    start = _oldest_quarter(now).astimezone(AMSTERDAM).date().isoformat()
    end = now.astimezone(AMSTERDAM).date().isoformat()
    url = API + "?" + urlencode({"bzn": "NL", "start": start, "end": end})
    try:
        with opener(Request(url, headers={"User-Agent": "Stroomvoorspeller/0.1.3"}), timeout=20) as response:
            payload = json.load(response)
    except (OSError, ValueError, TimeoutError) as exc:
        raise MarketHistoryError(f"Energy-Charts kon niet worden gelezen: {exc}") from exc
    if payload.get("unit") != "EUR / MWh" or "CC BY 4.0" not in payload.get("license_info", ""):
        raise MarketHistoryError("Onverwachte eenheid of licentie voor historische marktprijzen")
    stamps, prices = payload.get("unix_seconds"), payload.get("price")
    if not isinstance(stamps, list) or not isinstance(prices, list) or len(stamps) != len(prices):
        raise MarketHistoryError("Ongeldige historische marktprijsreeks")
    rows = []
    for stamp, value in zip(stamps, prices):
        if value is None:
            continue
        try:
            at = datetime.fromtimestamp(int(stamp), UTC)
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            raise MarketHistoryError("Ongeldig markttijdstip of bedrag") from None
        if at.second or at.minute % 15 or not math.isfinite(price) or not -1000 <= price <= 5000:
            raise MarketHistoryError("Marktreeks bevat ongeldige kwartierwaarde")
        if _oldest_quarter(now) <= at < now:
            rows.append({"start_utc": at.isoformat(), "market_eur_mwh": price})
    if len(rows) < 96 or len({row["start_utc"] for row in rows}) != len(rows):
        raise MarketHistoryError("Onvoldoende unieke historische marktkwartieren")
    return {"source": API, "license_info": payload["license_info"],
            "fetched_at": now.isoformat(), "prices": rows}


def derive_zonneplan_history(
    known: Iterable[Mapping[str, Any]], market: Mapping[str, Any], now: datetime
) -> tuple[list[dict[str, Any]], dict[str, float | int]]:
    """Fit and validate a market-to-tariff relation on overlapping true prices."""
    market_by_start = {datetime.fromisoformat(row["start_utc"]).astimezone(UTC):
                       float(row["market_eur_mwh"]) / 1000 for row in market.get("prices", [])}
    actual = {}
    for row in known:
        if not str(row.get("source", "")).startswith("ha_") or row.get("unit") != "EUR/kWh":
            continue
        start = datetime.fromisoformat(str(row["start_utc"]).replace("Z", "+00:00")).astimezone(UTC)
        if start < now and start.astimezone(AMSTERDAM).year == now.astimezone(AMSTERDAM).year:
            actual[start] = float(row["price"])
    overlap = [(market_by_start[start], price) for start, price in actual.items() if start in market_by_start]
    if len(overlap) < 96:
        raise MarketHistoryError("Minstens 96 overlappende Zonneplan-kwartieren nodig")
    xs, ys = zip(*overlap)
    if max(xs) - min(xs) < .04:
        raise MarketHistoryError("Te weinig prijsvariatie om de tariefomrekening te toetsen")
    xbar, ybar = mean(xs), mean(ys)
    denominator = sum((x - xbar) ** 2 for x in xs)
    slope = sum((x - xbar) * (y - ybar) for x, y in overlap) / denominator
    offset = ybar - slope * xbar
    residuals = [abs(y - (slope * x + offset)) for x, y in overlap]
    if not 1.15 <= slope <= 1.27 or not -.05 <= offset <= .4 or max(residuals) > .00015:
        raise MarketHistoryError("Marktprijzen sluiten niet nauwkeurig aan op het gekozen Zonneplan-tarief")
    earliest = min(actual)
    oldest = _oldest_quarter(now)
    year = now.astimezone(AMSTERDAM).year
    publication = market.get("fetched_at") or now.isoformat()
    rows = []
    for start, price in market_by_start.items():
        if not oldest <= start < earliest or start.astimezone(AMSTERDAM).year != year:
            continue
        rows.append({"start_utc": start.isoformat(), "end_utc": (start + timedelta(minutes=15)).isoformat(),
                     "price": slope * price + offset, "published_at": publication, "source": SOURCE})
    rows.sort(key=lambda row: row["start_utc"])
    return rows, {"overlap": len(overlap), "max_residual": max(residuals),
                  "slope": slope, "offset": offset, "derived_quarters": len(rows)}
