"""Storage accounting, explicit maintenance and privacy-filtered research exports."""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

from .local_runtime import BUSY
from .storage import price_archive_key, utc_iso
from .timestral_models import MODELS

TABLE_DATES = {'quarter_prices': 'end_utc', 'quarter_price_revisions': 'end_utc',
               'hourly_history': 'end_utc', 'forecast_runs': 'issued_at', 'input_snapshots': 'issued_at'}
MAX_EXPORT_BYTES = 32 * 1024 * 1024


def days_value(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 3650:
        raise ValueError(f'Kies een geheel aantal dagen van {minimum} t/m 3650')
    return value


def file_bytes(path):
    if path.is_symlink(): return 0
    if path.is_file(): return path.stat().st_size
    if not path.is_dir(): return 0
    total = 0
    for child in path.iterdir():
        try: total += file_bytes(child)
        except FileNotFoundError: pass  # concurrent cache/download rename
    return total


def storage_status(service, days=90):
    days_value(days, 7)
    root = service.store.data_dir
    groups = {'database_bytes': 0, 'timestral_bytes': 0, 'epex_cache_bytes': 0, 'other_bytes': 0}
    for child in root.iterdir():
        if child.name.startswith('stroomvoorspeller.sqlite3'):
            groups['database_bytes'] += file_bytes(child)
        elif child.name == 'models' and not child.is_symlink():
            for model in child.iterdir():
                key = ('timestral_bytes' if model.name in {s['folder'] for s in MODELS.values()} else
                       'epex_cache_bytes' if model.name == 'epex' else 'other_bytes')
                groups[key] += file_bytes(model)
        else: groups['other_bytes'] += file_bytes(child)
    cutoff = utc_iso(service.clock() - timedelta(days=days))
    with closing(service.store._connect()) as db:
        counts = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in [*TABLE_DATES, 'forecast_points']}
        removable = {table: db.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}<?', (cutoff,)).fetchone()[0]
                     for table, column in TABLE_DATES.items()}
    return {**groups, 'total_bytes': sum(groups.values()), 'free_bytes': shutil.disk_usage(root).free,
            'counts': counts, 'removable': removable, 'days': days, 'cutoff_utc': cutoff,
            'measured_at': utc_iso(service.clock()),
            'note': 'Bestandsgroottes in de datamap, inclusief database/WAL en modellen; exclusief add-on-image en backups buiten deze map.'}


def cleanup(service, body):
    if body.get('confirmed') is not True:
        raise ValueError('Bevestig het verwijderen van de gekozen gegevens')
    action = body.get('action')
    if action not in {'history', 'epex_cache', 'unused_models'}:
        raise ValueError('Onbekende opruimactie')
    days = days_value(body.get('days', 90), 7)
    if not service._lock.acquire(blocking=False):
        raise ValueError('Een berekening is bezig; probeer opruimen later opnieuw')
    acquired = False
    history_locked = download_locked = False
    try:
        history_locked = service._history_job_lock.acquire(blocking=False)
        download_locked = service._download_lock.acquire(blocking=False)
        if not history_locked or not download_locked:
            raise ValueError("Een achtergrondtaak start; probeer later opnieuw")
        if service._history_job.get('state') == 'running' or service._download_job.get('state') == 'running':
            raise ValueError('Een historische test of modeldownload is bezig; probeer later opnieuw')
        acquired = BUSY.acquire(blocking=False)
        if not acquired: raise ValueError('Een lokale ML-berekening is bezig; probeer later opnieuw')
        removed, compacted = {}, True
        if action == 'history':
            cutoff = utc_iso(service.clock() - timedelta(days=days))
            with service.store._lock, closing(service.store._connect()) as db:
                with db:
                    for table, column in TABLE_DATES.items():
                        removed[table] = db.execute(f'DELETE FROM {table} WHERE {column}<?', (cutoff,)).rowcount
                # SQLite DELETE frees pages internally; VACUUM also returns them to disk.
                try:
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute('VACUUM')
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                except Exception:
                    compacted = False  # committed deletions still succeeded
        else:
            models = service.store.data_dir / 'models'
            if models.is_symlink(): raise ValueError('Modelmap is een symbolische link; automatisch opruimen geweigerd')
            selected = service.get_settings()['timestral_model']
            targets = ['epex'] if action == 'epex_cache' else [spec['folder'] for size, spec in MODELS.items() if size != selected]
            for name in targets:
                path = models / name
                if path.is_symlink(): raise ValueError('Modelmap is een symbolische link; automatisch opruimen geweigerd')
            for name in targets:
                path = models / name
                if path.is_dir():
                    removed[name] = file_bytes(path)
                    shutil.rmtree(path)
        service._analysis_cache = None
        service._maturity_cache.clear()
        return {'ok': True, 'action': action, 'removed': removed, 'compacted': compacted,
                'message': 'Opruimen voltooid.' if compacted else 'Gegevens verwijderd; database kon nog niet worden verkleind. Probeer later opnieuw.',
                'storage': storage_status(service, days)}
    finally:
        if acquired: BUSY.release()
        if download_locked: service._download_lock.release()
        if history_locked: service._history_job_lock.release()
        service._lock.release()


