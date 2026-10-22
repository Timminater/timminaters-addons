from datetime import datetime,timedelta,timezone
from urllib.parse import parse_qs,urlsplit
import json
import pytest

from app.weather import fetch_archived_weather,fetch_weather_context,_archive_json
from app.weather_cache import cached_json,WeatherMinuteBudget
from app.timestral_weather import weather_covariates
from app.storage import Store,utc_iso
from app.backend import AppService
from app.comparison import start_history_test,comparison,WEATHER_ARCHIVE_VERSION
from app.model import ForecastRun,ForecastPoint
from app.methods import timestral_forecast
from app.timestral_models import model_spec

NOW=datetime(2026,10,22,12,tzinfo=timezone.utc)
ISSUE=NOW-timedelta(days=7)

class Response:
    def __init__(self,data):self.raw=json.dumps(data).encode()
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,n):return self.raw[:n]


def archive_response(url):
    q=parse_qs(urlsplit(url).query);is_run='run' in q
    if is_run:
        start=datetime.fromisoformat(q['run'][0]).replace(tzinfo=timezone.utc)
        end=start+timedelta(days=10)
    else:
        start=datetime.fromisoformat(q['start_date'][0]).replace(tzinfo=timezone.utc)
        end=datetime.fromisoformat(q['end_date'][0]).replace(tzinfo=timezone.utc)+timedelta(days=1)
    stamps=[(start+timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(int((end-start).total_seconds()/3600))]
    v=200 if is_run else 50
    one={'hourly_units':{'shortwave_radiation':'W/m²','wind_speed_10m':'m/s','temperature_2m':'°C'},
         'hourly':{'time':stamps,'shortwave_radiation':[v]*len(stamps),'wind_speed_10m':[5]*len(stamps),'temperature_2m':[12]*len(stamps)}}
    return [one for _ in q['latitude'][0].split(',')]


@pytest.mark.parametrize('profile,count',[('nl',4),('europe',10),('local',1)])
def test_archived_run_is_batched_cached_and_cannot_enter_live_predictions(tmp_path,monkeypatch,profile,count):
    calls=[]
    def opener(req,**kwargs):calls.append(req.full_url);return Response(archive_response(req.full_url))
    monkeypatch.setattr('app.weather_cache.cached_json',lambda url,root:cached_json(url,root,opener=opener,clock=lambda:NOW.timestamp()))
    ctx=fetch_weather_context(profile,52,5,ISSUE-timedelta(days=28),ISSUE-timedelta(hours=12),NOW,tmp_path)
    for _ in range(2):
        s=fetch_archived_weather(profile,52,5,ISSUE,168,NOW,tmp_path,ctx)
    assert len(calls)==2 and len(s['locations'])==count
    run=datetime.fromisoformat(s['weather_run'].replace('Z','+00:00'))
    assert run<=ISSUE-timedelta(hours=12)
    assert all(len(parse_qs(urlsplit(url).query)['latitude'][0].split(','))==count for url in calls)
    settings={'weather_region':profile,'latitude':52.,'longitude':5.}
    with pytest.raises(ValueError,match='uitsluitend'):weather_covariates(s,ISSUE-timedelta(days=28),2688,672,ISSUE,settings)
    settings['_historical_weather_reconstruction']=True
    rows=weather_covariates(s,ISSUE-timedelta(days=28),2688,672,ISSUE,settings)
    assert rows[0][0]==50 and rows[0][2688:]==[200]*672
    with pytest.raises(ValueError,match='uitsluitend'):weather_covariates(s,ISSUE-timedelta(days=28),2688,672,ISSUE+timedelta(days=1),settings)


def test_historical_test_uses_old_runs_and_keeps_archive_scores_out_of_live(tmp_path,monkeypatch):
    app=AppService(store=Store(tmp_path),clock=lambda:NOW)
    app.put_settings({'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh','timestral_weather':True,'timestral_model':'28M',
        'forecast_methods':{'local':True,'v4':True,'epex':False,'timestral':True}})
    settings=app.get_settings();archive=app._archive_entity('sensor.prices',settings)
    app.store.upsert_quarters([{'entity_id':archive,'start_utc':NOW-timedelta(minutes=15*i),
        'end_utc':NOW-timedelta(minutes=15*(i-1)),'price':.2,'observed_at':NOW,'published_at':NOW,
        'unit':'EUR/kWh','quality':'valid'} for i in reversed(range(1,30*96+1))])
    urls=[]
    def opener(req,**kwargs):urls.append(req.full_url);return Response(archive_response(req.full_url))
    monkeypatch.setattr('app.weather_cache.cached_json',lambda url,root:cached_json(url,root,opener=opener,clock=lambda:NOW.timestamp()))
    def base(rows,issued_at,weather=None,max_points=672,**kwargs):
        assert weather['hourly'][utc_iso(issued_at)]['shortwave_radiation']==200
        return ForecastRun('base-test',issued_at,[ForecastPoint(issued_at+timedelta(minutes=15*i),issued_at+timedelta(minutes=15*(i+1)),.3) for i in range(max_points)],'voorlopig',[])
    monkeypatch.setattr('app.comparison.forecast_quarters',base)
    monkeypatch.setattr('app.comparison.adaptive_forecast',lambda rows,issue,**kwargs:base(rows,issue,**{k:v for k,v in kwargs.items() if k!='base'}))
    spec=model_spec('28M')
    def runner(method,body,*args,**kwargs):
        n=body['prediction_length']
        assert body['weather'][0][-n:]==[200]*n
        return {'model':spec['repo'],'revision':spec['revision'],'calendar':True,'weather':True,'median':[.3]*n,'lower':[.1]*n,'upper':[.5]*n}
    monkeypatch.setattr('app.comparison.timestral_forecast',lambda rows,issue,settings,max_points:timestral_forecast(rows,issue,settings,max_points,runner=runner))
    start_history_test(app,168,'archive');app._history_thread.join(timeout=15)
    assert app._history_job['state']=='completed',app._history_job
    assert not app._history_job['errors'] and not app._history_job['skipped']
    assert len(urls)==8  # One context dataset + seven distinct original runs.
    result=comparison(app,mode='historical',horizon='144-168u',selected=['local','v4','timestral'])
    assert result['view']['paired'] and result['view']['n']==7*96
    assert all(m['version'].endswith(WEATHER_ARCHIVE_VERSION) and 'archiefweer' in m['label'] for m in result['view']['metrics'])
    assert comparison(app,mode='live')['view']['n']==0
    assert app.store.latest_forecast()[0] is None


def test_background_archive_waits_for_minute_budget_without_retrying_the_network(tmp_path,monkeypatch):
    stamp=[NOW.timestamp()]
    root=tmp_path/'weather-cache';root.mkdir()
    (root/'budget.json').write_text(json.dumps({'day':NOW.date().isoformat(),'count':12,'units':12,'recent':[stamp[0]]*12}))
    calls=[]
    def opener(*a,**kw):calls.append(1);return Response({'ok':True})
    monkeypatch.setattr('app.weather_cache.cached_json',lambda url,root:cached_json(url,root,opener=opener,clock=lambda:stamp[0]))
    class Stop:
        def is_set(self):return False
        def wait(self,seconds):stamp[0]+=seconds;return False
    url='https://single-runs-api.open-meteo.com/v1/forecast?latitude=52&longitude=5&run=2026-10-14T12:00'
    result=_archive_json(url,tmp_path,Stop())
    assert calls==[1] and result[0]=={'ok':True} and stamp[0]>=NOW.timestamp()+60
