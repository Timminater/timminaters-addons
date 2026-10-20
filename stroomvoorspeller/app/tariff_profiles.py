"""Verified local tariff presets. No runtime supplier or Enever API calls."""
from zoneinfo import ZoneInfo

PROFILES = {
    "zonneplan_2026": {
        "label": "Zonneplan — afname incl. belasting en btw (2026)",
        "multiplier": 1.21,
        "offset_eur_kwh": 0.09161 * 1.21 + 0.02,
        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "source": "https://www.zonneplan.nl/energie/dynamisch-energiecontract",
    },
}


def apply_profile(settings):
    profile = settings.get("epex_tariff_profile", "custom")
    if not isinstance(profile, str) or profile not in {"custom", *PROFILES}:
        raise ValueError("Kies een bekende Epex-leverancier of Eigen tarief")
    if profile == "custom":
        return
    preset = PROFILES[profile]
    if settings.get("price_source") != "energy_charts_nl" and settings.get("price_field") != "tax_included":
        raise ValueError("Het Zonneplan-profiel vereist Inclusief belasting als HA-tariefreeks")
    settings.update(epex_multiplier=preset["multiplier"],
                    epex_offset_eur_kwh=preset["offset_eur_kwh"], epex_tariff_confirmed=True)


def validate_profile_date(settings, start):
    preset = PROFILES.get(settings.get("epex_tariff_profile"))
    if preset:
        day = start.astimezone(ZoneInfo("Europe/Amsterdam")).date().isoformat()
        if not preset["valid_from"] <= day <= preset["valid_to"]:
            raise ValueError("Het Zonneplan-tariefprofiel geldt alleen voor 2026; kies Eigen tarief voor een andere periode")
