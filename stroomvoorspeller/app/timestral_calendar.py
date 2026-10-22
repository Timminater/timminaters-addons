"""Calendar-only future inputs. Never derives covariates from future prices."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def weekend_covariate(start, length):
    try: start = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError): raise ValueError('Timestral-kalender vereist een geldige UTC-contextstart') from None
    if start.tzinfo is None or start.minute % 15 or start.second or start.microsecond:
        raise ValueError('Timestral-kalender vereist een kwartierstart met tijdzone')
    if type(length) is not int or not 1 <= length <= 9312: raise ValueError('Ongeldige kalenderlengte')
    start = start.astimezone(timezone.utc)
    zone = ZoneInfo('Europe/Amsterdam')
    return [[int((start + i*timedelta(minutes=15)).astimezone(zone).weekday() >= 5) for i in range(length)]]
