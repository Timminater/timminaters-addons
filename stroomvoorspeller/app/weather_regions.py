"""Versioned, modest grids. Equal point weights are a proxy, not production shares."""
NL = [('Noord/oost',52.69,6.11),('West',52.36,4.90),('Zuid',51.45,5.47),('Noordzee/noord',53.36,4.98)]
EU = [('Duitsland west',51.3,8.6),('Duitsland noord',53.8,8.1),('België',50.73,4.79),
      ('Frankrijk noord',48.7,4.5),('Denemarken',56.24,9.7),('Verenigd Koninkrijk',54.0,-1.5)]


def locations(profile='nl',latitude=None,longitude=None):
    if profile=='nl': return NL
    if profile=='europe': return NL+EU
    if profile=='local':
        if latitude is None or longitude is None: raise ValueError('Vul coördinaten in voor de lokale weerlocatie')
        latitude,longitude=float(latitude),float(longitude)
        if not -90<=latitude<=90 or not -180<=longitude<=180: raise ValueError('Ongeldige weerlocatie')
        return [('Lokaal',latitude,longitude)]
    raise ValueError('Kies lokaal, Nederland of Nederland en buurlanden als weergebied')


def signature(settings):
    profile=settings.get('weather_region','nl')
    import hashlib,json
    return hashlib.sha256(json.dumps(locations(profile,settings.get('latitude'),settings.get('longitude'))).encode()).hexdigest()[:10]
