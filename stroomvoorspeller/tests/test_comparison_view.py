from datetime import datetime, timezone
import pytest
from app.comparison import comparison_view, comparison
from app.backend import AppService
from app.storage import Store


def result():
    return {'methods':[{'id':m,'label':m,'enabled':True,'live':{'version':'test'},'historical':{'version':'test'}} for m in ('local','v4','epex','timestral')]}


def test_chart_scores_and_lines_use_identical_issue_target_pairs():
    old=('2026-10-18T00:00:00Z','2026-10-19T00:00:00Z')
    newer=('2026-10-18T12:00:00Z','2026-10-19T00:00:00Z')
    different=('2026-10-18T12:00:01Z','2026-10-19T00:15:00Z')
    maps={'live':{'local':{old:(.3,.2),newer:(.22,.2),different:(.1,.2)},
                  'v4':{old:(.4,.2),newer:(.24,.2)},'epex':{},'timestral':{}},'historical':{}}
    view=comparison_view(result(),maps,'live','all',['local','v4','epex'],14)
    assert view['mode']=='live' and view['n']==2 and view['paired'] is True
    assert view['missing']==['epex']
    assert view['metrics'][0]['mae']==pytest.approx(.06)
    assert view['metrics'][1]['mae']==pytest.approx(.12)
    assert len(view['points'])==1  # shared target, newest common issue
    assert view['points'][0]['issued_at']==newer[0]
    assert view['points'][0]['predictions']=={'local':.22,'v4':.24}
    assert view['points'][0]['actual']==.2
    assert sum(d['n'] for d in view['daily'])==2


def test_no_overlap_or_no_selection_never_creates_false_ranking():
    a=('2026-10-18T00:00:00Z','2026-10-19T00:00:00Z')
    b=('2026-10-18T00:00:01Z','2026-10-19T00:00:00Z')
    maps={'live':{'local':{a:(.3,.2)},'v4':{b:(.4,.2)},'epex':{},'timestral':{}}}
    view=comparison_view(result(),maps,'live','all',['local','v4'],14)
    assert view['n']==0 and not view['points']
    assert all(m['mae'] is None and m['n']==0 for m in view['metrics'])
    empty=comparison_view(result(),maps,'live','all',[],14)
    assert empty['n']==0 and empty['metrics']==[]


def test_mode_horizon_period_are_preserved_and_invalid_filters_rejected(tmp_path):
    app=AppService(store=Store(tmp_path),clock=lambda:datetime(2026,10,22,tzinfo=timezone.utc))
    for mode in ('live','historical'):
        view=comparison(app,mode=mode,horizon='24-48u',selected=['local'],days=14)['view']
        assert view['mode']==mode and view['days']==14 and view['horizon']=='24-48u'
    for kwargs in ({'mode':'both'},{'horizon':'wrong'},{'selected':['unknown']},{'days':0},{'days':46}):
        with pytest.raises(ValueError):comparison(app,**kwargs)


def test_horizon_boundaries_and_single_method_are_explicit():
    origin='2026-10-18T00:00:00Z'
    maps={'historical':{'local':{(origin,'2026-10-18T23:45:00Z'):(.1,.2),
                                 (origin,'2026-10-19T00:00:00Z'):(.3,.2),
                                 (origin,'2026-10-20T00:00:00Z'):(.6,.2)}}}
    view=comparison_view(result(),maps,'historical','24-48u',['local'],7)
    assert view['n']==1 and not view['paired'] and view['metrics'][0]['mae']==pytest.approx(.1)


