from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs,urlsplit
import json
import pytest

from app.weather_cache import cached_json,LIVE_TTL
from app.weather_regions import locations
from app.weather import fetch_regional_weather,WeatherError,model_weather
from app.timestral_weather import weather_covariates,validate_covariates
from app.methods import timestral_forecast,timestral_version,epex_version
from app.timestral_models import model_spec
from app.backend import AppService
from app.storage import Store,utc_iso

UTC=timezone.utc
NOW=datetime(2026,10,22,12,tzinfo=UTC)
URL='https://api.open-meteo.com/v1/forecast?latitude=52,53&longitude=5,6&start_date=2026-10-22&end_date=2026-10-29&hourly=temperature_2m'

class Response:
    def __init__(self,data):self.raw=json.dumps(data).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return self.raw[:n]


def test_shared_cache_survives_new_calls_and_rolling_date_queries(tmp_path):
    calls=[];now=[100000.]
    def opener(req,**kwargs):calls.append(req.full_url);return Response({'hourly':{'time':[]}})
    first=cached_json(URL,tmp_path,opener=opener,clock=lambda:now[0])
    now[0]+=LIVE_TTL-1
    assert cached_json(URL.replace('2026-10-29','2026-10-30'),tmp_path,opener=opener,clock=lambda:now[0])==first
    assert len(calls)==1
    now[0]+=1;cached_json(URL,tmp_path,opener=opener,clock=lambda:now[0]);assert len(calls)==2


def test_concurrent_consumers_reserve_only_one_network_request(tmp_path):
    calls=[]
    def opener(*args,**kwargs):calls.append(1);return Response({'ok':True})
    with ThreadPoolExecutor(max_workers=6) as pool:
        values=list(pool.map(lambda _:cached_json(URL,tmp_path,opener=opener,clock=lambda:100000.),range(6)))
    assert len(calls)==1 and all(v==values[0] for v in values)


def test_429_retry_after_blocks_other_models_profiles_and_restart(tmp_path):
    calls=[];headers=Message();headers['Retry-After']='86400'
    def fail(*args,**kwargs):calls.append(1);raise HTTPError(URL,429,'rate limit',headers,None)
    with pytest.raises(ValueError,match='gepauzeerd'):cached_json(URL,tmp_path,opener=fail,clock=lambda:100000.)
    with pytest.raises(ValueError,match='afkoelperiode'):cached_json(URL+'&another=profile',tmp_path,opener=fail,clock=lambda:100001.)
    state=json.loads((tmp_path/'weather-cache/budget.json').read_text())
    assert len(calls)==1 and state['count']==1 and state['blocked_until']==186400.


def test_failed_requests_are_negative_cached_and_budgeted(tmp_path):
    calls=[]
    def fail(*args,**kwargs):calls.append(1);raise TimeoutError()
    for at in (100000.,100010.):
        with pytest.raises(ValueError):cached_json(URL,tmp_path,opener=fail,clock=lambda:at)
    assert len(calls)==1
    state=tmp_path/'weather-cache/budget.json';state.write_text(json.dumps({'day':'1970-01-02','count':48}))
    with pytest.raises(ValueError,match='budget'):cached_json(URL+'&new=1',tmp_path,opener=fail,clock=lambda:100010.)
    assert len(calls)==1


def test_historical_cache_keeps_ranges_separate_and_weights_request_cost(tmp_path):
    calls=[]
    def opener(*args,**kwargs):calls.append(1);return Response({'ok':True})
    url=URL.replace('api.open-meteo','historical-forecast-api.open-meteo')
    cached_json(url,tmp_path,opener=opener,clock=lambda:100000.)
    cached_json(url,tmp_path,opener=opener,clock=lambda:100000.+86400)
    cached_json(url.replace('2026-10-29','2026-10-30'),tmp_path,opener=opener,clock=lambda:100000.+86400)
    assert len(calls)==2


def payload(value):
    stamps=[NOW+timedelta(hours=i) for i in range(-72,8*24)]
    return {'hourly_units':{'shortwave_radiation':'W/m²','wind_speed_10m':'m/s','temperature_2m':'°C'},
        'hourly':{'time':[t.strftime('%Y-%m-%dT%H:%M') for t in stamps],
                  'shortwave_radiation':[value]*len(stamps),'wind_speed_10m':[value]*len(stamps),'temperature_2m':[value]*len(stamps)}}


def test_regional_batch_averages_every_location_without_fake_points(monkeypatch,tmp_path):
    urls=[]
    def batch(url,root):urls.append(url);return [payload(v) for v in (1,3,5,7)],NOW.timestamp()
    monkeypatch.setattr('app.weather_cache.cached_json',batch)
    weather=fetch_regional_weather('nl',None,None,NOW,tmp_path)
    query=parse_qs(urlsplit(urls[0]).query)
    assert len(query['latitude'][0].split(','))==4 and query['past_days']==['35']
    assert len(weather['locations'])==4 and weather['hourly'][utc_iso(NOW)]['wind_ms']==4
    assert min(model_weather(weather)['hourly'])==utc_iso(NOW)
    monkeypatch.setattr('app.weather_cache.cached_json',lambda *a:([payload(1)],NOW.timestamp()))
    with pytest.raises(WeatherError,match='locaties'):fetch_regional_weather('nl',None,None,NOW,tmp_path)


