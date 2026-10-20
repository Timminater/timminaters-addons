from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import pytest

from app.backend import AppService, make_handler
from app.local_runtime import BUSY, run_local, start_download, timestral_status
from app.storage import Store


def test_old_service_urls_are_not_exposed_or_accepted(tmp_path):
    app = AppService(store=Store(tmp_path))
    app.store.set_settings({'epex_url': 'http://old-epex', 'timestral_url': 'http://old-timestral'})
    assert 'epex_url' not in app.get_settings() and 'timestral_url' not in app.get_settings()
    for key in ('epex_url', 'timestral_url'):
        with pytest.raises(ValueError, match='Onbekende'):
            app.put_settings({key: 'http://external'})


def test_cpu_training_bounds(tmp_path):
    app = AppService(store=Store(tmp_path))
    assert app.get_settings()['ml_threads'] == 2
    for body in ({'ml_threads': 0}, {'ml_threads': 5}, {'ml_threads': True},
                 {'epex_training_days': 29}, {'epex_training_days': 181}):
        with pytest.raises(ValueError): app.put_settings(body)
    result = app.put_settings({'ml_threads': 1, 'epex_training_days': 30})
    assert result['ml_threads'] == 1 and result['epex_training_days'] == 30


def test_model_download_requires_explicit_confirmation(tmp_path, monkeypatch):
    app = AppService(store=Store(tmp_path))
    monkeypatch.setattr('app.local_runtime.run_local', lambda *a, **k: pytest.fail('unconfirmed download'))
    for confirm in (False, None, 1, 'true'):
        with pytest.raises(ValueError, match='Bevestig'):
            start_download(app, confirm)
    assert app._download_thread is None
    assert not (tmp_path / 'models').exists()


def test_download_job_is_explicit_local_and_async(tmp_path, monkeypatch):
    app = AppService(store=Store(tmp_path))
    operations = []
    def worker(operation, payload, settings, **kwargs):
        operations.append((operation, settings['_data_dir']))
        return {'ready': True}
    monkeypatch.setattr('app.local_runtime.run_local', worker)
    start_download(app, True)
    app._download_thread.join(timeout=3)
    assert app._download_job['state'] == 'completed'
    assert operations == [('timestral_download', str(tmp_path))]
    assert app._wake.is_set()


def test_enable_does_not_implicitly_download_model(tmp_path, monkeypatch):
    from app.local_worker import infer_timestral
    app = AppService(store=Store(tmp_path))
    app.put_settings({'forecast_methods': {'local': False, 'v4': False, 'epex': False, 'timestral': True},
                      'active_method': 'timestral'})
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    with pytest.raises(ValueError, match='Download en bevestig'):
        infer_timestral({'context': [.1] * 192, 'prediction_length': 8, 'threads': 1})
    assert not (tmp_path / 'models').exists()
    assert timestral_status(tmp_path)['ready'] is False


def test_verify_rejects_corrupt_weights_and_config(tmp_path, monkeypatch):
    import app.local_worker as worker
    weights, config = b'valid weights', b'{"config":1}'
    (tmp_path / 'LICENSE').write_text('license')
    (tmp_path / 'model.safetensors').write_bytes(weights)
    (tmp_path / 'config.json').write_bytes(config)
    monkeypatch.setattr(worker, 'WEIGHTS_BYTES', len(weights))
    monkeypatch.setattr(worker, 'WEIGHTS_SHA256', hashlib.sha256(weights).hexdigest())
    monkeypatch.setattr(worker, 'CONFIG_SHA256', hashlib.sha256(config).hexdigest())
    worker.verify_model(tmp_path)
    (tmp_path / 'verified.json').write_text('{}')
    (tmp_path / 'model.safetensors').write_bytes(b'x' * len(weights))
    with pytest.raises(ValueError, match='SHA-256'): worker.verify_model(tmp_path)
    assert not (tmp_path / 'verified.json').exists()
    (tmp_path / 'model.safetensors').write_bytes(weights)
    (tmp_path / 'config.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='SHA-256'): worker.verify_model(tmp_path)


def test_local_child_uses_fixed_cli_and_offline_timestral(tmp_path, monkeypatch):
    captured = {}
    class Child:
        returncode = 0
        def __init__(self, command, **kwargs): captured.update(command=command, **kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, input=None, timeout=None):
            captured['payload'] = json.loads(input)
            return '{"median": [0.2]}', ''
    monkeypatch.setattr('app.local_runtime.subprocess.Popen', Child)
    result = run_local('timestral', {'context': [.1] * 192}, {'_data_dir': str(tmp_path), 'ml_threads': 1})
    assert result['median'] == [.2]
    assert captured['command'][1:] == ['-m', 'app.local_worker', 'timestral']
    assert captured['env']['HF_HUB_OFFLINE'] == '1'
    assert captured['env']['OMP_NUM_THREADS'] == '1'
    assert captured['payload']['threads'] == 1
    assert not BUSY.locked()


def test_disable_kills_running_child_and_releases_slot(tmp_path, monkeypatch):
    events = []
    class Child:
        returncode = 0
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, input=None, timeout=None):
            if not events: raise subprocess.TimeoutExpired('worker', 1)
            return '', ''
        def kill(self): events.append('killed')
    monkeypatch.setattr('app.local_runtime.subprocess.Popen', Child)
    with pytest.raises(ValueError, match='gestopt'):
        run_local('epex', {}, {'_data_dir': str(tmp_path), '_cancel': lambda: True})
    assert events == ['killed'] and not BUSY.locked()


def test_only_one_ml_operation_at_once(tmp_path, monkeypatch):
    BUSY.acquire()
    try:
        monkeypatch.setattr('app.local_runtime.subprocess.Popen', lambda *a, **k: pytest.fail('second model started'))
        with pytest.raises(ValueError, match='al bezig'):
            run_local('epex', {}, {'_data_dir': str(tmp_path)})
    finally:
        BUSY.release()


def test_download_http_rejects_missing_consent_without_starting_job(tmp_path):
    app = AppService(store=Store(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        root = f'http://127.0.0.1:{server.server_port}'
        with urlopen(root + '/api/timestral') as response:
            assert json.load(response)['ready'] is False
        request = Request(root + '/api/timestral/download', data=b'{"confirmed":false}',
                          headers={'Content-Type': 'application/json'})
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 400
        assert app._download_thread is None
    finally:
        server.shutdown(); server.server_close(); thread.join()
