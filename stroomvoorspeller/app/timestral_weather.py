"""Aligned price-independent weather covariates, no invented missing weather."""
from datetime import datetime,timedelta,timezone
import math


def validate_covariates(rows,context,horizon):
    if not isinstance(rows,list) or len(rows)!=3: raise ValueError('Timestral vereist drie weerreeksen')
    for row in rows:
        if not isinstance(row,list) or len(row)!=context+horizon: raise ValueError('Onjuiste lengte van Timestral-weer')
        if any(v is not None and (type(v) not in (int,float) or not math.isfinite(v)) for v in row):
            raise ValueError('Ongeldige Timestral-weerwaarden')
        if sum(v is not None for v in row[:context])<192 or any(v is None for v in row[context:]):
            raise ValueError('Timestral-weer vereist minimaal 192 historische kwartieren en een volledige verwachting')


def weather_covariates(snapshot,begin,context,horizon,issue,settings):
    if not snapshot or snapshot.get('source')!='open_meteo' or snapshot.get('profile')!=settings.get('weather_region','nl'):
        raise ValueError('Timestral met weer vereist passende regionale Open-Meteo-data; kies Open-Meteo of zet Timestral-weer uit')
    if snapshot.get('profile')=='local' and snapshot.get('location')!={'latitude':settings.get('latitude'),'longitude':settings.get('longitude')}:
        raise ValueError('Timestral-weerlocatie komt niet overeen')
    observed=datetime.fromisoformat(snapshot['observed_at'].replace('Z','+00:00'))
    if observed>issue+timedelta(minutes=5) or issue-observed>timedelta(hours=6):
        raise ValueError('Geen passende weersverwachting van rond deze uitgifte; historische weerdata wordt niet verzonnen')
    keys=('solar_wm2','wind_ms','temp_c');rows=[[],[],[]]
    for i in range(context+horizon):
        stamp=(begin+i*timedelta(minutes=15)).astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0).isoformat().replace('+00:00','Z')
        values=(snapshot.get('hourly') or {}).get(stamp,{})
        for row,key in zip(rows,keys):row.append(values.get(key))
    validate_covariates(rows,context,horizon)
    return rows