def test_grids_have_real_nl_south_and_optional_european_points():
    assert len(locations('nl'))==4 and min(p[1] for p in locations('nl'))>=51
    assert len(locations('europe'))==10
    assert locations('local',52,5)==[('Lokaal',52.,5.)]
    with pytest.raises(ValueError):locations('local')


def snapshot():
    return {'source':'open_meteo','profile':'nl','observed_at':utc_iso(NOW),
        'hourly':{utc_iso(NOW+timedelta(hours=i)):{'solar_wm2':i+100,'wind_ms':5,'temp_c':12} for i in range(-72,8*24)}}


def test_timestral_covariates_are_aligned_and_reject_missing_future_or_stale_vintage():
    s=snapshot();rows=weather_covariates(s,NOW-timedelta(days=3),288,8,NOW,{'weather_region':'nl'})
    assert len(rows)==3 and len(rows[0])==296 and rows[0][0:4]==[28]*4
    assert rows[0][288:292]==[100]*4
    s['hourly'].pop(utc_iso(NOW))
    with pytest.raises(ValueError,match='volledige'):weather_covariates(s,NOW-timedelta(days=3),288,8,NOW,{'weather_region':'nl'})
    with pytest.raises(ValueError,match='historische'):weather_covariates(snapshot(),NOW-timedelta(days=4),288,8,NOW-timedelta(days=1),{'weather_region':'nl'})
    with pytest.raises(ValueError,match='weergebied'):weather_covariates(snapshot(),NOW-timedelta(days=3),288,8,NOW,{'weather_region':'europe'})


def test_covariate_masks_preserve_missing_context_and_validate_worker_payload():
    s=snapshot();s['hourly'].pop(utc_iso(NOW-timedelta(days=3)))
    rows=weather_covariates(s,NOW-timedelta(days=3),288,8,NOW,{'weather_region':'nl'})
    assert rows[0][:4]==[None]*4
    from app.local_worker import validate_timestral
    validate_timestral({'context':[.2]*288,'prediction_length':8,'weather':rows})
    rows[0][-1]=float('nan')
    with pytest.raises(ValueError,match='weerwaarden'):validate_timestral({'context':[.2]*288,'prediction_length':8,'weather':rows})


def test_timestral_adapter_passes_numeric_weather_with_separate_identity():
    s=snapshot();settings={'weather_region':'nl','timestral_weather':True,'timestral_model':'28M','_weather':s}
    history=[{'start_utc':NOW-timedelta(minutes=15*i),'end_utc':NOW-timedelta(minutes=15*(i-1)),
              'price':.2,'published_at':NOW-timedelta(minutes=15*(i-1))} for i in reversed(range(1,289))]
    sent=[];spec=model_spec('28M')
    def runner(method,request,*args,**kwargs):
        sent.append(request)
        return {'model':spec['repo'],'revision':spec['revision'],'weather':True,'median':[.2]*8,'lower':[.1]*8,'upper':[.3]*8}
    run=timestral_forecast(history,NOW,settings,8,runner=runner)
    assert ':weather-1-' in run.model_version and len(sent[0]['weather'])==3
    assert timestral_version(settings)!=timestral_version({**settings,'timestral_weather':False})
    assert epex_version({'weather_region':'nl'})!=epex_version({'weather_region':'europe'})


def test_settings_and_as_of_snapshots_preserve_real_weather_vintages(tmp_path):
    app=AppService(store=Store(tmp_path));assert app.get_settings()['weather_region']=='nl'
    assert app.get_settings()['timestral_weather'] is False
    app.put_settings({'weather_region':'europe','timestral_weather':True,'forecast_methods':{'local':True,'v4':False,'epex':False,'timestral':True}})
    with pytest.raises(ValueError):app.put_settings({'weather_source':'ha'})
    with pytest.raises(ValueError):app.put_settings({'weather_region':'local'})
    app.store.save_snapshot(NOW-timedelta(hours=1),'weather',{'version':'then'})
    app.store.save_snapshot(NOW+timedelta(hours=1),'weather',{'version':'future'})
    assert app.store.latest_snapshot('weather',as_of=NOW)['payload']['version']=='then'


