"""Cross-process Open-Meteo cache and request budget, including failed calls."""
from datetime import datetime, timezone
import math
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qsl, urlencode
from urllib.request import Request, urlopen

LIVE_TTL = 3*3600
MAX_BYTES = 8*1024*1024


def _read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {}


def _write(path, data):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,allow_nan=False,separators=(',',':')))
    temporary.replace(path)


def cached_json(url, root, *, opener=urlopen, clock=time.time):
    """No force bypass. Historical ranges cache 30d; live location/variable sets 3h.

    Live keys omit dates, deliberately preventing rolling endpoints and profile
    switches back to the same grid from bypassing the refresh interval.
    """
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname not in {'api.open-meteo.com','historical-forecast-api.open-meteo.com'}:
        raise ValueError('Onbekende weerbron')
    historical=parts.hostname.startswith('historical-')
    params=sorted((k,v) for k,v in parse_qsl(parts.query) if historical or k not in {'start_date','end_date'})
    key=hashlib.sha256((parts.hostname+parts.path+'?'+urlencode(params)).encode()).hexdigest()
    root=Path(root)/'weather-cache';root.mkdir(parents=True,exist_ok=True)
    target=root/(key+'.json');statefile=root/'budget.json'
    with (root/'budget.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        now=clock();cached=_read(target);state=_read(statefile)
        ttl=30*86400 if historical else LIVE_TTL
        age=now-cached.get('fetched_at',0)
        if cached.get('data') is not None and 0<=age<ttl:
            return cached['data'],cached['fetched_at']
        if now<max(state.get('blocked_until',0),cached.get('retry_after',0)):
            raise ValueError('Weerbron in afkoelperiode; bestaande cache wordt hergebruikt waar volledig')
        day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        calls=[t for t in state.get('recent',[]) if now-60<t<=now]
        count=state.get('count',0) if state.get('day')==day else 0
        units=state.get('units',0) if state.get('day')==day else 0
        query=dict(parse_qsl(parts.query))
        points=len(query.get('latitude','').split(','))
        span=int(query.get('past_days','0'))+int(query.get('forecast_days','8'))
        if 'start_date' in query and 'end_date' in query:
            span=(datetime.fromisoformat(query['end_date'])-datetime.fromisoformat(query['start_date'])).days+1
        cost=points*max(1,math.ceil(span/14))
        if count>=48 or units+cost>512 or len(calls)>=12:
            raise ValueError('Gedeeld weer-API-budget bereikt; probeer later opnieuw')
        state.update(day=day,count=count+1,units=units+cost,recent=[*calls,now])
        _write(statefile,state)  # reserve before I/O, including crashes and failures
        try:
            with opener(Request(url,headers={'Accept':'application/json','User-Agent':'Stroomvoorspeller/weer'}),timeout=45) as response:
                raw=response.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES: raise ValueError('Weerantwoord overschrijdt 8 MiB')
            data=json.loads(raw)
            if not isinstance(data,(dict,list)) or isinstance(data,dict) and data.get('error'):
                raise ValueError('Weerbron retourneerde geen geldige weerdata')
            _write(target,{'fetched_at':now,'data':data})
            # Bound the shared payload cache; never remove request-budget state.
            entries=sorted(root.glob('[0-9a-f]'*64+'.json'),key=lambda p:p.stat().st_mtime)
            total=sum(p.stat().st_size for p in entries)
            for old in entries:
                if total<=64*1024*1024: break
                if old==target: continue
                total-=old.stat().st_size;old.unlink(missing_ok=True)
            return data,now
        except Exception as exc:
            delay=3600
            if isinstance(exc,HTTPError) and exc.code in {429,503}:
                delay=6*3600
                value=exc.headers.get('Retry-After','') if exc.headers else ''
                try: delay=max(delay,int(value))
                except ValueError:
                    try: delay=max(delay,parsedate_to_datetime(value).timestamp()-now)
                    except (TypeError,ValueError,OverflowError): pass
                state['blocked_until']=now+delay
                _write(statefile,state)
            cached['retry_after']=now+delay
            _write(target,cached)
            raise ValueError('Weerbron niet beschikbaar; aanvragen zijn tijdelijk gepauzeerd') from exc
