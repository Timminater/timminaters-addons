from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import base64
import io
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile
import zlib

import pytest

from app.backend import AppService, make_handler
from app.compact_database import compact_copy
from app.data_management import cleanup, export_archive, storage_status
from app.local_runtime import BUSY
from app.storage import Store, price_archive_key, utc_iso

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def fixture_app(tmp_path):
    app = AppService(store=Store(tmp_path), clock=lambda: NOW)
    app.store.set_settings({'tariff_entity': 'sensor.secret_house_tariff', 'tariff_unit': 'EUR/kWh',
                            'timestral_model': '28M', 'latitude': 52.123456, 'longitude': 4.123456,
                            'ha_token': 'SECRET_TOKEN', 'weather_entities': {'solar': 'sensor.private_location'}})
    for age in (40, 2):
        stamp = NOW - timedelta(days=age)
        app.store.upsert_quarters([{'entity_id': price_archive_key('sensor.secret_house_tariff', 'tax_included', 'EUR/kWh'),
          'start_utc': stamp, 'end_utc': stamp + timedelta(minutes=15), 'price': -.01, 'unit': 'EUR/kWh',
          'observed_at': stamp, 'published_at': stamp, 'quality': 'published', 'source': 'ha'}])
        app.store.save_forecast(f'run-{age}', stamp, 'adaptive-quarter-1', 'voorlopig', ['SECRET_REASON'],
            {'tariff_entity': 'sensor.secret_house_tariff', 'tariff_unit': 'EUR/kWh', 'evaluation_mode': 'historical' if age == 40 else 'live',
             'method': 'local', 'history': [{'token': 'SECRET_HISTORY'}],
             'weather_snapshot': {'latitude': 52.123456, 'token': 'SECRET_WEATHER',
                                  'hourly': {utc_iso(stamp): {'solar_wm2': 30, 'temp_c': 5, 'token': 'SECRET_HOURLY'}}}},
            [{'start_utc': stamp, 'end_utc': stamp + timedelta(minutes=15), 'price': .02, 'reason': 'SECRET_POINT'}])
        app.store.save_snapshot(stamp, 'price', {'token': 'SECRET_SNAPSHOT'}, 'SECRET_ERROR')
    return app


def test_status_accounts_files_and_previews_old_rows(tmp_path):
    app = fixture_app(tmp_path)
    for name, size in [('timestral-28m/model.safetensors', 100), ('timestral-5m/model.safetensors', 30), ('epex/cache.json', 20)]:
        path = tmp_path / 'models' / name
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * size)
    status = storage_status(app, 7)
    assert status['timestral_bytes'] == 130
    assert status['epex_cache_bytes'] == 20
    assert status['database_bytes'] > 0
    assert status['total_bytes'] == sum(status[key] for key in ('timestral_bytes', 'epex_cache_bytes', 'database_bytes', 'other_bytes'))
    assert status['counts']['forecast_points'] == 2
    assert status['removable']['forecast_runs'] == 1
    assert status['removable']['quarter_prices'] == 1


def test_cleanup_cascades_old_runs_preserves_recent_data_and_settings(tmp_path):
    app = fixture_app(tmp_path)
    original = app.store.get_settings()
    result = cleanup(app, {'action': 'history', 'days': 7, 'confirmed': True})
    assert result['compacted'] is True
    assert result['removed']['forecast_runs'] == 1
    assert app.store.get_settings() == original
    status = storage_status(app, 7)
    assert status['counts']['forecast_points'] == 1
    assert status['counts']['quarter_price_revisions'] == 1
    assert status['counts']['quarter_prices'] == 1
    assert status['counts']['input_snapshots'] == 1
    assert status['removable']['forecast_runs'] == 0