@pytest.mark.parametrize('enabled,with_weather,expected',[(False,False,False),(True,False,False),(True,True,True)])
def test_price_only_timestral_and_disabled_methods_never_fetch_weather(tmp_path,monkeypatch,enabled,with_weather,expected):
    app=AppService(store=Store(tmp_path),clock=lambda:NOW)
    app.put_settings({'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh',
        'forecast_methods':{'local':False,'v4':False,'epex':False,'timestral':enabled},
        'active_method':'timestral','timestral_weather':with_weather})
    rows=[{'start_utc':utc_iso(NOW-timedelta(minutes=15*i)),
           'end_utc':utc_iso(NOW-timedelta(minutes=15*(i-1))),
           'published_at':utc_iso(NOW-timedelta(minutes=15*(i-1))),
           'observed_at':utc_iso(NOW),'price':.2,'unit':'EUR/kWh','quality':'valid'} for i in reversed(range(1,289))]
    monkeypatch.setattr('app.backend.collect_price_data',lambda *a,**kw:(rows,[],NOW))
    calls=[]
    def fetch(*args,**kwargs):calls.append(1);return snapshot()
    monkeypatch.setattr(app,'_weather_snapshot',fetch)
    monkeypatch.setattr(app,'_run_methods',lambda *a,**kw:{'ok':True})
    assert app.refresh(force_model=True)['ok']
    assert bool(calls)==expected


def test_historical_timestral_does_not_use_a_current_weather_snapshot(tmp_path,monkeypatch):
    from app.comparison import start_history_test
    app=AppService(store=Store(tmp_path),clock=lambda:NOW)
    app.put_settings({'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh','timestral_weather':True,
        'forecast_methods':{'local':False,'v4':False,'epex':False,'timestral':True},'active_method':'timestral'})
    archive=app._archive_entity('sensor.prices',app.get_settings())
    app.store.upsert_quarters([{'entity_id':archive,'start_utc':NOW-timedelta(minutes=15*i),
        'end_utc':NOW-timedelta(minutes=15*(i-1)),'published_at':NOW,'observed_at':NOW,
        'price':.2,'unit':'EUR/kWh','quality':'valid'} for i in reversed(range(1,22*96+1))])
    app.store.save_snapshot(NOW,'weather',snapshot())
    def must_not_infer(*args,**kwargs):raise AssertionError('No Timestral process may start without its historical weather')
    monkeypatch.setattr('app.local_runtime.run_local',must_not_infer)
    start_history_test(app,168);app._history_thread.join(timeout=10)
    assert app._history_job['state']=='completed_with_skips'
    assert app._history_job['skipped'] and not app._history_job['errors']
    assert all('historische uitgifte' in e['message'] for e in app._history_job['skipped'])
    assert app.store.latest_forecast(evaluation_mode='historical')[0] is None


def test_production_profile_switch_uses_shared_cache_without_hour_long_empty_weather(tmp_path,monkeypatch):
    from app.weather_cache import cached_json as request
    at=[NOW];calls=[]
    def opener(req,**kwargs):
        query=parse_qs(urlsplit(req.full_url).query)
        n=len(query['latitude'][0].split(','));calls.append(n)
        return Response([payload(i+1) for i in range(n)])
    monkeypatch.setattr('app.weather_cache.cached_json',lambda url,root:request(url,root,opener=opener,clock=lambda:at[0].timestamp()))
    app=AppService(store=Store(tmp_path),clock=lambda:at[0])
    app.put_settings({'timestral_weather':True,'forecast_methods':{'local':False,'v4':False,'epex':False,'timestral':True},'active_method':'timestral'})
    for i,profile in enumerate(('nl','europe','local','nl','europe','local')):
        at[0]=NOW+timedelta(minutes=5*i)
        app.put_settings({'weather_region':profile,'latitude':52.,'longitude':5.})
        settings=app.get_settings();s=app._weather_snapshot(settings,at[0],force=True)
        assert s is not None and s['profile']==profile
        weather_covariates(s,at[0].replace(minute=0)-timedelta(days=2),192,4,at[0],settings)
        app.store.save_snapshot(at[0],'weather',s)
    assert calls==[4,10,1]  # Returning to any profile reuses the 3-hour cache.
    assert json.loads((tmp_path/'weather-cache/budget.json').read_text())['count']==3


def test_live_timestral_reports_the_weather_failure_instead_of_wrong_source_advice(tmp_path,monkeypatch):
    app=AppService(store=Store(tmp_path),clock=lambda:NOW)
    app.put_settings({'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh','timestral_weather':True,
        'forecast_methods':{'local':False,'v4':False,'epex':False,'timestral':True},'active_method':'timestral'})
    rows=[{'start_utc':utc_iso(NOW-timedelta(minutes=15*i)),'end_utc':utc_iso(NOW-timedelta(minutes=15*(i-1))),
           'price':.2,'published_at':utc_iso(NOW),'observed_at':utc_iso(NOW),'unit':'EUR/kWh','quality':'valid'} for i in reversed(range(1,289))]
    monkeypatch.setattr('app.backend.collect_price_data',lambda *a,**kw:(rows,[],NOW))
    def failure(*a,**kw):raise ValueError('Open-Meteo in afkoelperiode na HTTP 429')
    monkeypatch.setattr(app,'_weather_snapshot',failure)
    app.refresh(force_model=True)
    error=app.store.source_statuses()['method:timestral']['error']
    assert 'Geen actuele Open-Meteo-weerdata' in error and 'HTTP 429' in error
    assert 'kies Open-Meteo' not in error
