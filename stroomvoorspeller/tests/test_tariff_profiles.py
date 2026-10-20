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


@pytest.mark.parametrize('supplier', [
    'anwb', 'budget', 'clean_energy', 'coolblue', 'easyenergy', 'eneco', 'energiedirect',
    'energiek', 'energie_vanons', 'energyzero', 'essent', 'frank', 'groenestroomlokaal',
    'hegg', 'innova', 'nextenergy', 'pure', 'quatt', 'samsam', 'tibber', 'vandebron',
    'vattenfall', 'vrijopnaam', 'zondergas', 'zonneplan',
])
def test_all_suppliers_share_conversion_between_epex_and_energy_charts(tmp_path, supplier):
    from app.energy_prices import all_in_price
    from app.tariff_profiles import SUPPLIER_PRESETS
    app = AppService(store=Store(tmp_path))
    metadata = app.get_settings()
    assert len(metadata['supplier_presets']) == 26  # 25 suppliers + custom
    profile = 'zonneplan_2026' if supplier == 'zonneplan' else supplier + '_2026_08'
    ha = app.put_settings({'epex_tariff_profile': profile})
    market = app.put_settings({'price_source': 'energy_charts_nl', 'price_choice': 'all_in',
                               'supplier': supplier, 'supplier_fee_confirmed': True})
    assert market['supplier_fee_eur_kwh_incl_vat'] == SUPPLIER_PRESETS[supplier]['fee_incl_vat']
    assert market['tariff_valid_from'] == '2026-08-04'
    when = datetime(2026, 10, 20, tzinfo=timezone.utc)
    for price in (-100, 0, 100):
        assert _market_to_tariff(price, ha, when) == pytest.approx(all_in_price(price / 1000, market, when))


def test_snapshot_does_not_backfill_before_peildatum(tmp_path):
    from app.energy_prices import all_in_price, EnergyPriceError
    app = AppService(store=Store(tmp_path))
    ha = app.put_settings({'epex_tariff_profile': 'tibber_2026_08'})
    market = app.put_settings({'price_source': 'energy_charts_nl', 'price_choice': 'all_in',
                               'supplier': 'tibber', 'supplier_fee_confirmed': True})
    before = datetime.fromisoformat('2026-08-03T21:45:00+00:00')
    first = datetime.fromisoformat('2026-08-03T22:00:00+00:00')
    with pytest.raises(ValueError, match='2026-08-04'):
        _market_to_tariff(100, ha, before)
    with pytest.raises(EnergyPriceError, match='niet geldig'):
        all_in_price(.1, market, before)
    assert _market_to_tariff(100, ha, first) == pytest.approx(.2566481)
    assert all_in_price(.1, market, first) == pytest.approx(.2566481)


def test_existing_market_components_not_overwritten_by_new_catalog(tmp_path):
    app = AppService(store=Store(tmp_path))
    app.store.set_settings({'supplier': 'tibber', 'supplier_fee_eur_kwh_incl_vat': .02178,
                            'energy_tax_eur_kwh': .1, 'vat_percent': 21,
                            'tariff_valid_from': '2026-01-01', 'tariff_valid_to': '2026-12-31',
                            'supplier_fee_confirmed': True})
    saved = app.put_settings({})
    assert saved['supplier_fee_eur_kwh_incl_vat'] == .02178
    assert saved['energy_tax_eur_kwh'] == .1
    assert saved['tariff_valid_from'] == '2026-01-01'
    changed = app.put_settings({'supplier': 'budget'})
    assert changed['supplier_fee_eur_kwh_incl_vat'] == .01682
    assert changed['tariff_valid_from'] == '2026-08-04'
    custom = app.put_settings({'supplier': 'custom', 'supplier_fee_eur_kwh_incl_vat': .04,
                               'tariff_valid_from': '2025-01-01'})
    assert custom['supplier_fee_eur_kwh_incl_vat'] == .04
    assert custom['tariff_valid_from'] == '2025-01-01'


def test_table_units_and_precision():
    from app.tariff_profiles import SUPPLIER_PRESETS
    assert SUPPLIER_PRESETS['vattenfall']['fee_incl_vat'] == .025495
    assert SUPPLIER_PRESETS['tibber']['fee_incl_vat'] == .02480
    assert SUPPLIER_PRESETS['energyzero']['fee_incl_vat'] == .02242  # superseded 3.388 ct is not used
    assert SUPPLIER_PRESETS['budget']['fee_incl_vat'] == .01682