def test_forecast_history_shares_unchanged_days_and_reads_legacy_runs(tmp_path):
    store = Store(tmp_path)
    older = NOW - timedelta(days=181)
    recent = NOW - timedelta(days=2)
    scope = {'tariff_entity': 'sensor.tariff', 'tariff_unit': 'EUR/kWh'}
    shared = {'start_utc': '2026-04-20T00:00:00Z', 'price': .22}
    revised = {'start_utc': '2026-04-21T00:00:00Z', 'price': .31}
    old_history = [shared, {**revised, 'price': .30}]
    recent_history = [shared, revised]
    store.save_forecast('old', older, 'v', 'voorlopig', [], {**scope, 'history': old_history}, [])
    store.save_forecast('recent', recent, 'v', 'voorlopig', [], {**scope, 'history': recent_history}, [])
    with closing(store._connect()) as db, db:
        assert db.execute('SELECT COUNT(*) FROM forecast_history_refs').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM forecast_history_chunks').fetchone()[0] == 3
        legacy = base64.b64encode(zlib.compress(json.dumps(old_history).encode())).decode()
        db.execute('INSERT INTO forecast_runs VALUES(?,?,?,?,?,?)',
                   ('legacy', utc_iso(recent - timedelta(days=1)), 'v', 'voorlopig', '[]',
                    json.dumps({**scope, 'history_zlib_b64': legacy})))

    assert store.latest_forecast()[0]['inputs']['history'] == recent_history
    assert store.latest_forecast(include_history=False)[0]['inputs'] == scope
    assert [run['history'] for run in store.forecast_snapshots()] == [old_history, old_history, recent_history]
    daily = store.daily_stored_forecasts('sensor.tariff', recent - timedelta(days=1), NOW,
                                         'tax_included', 'EUR/kWh', include_history=True)
    assert [run['history'] for run in daily] == [old_history, recent_history]

    store.prune(NOW)
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM forecast_history_chunks').fetchone()[0] == 2
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert store.latest_forecast()[0]['inputs']['history'] == recent_history


def test_compact_copy_preserves_history_points_and_source_wal(tmp_path):
    store = Store(tmp_path / 'original')
    store.set_settings({'tariff_unit': 'EUR/kWh'})
    history = [{'start_utc': utc_iso(NOW), 'price': .12}]
    point = {'start_utc': NOW, 'end_utc': NOW + timedelta(minutes=15), 'price': .23}
    packed = base64.b64encode(zlib.compress(json.dumps(history).encode())).decode()
    with closing(store._connect()) as source:
        source.execute('PRAGMA wal_autocheckpoint=0')
        source.execute("UPDATE settings SET value_json=? WHERE key='tariff_unit'", (json.dumps('EUR/MWh'),))
        source.commit()
        store.save_forecast('raw', NOW, 'v', 'q', [], {'history_zlib_b64': packed, 'weather_region': 'nl'}, [point])
        assert store.path.with_suffix('.sqlite3-wal').stat().st_size > 0
        original_bytes = store.path.read_bytes()
        target, count = compact_copy(store.path, tmp_path / 'compact')
        assert count == 1 and store.path.read_bytes() == original_bytes
        assert source.execute("SELECT json_extract(inputs_json,'$.history_zlib_b64') FROM forecast_runs").fetchone()[0] == packed
        assert Store(target.parent).get_settings() == {'tariff_unit': 'EUR/MWh'}
        assert Store(target.parent).latest_forecast() == store.latest_forecast()
        with pytest.raises(FileExistsError):
            compact_copy(store.path, target.parent)


def test_cleanup_models_preserves_selected_model_and_does_not_follow_symlinks(tmp_path):
    app = fixture_app(tmp_path)
    for folder in ('timestral-28m', 'timestral-5m', 'epex'):
        path = tmp_path / 'models' / folder
        path.mkdir(parents=True); (path / 'weights').write_bytes(b'weights')
    cleanup(app, {'action': 'unused_models', 'confirmed': True})
    assert (tmp_path / 'models/timestral-28m/weights').exists()
    assert not (tmp_path / 'models/timestral-5m').exists()
    assert (tmp_path / 'models/epex').exists()
    cleanup(app, {'action': 'epex_cache', 'confirmed': True})
    assert not (tmp_path / 'models/epex').exists()
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'keep').write_text('safe')
    (tmp_path / 'models/epex').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolische'):
        cleanup(app, {'action': 'epex_cache', 'confirmed': True})
    assert (outside / 'keep').read_text() == 'safe'


def test_cleanup_requires_valid_explicit_consent_and_idle_jobs(tmp_path):
    app = fixture_app(tmp_path)
    for body in ({}, {'action': 'history', 'confirmed': 1}, {'action': '../outside', 'confirmed': True},
                 {'action': 'history', 'days': 0, 'confirmed': True}, {'action': 'history', 'days': True, 'confirmed': True}):
        with pytest.raises(ValueError): cleanup(app, body)
    BUSY.acquire()
    try:
        with pytest.raises(ValueError, match='ML-berekening'):
            cleanup(app, {'action': 'history', 'confirmed': True})
    finally: BUSY.release()
    app._history_job = {'state': 'running'}
    with pytest.raises(ValueError, match='historische test'):
        cleanup(app, {'action': 'history', 'confirmed': True})
    assert storage_status(app)['counts']['forecast_runs'] == 2


