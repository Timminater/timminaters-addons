from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import io
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import pytest

from app.backend import AppService, make_handler
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
