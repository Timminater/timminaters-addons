"""Local tariff proposals with provenance; never fetch forum data at runtime."""
from zoneinfo import ZoneInfo


# First post, Netherlands / private customers: all fees explicitly include 21% VAT.
# This snapshot is not proof of contract validity before its publication date.
SOURCE = "https://gathering.tweakers.net/forum/list_messages/2049754"
AS_OF = "2026-08-04"
# EUR/kWh, not cents. Excludes fixed monthly charges and export arrangements.
FEES = {
    "anwb": ("ANWB Energie", .02000),
    "budget": ("Budget Energie", .01682),
    "clean_energy": ("Clean Energy", .01815),
    "coolblue": ("Coolblue Energie", .02000),
    "easyenergy": ("easyEnergy", .02178),
    "eneco": ("Eneco", .02257),
    "energiedirect": ("Energiedirect", .02050),
    "energiek": ("Energiek", .01800),
    "energie_vanons": ("Energie VanOns", .02900),
    "energyzero": ("EnergyZero", .02242),
    "essent": ("Essent", .02528),
    "frank": ("Frank Energie", .01815),
    "groenestroomlokaal": ("GroeneStroomLokaal", .02242),
    "hegg": ("Hegg Energy", .02242),
    "innova": ("Innova", .02508),
    "nextenergy": ("NextEnergy", .02190),
    "pure": ("Pure Energie", .01999),
    "quatt": ("Quatt", .01815),
    "samsam": ("SamSam", .02242),
    "tibber": ("Tibber", .02480),
    "vandebron": ("Vandebron", .02571),
    "vattenfall": ("Vattenfall", .025495),
    "vrijopnaam": ("Vrijopnaam", .01750),
    "zondergas": ("ZonderGas", .02242),
    "zonneplan": ("Zonneplan", .02000),
}
SUPPLIER_PRESETS = {
    key: {"label": label, "fee_incl_vat": fee, "valid_from": AS_OF,
          "valid_to": "2026-12-31", "source": SOURCE, "as_of": AS_OF,
          "energy_tax_eur_kwh": .09161, "vat_percent": 21}
    for key, (label, fee) in FEES.items()
}
# Preserve the previously published Zonneplan profile and its independent source.

PROFILES = {
    "zonneplan_2026": {
        "label": "Zonneplan — afname incl. belasting en btw (2026)",
        "multiplier": 1.21,
        "offset_eur_kwh": 0.09161 * 1.21 + 0.02,
        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "source": "https://www.zonneplan.nl/energie/dynamisch-energiecontract",
    },
}
for key, preset in SUPPLIER_PRESETS.items():
    if key == "zonneplan":
        continue
    PROFILES[key + "_2026_08"] = {
        **preset, "label": preset["label"] + " — afname incl. belasting/btw (peildatum 04-08-2026)",
        "multiplier": 1.21, "offset_eur_kwh": .09161 * 1.21 + preset["fee_incl_vat"],
    }
PROFILES["zonneplan_2026"]["as_of"] = AS_OF


def apply_profile(settings):
    profile = settings.get("epex_tariff_profile", "custom")
    if not isinstance(profile, str) or profile not in {"custom", *PROFILES}:
        raise ValueError("Kies een bekende Epex-leverancier of Eigen tarief")
    if profile == "custom":
        return
    preset = PROFILES[profile]
    if settings.get("price_source") != "energy_charts_nl" and settings.get("price_field") != "tax_included":
        raise ValueError("Het leveranciersprofiel vereist Inclusief belasting als HA-tariefreeks")
    settings.update(epex_multiplier=preset["multiplier"],
                    epex_offset_eur_kwh=preset["offset_eur_kwh"], epex_tariff_confirmed=True)


def validate_profile_date(settings, start):
    preset = PROFILES.get(settings.get("epex_tariff_profile"))
    if preset:
        day = start.astimezone(ZoneInfo("Europe/Amsterdam")).date().isoformat()
        if not preset["valid_from"] <= day <= preset["valid_to"]:
            raise ValueError(f"Dit tariefprofiel geldt van {preset['valid_from']} t/m {preset['valid_to']}; kies Eigen tarief voor een andere periode")