def test_export_filters_private_fields_and_links_truth_to_predictions(tmp_path):
    app = fixture_app(tmp_path)
    with export_archive(app, 7) as output:
        with zipfile.ZipFile(output) as archive:
            joined = b''.join(archive.read(name) for name in archive.namelist())
            for private in (b'SECRET', b'sensor.secret', b'52.123456', b'4.123456', b'private_location'):
                assert private not in joined
            runs = [json.loads(line) for line in archive.read('forecast_runs.jsonl').splitlines()]
            prices = [json.loads(line) for line in archive.read('quarter_prices.jsonl').splitlines()]
            points = [json.loads(line) for line in archive.read('forecast_points.jsonl').splitlines()]
            assert len(runs) == len(points) == 1
            assert len(prices) == 2  # 90 extra days for training/context
            assert runs[0]['series_id'] == prices[0]['series_id']
            assert points[0]['run_id'] == runs[0]['id']
            assert runs[0]['evaluation_mode'] == 'live'
            assert next(iter(runs[0]['weather_snapshot']['hourly'].values())) == {'solar_wm2': 30, 'temp_c': 5}
            assert json.loads(archive.read('manifest.json'))['schema'] == 'stroomvoorspeller-research-v1'
    with export_archive(app, 60) as output, zipfile.ZipFile(output) as archive:
        runs = [json.loads(line) for line in archive.read('forecast_runs.jsonl').splitlines()]
        assert {r['evaluation_mode'] for r in runs} == {'live', 'historical'}