def test_shared_issue_is_chosen_before_daily_run_selection(tmp_path):
    from datetime import timedelta
    from app.storage import utc_iso
    now=datetime(2026,10,22,12,tzinfo=timezone.utc)
    app=AppService(store=Store(tmp_path),clock=lambda:now)
    app.put_settings({'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh',
                      'forecast_methods':{'local':True,'v4':True,'epex':False,'timestral':False}})
    s=app.get_settings();archive=app._archive_entity('sensor.prices',s)
    common=now-timedelta(days=2,hours=3)  # 11:00 local, both methods
    solo=common+timedelta(hours=1)      # 12:00 local, local only
    target=common+timedelta(days=1)
    app.store.upsert_quarters([{'entity_id':archive,'start_utc':target,'end_utc':target+timedelta(minutes=15),
                              'price':.2,'unit':'EUR/kWh','observed_at':now,'published_at':now}])
    for method,issued,pred in (('local',common,.3),('v4',common,.4),('local',solo,.1)):
        app.store.save_forecast(f'{method}-{issued}',issued,method+'-v1','voorlopig',[],
          {'tariff_entity':'sensor.prices','tariff_unit':'EUR/kWh','price_field':'tax_included','method':method,'evaluation_mode':'live'},
          [{'start_utc':target,'end_utc':target+timedelta(minutes=15),'price':pred}])
    view=comparison(app,selected=['local','v4'])['view']
    assert view['n']==1 and view['paired']
    assert view['points'][0]['issued_at']==utc_iso(common)
    assert view['points'][0]['predictions']=={'local':.3,'v4':.4}


def test_http_filters_and_timeline_model_identity(tmp_path):
    import json, threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from urllib.error import HTTPError
    from app.backend import make_handler
    app=AppService(store=Store(tmp_path))
    app.put_settings({'price_source':'energy_charts_nl','timestral_model':'28M'})
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    root=f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root+'/api/methods?mode=historical&horizon=24-48u&days=7&methods=none') as response:
            view=json.load(response)['view']
        assert view['selected']==[] and view['days']==7 and view['mode']=='historical'
        with pytest.raises(HTTPError) as exc:urlopen(root+'/api/methods?methods=unknown')
        assert exc.value.code==400
        with urlopen(root+'/api/timeline') as response:timeline=json.load(response)
        assert timeline['method']['id']=='local' and timeline['method']['enabled'] is True
        assert timeline['model_version'] is None  # selected, not falsely labelled as computed
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


@pytest.mark.parametrize('hour,expected',[(48,'48-72u'),(72,'72-96u'),(96,'96-120u'),(120,'120-144u'),(144,'144-168u')])
def test_later_daily_horizons_use_exact_boundaries(hour,expected):
    from datetime import timedelta
    from app.storage import utc_iso
    issue=datetime(2026,10,1,tzinfo=timezone.utc)
    rows={(utc_iso(issue),utc_iso(issue+timedelta(hours=h))):(.3,.2) for h in (hour-.25,hour,hour+23.75,hour+24)}
    view=comparison_view(result(),{'live':{'local':rows}},'live',expected,['local'],14)
    assert view['n']==2
    assert [p['start'] for p in view['points']]==[utc_iso(issue+timedelta(hours=h)) for h in (hour,hour+23.75)]


def test_history_http_horizon_is_forwarded_and_invalid_choices_rejected(tmp_path,monkeypatch):
    import json,threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    from app.backend import make_handler
    from app.comparison import start_history_test
    app=AppService(store=Store(tmp_path));choices=[]
    def start(service,horizon_hours):
        if horizon_hours not in (48,168):return start_history_test(service,horizon_hours)
        choices.append(horizon_hours);return {'state':'running','horizon_hours':horizon_hours}
    monkeypatch.setattr('app.comparison.start_history_test',start)
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    root=f'http://127.0.0.1:{server.server_port}/api/history-test'
    try:
        for suffix,horizon in (('',48),('?horizon_hours=168',168)):
            with urlopen(Request(root+suffix,method='POST')) as response:
                assert response.status==202 and json.load(response)['horizon_hours']==horizon
        assert choices==[48,168]
        for suffix in ('?horizon_hours=72','?horizon_hours=bad'):
            with pytest.raises(HTTPError) as exc:urlopen(Request(root+suffix,method='POST'))
            assert exc.value.code==400
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
