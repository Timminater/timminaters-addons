from datetime import datetime, timedelta, timezone
from dataclasses import replace
import math

import pytest

from app.adaptive_validation import AnalogIndex, validated_blend
from app.methods import adaptive_forecast, LOCAL, VERSIONS
from app.model import ForecastRun, ForecastPoint, STEP

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def known_grid(days=28):
    return {NOW-timedelta(days=days)+i*STEP:
            {'price': .2+.03*math.sin(i/96*2*math.pi),
             'end': NOW-timedelta(days=days)+(i+1)*STEP,
             'published_at': NOW-timedelta(days=days)+(i+1)*STEP,
             'available_at': NOW-timedelta(days=days)+(i+1)*STEP}
            for i in range(days*96)}


def test_validation_folds_only_see_ended_available_prices_and_no_current_weather():
    known = known_grid()
    calls = []
    def base(history, issued_at, weather, max_points, price_scale):
        assert weather is None and max_points == 192
        assert all(r['end_utc'] <= issued_at and r['published_at'] <= issued_at for r in history)
        calls.append(issued_at)
        return ForecastRun('base', issued_at, [ForecastPoint(issued_at+i*STEP, issued_at+(i+1)*STEP, .4)
                                             for i in range(max_points)], 'voorlopig', [])
    selected, _, samples = validated_blend(known, NOW, 1000, base, LOCAL)
    assert 48 <= samples <= 168 and len(calls) <= 7
    assert selected[2] > 0  # unbiased analogues beat a deliberately biased baseline
    poisoned = dict(known)
    for stamp in list(poisoned)[-96:]:
        poisoned[stamp] = {**poisoned[stamp], 'published_at': NOW+STEP, 'available_at': NOW+STEP}
    # Labels not available at issue are excluded, including when their interval ended.
    _, _, fewer_samples = validated_blend(poisoned, NOW, 1000, base, LOCAL)
    assert fewer_samples < samples


def test_cached_analogues_match_original_including_negative_prices():
    from app.methods import _analog
    known = known_grid()
    for row in known.values(): row['price'] -= .3
    index = AnalogIndex(known, LOCAL)
    for days in (7,14,28):
        for level in (False,True):
            for hour in (0,9,18):
                target=NOW+timedelta(days=1,hours=hour)
                original={'rows':list(known.items()),'slots':index.slots}
                assert index.price(target,NOW,days,level) == pytest.approx(_analog(original,target,NOW,days,level))


def test_only_24_to_48_hour_points_change_and_band_width_is_preserved(monkeypatch):
    known = known_grid()
    history = [{'start_utc':t,'end_utc':r['end'],'price':r['price'],'published_at':r['published_at']} for t,r in known.items()]
    def base(history, issued_at, weather=None, max_points=672, price_scale=1):
        return ForecastRun('base',issued_at,[ForecastPoint(issued_at+i*STEP,issued_at+(i+1)*STEP,.4,.3,.5) for i in range(max_points)],'voorlopig',[])
    monkeypatch.setattr('app.adaptive_validation.validated_blend',lambda *a:None)
    old=adaptive_forecast(history,NOW,max_points=200,base=base)
    monkeypatch.setattr('app.adaptive_validation.validated_blend',lambda *a:((0,False,0.),None,100))
    new=adaptive_forecast(history,NOW,max_points=200,base=base)
    for before,after in zip(old.points,new.points):
        h=(after.start_utc-NOW).total_seconds()/3600
        if 24 <= h < 48: assert after.price == .4
        else: assert after == before
        assert after.upper-after.lower == pytest.approx(.2)
    assert new.model_version == VERSIONS['local'] == 'adaptive-quarter-2'


def test_cold_start_never_runs_inner_forecasts():
    assert validated_blend(known_grid(5),NOW,1000,lambda *a,**kw:pytest.fail('cold training'),LOCAL) is None


def test_future_target_poison_does_not_change_prediction():
    from app.model import forecast_quarters
    history=[{'start_utc':t,'end_utc':r['end'],'price':r['price'],'published_at':r['published_at']} for t,r in known_grid(16).items()]
    clean=adaptive_forecast(history,NOW,max_points=100,price_scale=1000,base=forecast_quarters)
    poison=history+[{'start_utc':NOW+timedelta(hours=30),'end_utc':NOW+timedelta(hours=30)+STEP,
                    'price':999,'published_at':NOW+timedelta(days=2)}]
    dirty=adaptive_forecast(poison,NOW,max_points=100,price_scale=1000,base=forecast_quarters)
    assert clean.points == dirty.points


def test_tariff_unit_scaling_is_preserved():
    from app.model import forecast_quarters
    history=[{'start_utc':t,'end_utc':r['end'],'price':r['price'],'published_at':r['published_at']} for t,r in known_grid(16).items()]
    small=adaptive_forecast(history,NOW,max_points=100,price_scale=1000,base=forecast_quarters)
    large=adaptive_forecast([{**r,'price':r['price']*1000} for r in history],NOW,max_points=100,price_scale=1,base=forecast_quarters)
    assert [p.price for p in large.points] == pytest.approx([p.price*1000 for p in small.points])


def test_rerun_at_same_issue_prefers_new_version_without_deleting_old_scores(tmp_path):
    from app.storage import Store
    store=Store(tmp_path)
    inputs={'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh','price_field':'tax_included',
            'method':'local','evaluation_mode':'historical'}
    points=[{'start_utc':NOW,'end_utc':NOW+STEP,'price':.2}]
    store.save_forecast('old',NOW,'adaptive-quarter-1','voorlopig',[],inputs,points)
    store.save_forecast('new',NOW,'adaptive-quarter-2','voorlopig',[],inputs,points)
    assert store.latest_forecast(method='local',evaluation_mode='historical')[0]['id']=='new'
    assert store.latest_forecast(method='local',evaluation_mode='historical',model_version='adaptive-quarter-1')[0]['id']=='old'
    selected=store.daily_stored_forecasts('sensor.prices',NOW-timedelta(days=1),NOW,'tax_included','EUR/kWh',
                                         method='local',evaluation_mode='historical')
    assert [r['id'] for r in selected]==['new']
