from datetime import datetime, timezone

import pytest

from app.backend import AppService
from app.methods import _market_to_tariff, epex_version
from app.storage import Store


def test_existing_custom_values_survive_and_preset_is_authoritative(tmp_path):
    app = AppService(store=Store(tmp_path))
    app.store.set_settings({'epex_multiplier': 1.13, 'epex_offset_eur_kwh': .08,
                            'epex_tariff_confirmed': True})
    assert app.get_settings()['epex_tariff_profile'] == 'custom'
    original = app.put_settings({})
    assert original['epex_multiplier'] == 1.13
    assert original['epex_offset_eur_kwh'] == .08
    selected = app.put_settings({'epex_tariff_profile': 'zonneplan_2026',
                                 'epex_multiplier': 99, 'epex_offset_eur_kwh': 99,
                                 'epex_tariff_confirmed': False})
    assert selected['epex_multiplier'] == 1.21
    assert selected['epex_offset_eur_kwh'] == pytest.approx(.1308481)
    assert selected['epex_tariff_confirmed'] is True
    assert epex_version(original) != epex_version(selected)
    reloaded = AppService(store=Store(tmp_path)).get_settings()
    assert reloaded['epex_tariff_profile'] == 'zonneplan_2026'
    assert _market_to_tariff(100, reloaded, datetime(2026, 10, 20, tzinfo=timezone.utc)) == pytest.approx(.2518481)
    custom = app.put_settings({'epex_tariff_profile': 'custom', 'epex_multiplier': 1,
                               'epex_offset_eur_kwh': .03})
    assert _market_to_tariff(-100, custom, datetime(2026, 10, 20, tzinfo=timezone.utc)) == pytest.approx(-.07)


def test_profile_rejects_unknown_supplier_and_conflicting_price_field(tmp_path):
    app = AppService(store=Store(tmp_path))
    for profile in ('unknown', None, [], {}):
        with pytest.raises(ValueError, match='leverancier'):
            app.put_settings({'epex_tariff_profile': profile})
    with pytest.raises(ValueError, match='Inclusief belasting'):
        app.put_settings({'epex_tariff_profile': 'zonneplan_2026', 'price_field': 'tax_excluded'})


def test_profile_dates_use_dutch_quarter_and_reject_other_years(tmp_path):
    app = AppService(store=Store(tmp_path))
    settings = app.put_settings({'epex_tariff_profile': 'zonneplan_2026'})
    for stamp in ('2025-12-31T23:00:00+00:00', '2026-12-31T22:45:00+00:00'):
        assert _market_to_tariff(-100, settings, datetime.fromisoformat(stamp)) == pytest.approx(.0098481)
    for stamp in ('2025-12-31T22:45:00+00:00', '2026-12-31T23:00:00+00:00'):
        with pytest.raises(ValueError, match='2026'):
            _market_to_tariff(100, settings, datetime.fromisoformat(stamp))
    settings['tariff_unit'] = 'EUR/MWh'
    assert _market_to_tariff(100, settings, datetime(2026, 6, 1, tzinfo=timezone.utc)) == pytest.approx(251.8481)


def test_energy_charts_uses_own_components_instead(tmp_path):
    app = AppService(store=Store(tmp_path))
    settings = app.put_settings({'epex_tariff_profile': 'zonneplan_2026',
                                 'price_source': 'energy_charts_nl', 'price_choice': 'bare'})
    assert _market_to_tariff(100, settings, datetime(2027, 6, 1, tzinfo=timezone.utc)) == .1


def test_expired_profile_does_not_start_ml_worker(tmp_path):
    from app.methods import epex_forecast
    app = AppService(store=Store(tmp_path))
    settings = app.put_settings({'epex_tariff_profile': 'zonneplan_2026'})
    with pytest.raises(ValueError, match='2026'):
        epex_forecast([], datetime(2027, 1, 1, tzinfo=timezone.utc), settings,
                      runner=lambda *a, **kw: pytest.fail('expired profile launched ML'))