def export_archive(service, days=30):
    """Consistent SQLite read snapshot, streamed JSONL into a bounded ZIP file.

    Returns an open temporary file. No raw DB, settings dump, errors, paths,
    coordinates, HA responses or compressed raw input histories are copied.
    """
    days_value(days)
    if not service._lock.acquire(blocking=False):
        raise ValueError('Een berekening is bezig; probeer exporteren later opnieuw')
    output = tempfile.TemporaryFile()
    try:
        now = service.clock()
        cutoff, until = utc_iso(now - timedelta(days=days)), utc_iso(now)
        price_cutoff = utc_iso(now - timedelta(days=days + 90))
        salt = uuid.uuid4().hex
        def alias(value):
            return 'series-' + hashlib.sha256((salt + str(value)).encode()).hexdigest()[:16]
        counts = {}
        def numeric(value):
            return value if type(value) in (int, float) and math.isfinite(value) else None
        def weather(value):
            if not isinstance(value, dict): return {}
            hourly = value.get('hourly', {})
            if not isinstance(hourly, dict): return {}
            result = {}
            for stamp, row in hourly.items():
                if not isinstance(row, dict): continue
                try: stamp = utc_iso(stamp)
                except (ValueError, TypeError): continue
                result[stamp] = {key: numeric(row[key]) for key in ('solar_wm2', 'wind_ms', 'temp_c', 'shortwave_radiation', 'wind_ms_10m') if key in row}
            cleaned = {'hourly': result}
            if value.get('reconstruction') is True: cleaned['reconstruction']=True
            if value.get('archive_kind')=='single_run_gfs': cleaned['archive_kind']='single_run_gfs'
            for field in ('weather_run','issued_for'):
                if value.get(field):
                    try: cleaned[field]=utc_iso(value[field])
                    except (ValueError,TypeError): pass
            if value.get('observed_at'):
                try: cleaned['observed_at'] = utc_iso(value['observed_at'])
                except (ValueError, TypeError): pass
            return cleaned
        with closing(service.store._connect()) as db, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            db.execute('BEGIN')
            def write_rows(name, rows):
                count = 0
                with archive.open(name + '.jsonl', 'w') as stream:
                    for row in rows:
                        stream.write((json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode())
                        count += 1
                        if output.tell() > MAX_EXPORT_BYTES:
                            raise ValueError('Export groter dan 32 MiB; kies een kortere periode')
                counts[name] = count
            for table in ('quarter_prices', 'quarter_price_revisions', 'hourly_history'):
                def prices(table=table):
                    for row in db.execute(f'SELECT * FROM {table} WHERE end_utc>=? ORDER BY entity_id,start_utc', (price_cutoff,)):
                        result = {key: row[key] for key in ('start_utc', 'end_utc', 'price', 'unit', 'observed_at')}
                        result['series_id'] = alias(row['entity_id'])
                        if 'published_at' in row.keys(): result['published_at'] = row['published_at']
                        if 'quality' in row.keys(): result['quality'] = row['quality']
                        yield result
                write_rows(table, prices())
            def runs():
                for row in db.execute('SELECT * FROM forecast_runs WHERE issued_at>=? AND issued_at<=? ORDER BY issued_at,id', (cutoff, until)):
                    inputs = json.loads(row['inputs_json'])
                    yield {'id': row['id'], 'issued_at': row['issued_at'], 'model_version': row['model_version'],
                           'quality': row['quality'], 'method': inputs.get('method', 'local'),
                           'evaluation_mode': inputs.get('evaluation_mode', 'live'),
                           'test_horizon_hours': inputs.get('test_horizon_hours'),
                           'weather_reconstruction': inputs.get('weather_reconstruction',False), 'weather_region': inputs.get('weather_region'), 'timestral_weather': inputs.get('timestral_weather', False),
                           'unit': inputs.get('tariff_unit'), 'price_field': inputs.get('price_field', 'tax_included'),
                           'series_id': alias(price_archive_key(inputs.get('tariff_entity', ''), inputs.get('price_field', 'tax_included'), inputs.get('tariff_unit'))),
                           'weather_snapshot': weather(inputs.get('weather_snapshot')), 'weather_model': weather(inputs.get('weather_model'))}
            write_rows('forecast_runs', runs())
            write_rows('forecast_points', (dict(row) for row in db.execute('''SELECT p.run_id,p.start_utc,p.end_utc,p.price,p.lower_price,p.upper_price,p.quality
                FROM forecast_points p JOIN forecast_runs r ON r.id=p.run_id WHERE r.issued_at>=? AND r.issued_at<=? ORDER BY r.issued_at,p.run_id,p.start_utc''', (cutoff, until))))
            # Read settings from the same DB snapshot, strictly allowlisted.
            settings = {row['key']: json.loads(row['value_json']) for row in db.execute('SELECT key,value_json FROM settings')}
            allowed = ('price_source', 'price_choice', 'supplier', 'tariff_unit', 'price_field', 'energy_tax_eur_kwh', 'vat_percent',
                       'supplier_fee_eur_kwh_incl_vat', 'tariff_valid_from', 'tariff_valid_to', 'forecast_methods', 'active_method',
                       'weather_region', 'timestral_weather', 'timestral_model', 'timestral_calendar', 'timestral_context_days', 'epex_training_days', 'epex_tariff_profile', 'epex_multiplier',
                       'epex_offset_eur_kwh', 'history_days', 'calculation_interval_minutes')
            archive.writestr('settings.json', json.dumps({key: settings[key] for key in allowed if key in settings}))
            archive.writestr('README.txt', 'Stroomvoorspeller analyse-export\n\nJSONL: een JSON-record per regel; timestamps zijn UTC.\nKoppel forecast_points.run_id aan forecast_runs.id. Koppel werkelijke prijzen aan forecast_runs.series_id en forecast_points.start_utc. Gebruik quarter_price_revisions met observed_at/published_at voor een as-of reconstructie. Prijswaarden en banden gebruiken de unit van de run/reeks (EUR/kWh of EUR/MWh). Live en historical niet mengen.\nModelinvoerhistorie is niet per run gedupliceerd: prijsdata bevat 90 extra dagen lookback. Volledige reproduceerbaarheid is niet gegarandeerd als brondata eerder is verwijderd. Instellingen zijn de huidige instellingen, geen configuratiehistorie. Geen ruwe database, snapshots, tokens, locatiecoordinaten, modelgewichten of originele entiteitsnamen. De export kan nog persoonlijke patronen bevatten.\nDeze ZIP is tijdelijk lokaal gemaakt en wordt niet automatisch verstuurd.\n')
            archive.writestr('manifest.json', json.dumps({'schema': 'stroomvoorspeller-research-v1', 'created_at': until,
                'forecast_from': cutoff, 'prices_from': price_cutoff, 'counts': counts,
                'notice': 'Prijsdata bevat 90 extra dagen invoerhistorie. Live en historisch blijven onderscheiden. Reeks-ID’s zijn alleen binnen deze export vergelijkbaar. Geen tokens, locatie, entiteitsnamen, ruwe snapshots of modelgewichten. Prijs- en weerpatronen kunnen nog persoonlijke informatie bevatten. Geen automatische upload.'}))
        if output.tell() > MAX_EXPORT_BYTES: raise ValueError('Export groter dan 32 MiB; kies een kortere periode')
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
    finally:
        service._lock.release()