def test_export_size_limit_removes_temporary_file_and_releases_lock(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    monkeypatch.setattr('app.data_management.MAX_EXPORT_BYTES', 1)
    with pytest.raises(ValueError, match='32 MiB'): export_archive(app)
    acquired = []
    def check_lock():
        free = app._lock.acquire(blocking=False); acquired.append(free)
        if free: app._lock.release()
    thread = threading.Thread(target=check_lock); thread.start(); thread.join(timeout=2)
    assert acquired == [True]


def test_http_storage_and_export_work_under_ingress_prefix(tmp_path):
    app = fixture_app(tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    root = f'http://127.0.0.1:{server.server_port}/api/hassio_ingress/example'
    try:
        with urlopen(root + '/api/storage?days=7') as response:
            assert json.load(response)['counts']['quarter_prices'] == 2
        with urlopen(root + '/api/export?days=7') as response:
            assert response.headers['Content-Type'] == 'application/zip'
            assert 'attachment' in response.headers['Content-Disposition']
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                assert 'manifest.json' in archive.namelist()
        request = Request(root + '/api/storage/cleanup', data=b'{"action":"history","days":7}', headers={'Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc: urlopen(request)
        assert exc.value.code == 400
        request = Request(root + '/api/storage/cleanup', data=b'{"action":"history","days":7,"confirmed":true}', headers={'Content-Type':'text/plain'})
        with pytest.raises(HTTPError) as exc: urlopen(request)
        assert exc.value.code == 400
        with pytest.raises(HTTPError) as exc: urlopen(root + '/api/export?days=-1')
        assert exc.value.code == 400
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_cleanup_retains_exact_cutoff_and_future_intervals(tmp_path):
    app = fixture_app(tmp_path)
    cutoff = NOW - timedelta(days=7)
    entity = price_archive_key('sensor.secret_house_tariff', 'tax_included', 'EUR/kWh')
    app.store.upsert_quarters([{'entity_id': entity, 'start_utc': cutoff - timedelta(minutes=15),
                               'end_utc': cutoff, 'price': .3, 'unit': 'EUR/kWh', 'observed_at': cutoff}])
    cleanup(app, {'action': 'history', 'days': 7, 'confirmed': True})
    assert any(row['end_utc'] == utc_iso(cutoff) for row in app.store.quarters(entity))


def test_cleanup_rejects_starting_jobs_and_releases_all_guards(tmp_path):
    app = fixture_app(tmp_path)
    app._download_lock.acquire()
    try:
        with pytest.raises(ValueError, match='achtergrondtaak'):
            cleanup(app, {'action': 'unused_models', 'confirmed': True})
    finally: app._download_lock.release()
    assert app._history_job_lock.acquire(blocking=False)
    app._history_job_lock.release()
    assert not BUSY.locked()
    cleanup(app, {'action': 'history', 'days': 7, 'confirmed': True})


def test_external_database_migration_preserves_settings_and_wal(tmp_path):
    import sqlite3
    from app.storage import Store
    data=tmp_path/'app'; external=tmp_path/'media'/'stroomvoorspeller'
    old=Store(data);old.set_settings({'active_method':'v4'})
    conn=sqlite3.connect(old.path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES('migration_probe','42','2026-01-01T00:00:00Z')")
    conn.commit()
    migrated=Store(data,database_dir=external)
    assert migrated.get_settings()['active_method']=='v4'
    assert migrated.get_settings()['migration_probe']==42
    assert migrated.path.parent==external and not old.path.exists()
    conn.close()


def test_existing_external_database_is_never_overwritten(tmp_path):
    from app.storage import Store
    old=Store(tmp_path/'app');old.set_settings({'probe':'old'})
    external=Store(tmp_path/'media');external.set_settings({'probe':'current'})
    result=Store(tmp_path/'app',database_dir=tmp_path/'media')
    assert result.get_settings()['probe']=='current'
    assert old.path.exists()


def test_storage_indicator_includes_external_database(tmp_path):
    from app.storage import Store
    from app.backend import AppService
    from app.data_management import storage_status
    store=Store(tmp_path/'app',database_dir=tmp_path/'media')
    status=storage_status(AppService(store=store),90)
    assert status['database_bytes']>=store.path.stat().st_size
    assert status['total_bytes']>=status['database_bytes']


def test_migration_from_media_takes_precedence_over_old_app_data(tmp_path, monkeypatch):
    from pathlib import Path
    import app.storage as storage
    data = tmp_path / 'data'
    media = tmp_path / 'media' / 'stroomvoorspeller'
    share = tmp_path / 'share' / 'stroomvoorspeller'
    storage.Store(data).set_settings({'probe': 'older'})
    storage.Store(media).set_settings({'probe': 'media'})
    monkeypatch.setattr(storage, 'SHARE_DATABASE_DIR', share)
    monkeypatch.setattr(storage, 'MEDIA_DATABASE_DIR', media)
    monkeypatch.setattr(Path, 'is_mount', lambda self: True)
    monkeypatch.setenv('DATA_DIR', str(data))
    monkeypatch.setenv('DATABASE_DIR', str(share))
    migrated = storage.Store()
    assert migrated.get_settings()['probe'] == 'media'
    assert not (media / 'stroomvoorspeller.sqlite3').exists()
    assert (data / 'stroomvoorspeller.sqlite3').exists()
    storage.Store(media).set_settings({'probe': 'do not overwrite'})
    assert storage.Store().get_settings()['probe'] == 'media'
    assert (media / 'stroomvoorspeller.sqlite3').exists()


def test_missing_media_mount_does_not_create_empty_share_database(tmp_path, monkeypatch):
    from pathlib import Path
    import app.storage as storage
    share = tmp_path / 'share' / 'stroomvoorspeller'
    media = tmp_path / 'media' / 'stroomvoorspeller'
    monkeypatch.setattr(storage, 'SHARE_DATABASE_DIR', share)
    monkeypatch.setattr(storage, 'MEDIA_DATABASE_DIR', media)
    monkeypatch.setattr(Path, 'is_mount', lambda self: self == share.parent)
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('DATABASE_DIR', str(share))
    with pytest.raises(RuntimeError, match='mediamap'):
        storage.Store()
    assert not (share / 'stroomvoorspeller.sqlite3').exists()

def test_export_temp_failure_releases_export_lock_and_can_retry(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    def fail():
        raise OSError("temporary directory full")
    with monkeypatch.context() as m:
        m.setattr("app.data_management.tempfile.TemporaryFile", fail)
        with pytest.raises(OSError):
            export_archive(app)
    assert not app._export_lock.locked()
    assert app._lock.acquire(blocking=False)
    app._lock.release()
    with export_archive(app, 7) as output:
        assert zipfile.is_zipfile(output)
