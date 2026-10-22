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
    if settings.get('weather_source','open_meteo')!='open_meteo':
        raise ValueError('Timestral met weer gebruikt Open-Meteo; de ingestelde weerbron is Home Assistant')
    if not snapshot:
        detail=settings.get('_weather_error') or 'Er is nog geen bruikbare weersnapshot beschikbaar voor deze berekening'
        raise ValueError('Geen actuele Open-Meteo-weerdata voor Timestral: '+detail)
    if snapshot.get('source')!='open_meteo':
        raise ValueError('De beschikbare weersnapshot komt niet van Open-Meteo; deze wordt niet voor Timestral gebruikt')
    if snapshot.get('profile')!=settings.get('weather_region','nl'):
        raise ValueError('Open-Meteo-cache hoort bij een ander weergebied; verwacht '+settings.get('weather_region','nl'))
    if snapshot.get('profile')=='local' and snapshot.get('location')!={'latitude':settings.get('latitude'),'longitude':settings.get('longitude')}:
        raise ValueError('Timestral-weerlocatie komt niet overeen')
    observed=datetime.fromisoformat(snapshot['observed_at'].replace('Z','+00:00'))
    reconstructed=snapshot.get('reconstruction') is True
    if reconstructed:
        run=datetime.fromisoformat(snapshot['weather_run'].replace('Z','+00:00'))
        for_issue=datetime.fromisoformat(snapshot['issued_for'].replace('Z','+00:00'))
        if settings.get('_historical_weather_reconstruction') is not True or for_issue!=issue or run>issue-timedelta(hours=12):
            raise ValueError('Gereconstrueerd archiefweer is uitsluitend voor de juiste historische uitgifte toegestaan')
    elif observed>issue+timedelta(minutes=5) or issue-observed>timedelta(hours=6):
        raise ValueError('Geen passende weersverwachting van rond deze uitgifte; historische weerdata wordt niet verzonnen')
    keys=('solar_wm2','wind_ms','temp_c');rows=[[],[],[]]
    for i in range(context+horizon):
        stamp=(begin+i*timedelta(minutes=15)).astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0).isoformat().replace('+00:00','Z')
        values=(snapshot.get('hourly') or {}).get(stamp,{})
        for row,key in zip(rows,keys):row.append(values.get(key))
    validate_covariates(rows,context,horizon)
    return rows
