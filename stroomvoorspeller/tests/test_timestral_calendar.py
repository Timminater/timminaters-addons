from datetime import datetime, timedelta, timezone
import pytest
from app.timestral_calendar import weekend_covariate
from app.methods import timestral_forecast, timestral_version
from app.timestral_models import model_spec
from app.backend import AppService
from app.storage import Store


def test_local_weekend_boundaries_and_dst_use_utc_quarter_grid():
    assert weekend_covariate('2026-10-23T21:45:00Z',3)==[[0,1,1]]
    assert weekend_covariate('2026-10-25T22:45:00Z',3)==[[1,0,0]]
    assert weekend_covariate('2026-10-25T00:45:00Z',4)==[[1,1,1,1]]  # repeated local 02:00
    for start in (None,'2026-10-23T00:00:00','2026-10-23T00:01:00Z'):
        with pytest.raises(ValueError):weekend_covariate(start,10)


def test_calendar_setting_is_explicit_and_versions_do_not_mix(tmp_path):
    app=AppService(store=Store(tmp_path))
    assert app.get_settings()['timestral_calendar'] is True
    app.put_settings({'timestral_calendar':False})
    assert app.get_settings()['timestral_calendar'] is False
    with pytest.raises(ValueError):app.put_settings({'timestral_calendar':'true'})
    assert timestral_version({'timestral_calendar':True})!=timestral_version({'timestral_calendar':False})
    assert timestral_version({})==timestral_version({'timestral_calendar':False})


def test_adapter_passes_exact_context_start_and_no_future_target_prices():
    now=datetime(2026,10,23,tzinfo=timezone.utc)
    start=now-timedelta(days=3)
    history=[{'start_utc':start+i*timedelta(minutes=15),'end_utc':start+(i+1)*timedelta(minutes=15),
              'price':.2,'published_at':start+(i+1)*timedelta(minutes=15)} for i in range(288)]
    settings={'timestral_model':'28M','timestral_calendar':True}
    spec=model_spec('28M');sent=[]
    def runner(method,payload,*args,**kwargs):
        sent.append(payload)
        return {'model':spec['repo'],'revision':spec['revision'],'calendar':True,'median':[.1]*4,'lower':[-.1]*4,'upper':[.3]*4}
    timestral_forecast(history,now,settings,4,runner=runner)
    assert sent[0]['calendar'] is True
    assert datetime.fromisoformat(sent[0]['context_start'])==start
    assert sent[0]['context']==[.2]*288
    with pytest.raises(ValueError,match='weekendinformatie'):
        timestral_forecast(history,now,settings,4,runner=lambda *a,**kw:{'model':spec['repo'],'revision':spec['revision']})
