from datetime import datetime, timezone
import hashlib
import json

import pytest

from app.backend import AppService
from app.storage import Store
from app.timestral_models import MODELS, model_spec
from app.local_runtime import run_local, start_download, timestral_status
from app.methods import timestral_version, timestral_forecast


def test_new_install_defaults_to_28m_and_old_settings_keep_209m(tmp_path):
    app = AppService(store=Store(tmp_path))
    assert app.get_settings()['timestral_model'] == '28M'
    app.put_settings({'timestral_model': '5M'})
    assert AppService(store=Store(tmp_path)).get_settings()['timestral_model'] == '5M'
    old = AppService(store=Store(tmp_path / 'old'))
    old.store.set_settings({'timestral_context_days': 28})
    assert old.get_settings()['timestral_model'] == '209M'
    assert old.put_settings({})['timestral_model'] == '209M'


def test_existing_model_folder_keeps_209m_and_invalid_models_rejected(tmp_path):
    (tmp_path / 'models' / 'timestral-209m').mkdir(parents=True)
    app = AppService(store=Store(tmp_path))
    assert app.get_settings()['timestral_model'] == '209M'
    for value in ('1B', '../209M', 'https://example.com', None, {}, []):
        with pytest.raises(ValueError, match='Kies Timestral'):
            app.put_settings({'timestral_model': value})
        with pytest.raises(ValueError): timestral_status(tmp_path, value)


def test_versions_are_distinct_and_legacy_scores_are_preserved():
    versions = {timestral_version({'timestral_model': size}) for size in MODELS}
    assert len(versions) == 4
    old_digest = hashlib.sha256(json.dumps([28]).encode()).hexdigest()[:12]
    assert timestral_version({'timestral_model': '209M'}) == 'timestral-local-209m-1:9874d252ab0a:' + old_digest


@pytest.mark.parametrize('size', list(MODELS))
def test_download_pins_requested_variant_and_does_not_change_saved_selection(tmp_path, monkeypatch, size):
    app = AppService(store=Store(tmp_path))
    app.put_settings({'timestral_model': '28M'})
    calls = []
    def worker(method, payload, settings, **kwargs):
        calls.append((method, settings['timestral_model']))
        return {'ready': True}
    monkeypatch.setattr('app.local_runtime.run_local', worker)
    start_download(app, True, size)
    app._download_thread.join(timeout=3)
    assert calls == [('timestral_download', size)]
    assert app.get_settings()['timestral_model'] == '28M'


@pytest.mark.parametrize('size', list(MODELS))
def test_download_verifies_selected_files_in_own_directory(tmp_path, monkeypatch, size):
    import app.local_worker as worker
    from unittest.mock import Mock
    import sys
    spec = model_spec(size)
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    expected = tmp_path / 'models' / spec['folder']
    download = Mock()
    monkeypatch.setitem(sys.modules, 'huggingface_hub', type('Hub', (), {'snapshot_download': download}))
    verified = []
    monkeypatch.setattr(worker, 'verify_model', lambda folder, selected: verified.append((folder, selected)))
    worker.download_timestral({'model_size': size})
    assert download.call_args.args == (spec['repo'],)
    assert download.call_args.kwargs['revision'] == spec['revision']
    assert download.call_args.kwargs['local_dir'] == expected
    assert verified == [(expected, spec)]
    assert json.loads((expected / 'verified.json').read_text())['sha256'] == spec['sha256']
    assert timestral_status(tmp_path, size)['download_bytes'] == spec['bytes']
    assert timestral_status(tmp_path, size)['ready'] is False  # mock downloaded no weights


def test_worker_rejects_another_models_files(tmp_path, monkeypatch):
    import app.local_worker as worker
    weights, config = b'weights', b'config'
    (tmp_path / 'model.safetensors').write_bytes(weights)
    (tmp_path / 'config.json').write_bytes(config)
    (tmp_path / 'LICENSE').write_text('license')
    spec = {'bytes': len(weights), 'sha256': hashlib.sha256(weights).hexdigest(),
            'config_sha256': hashlib.sha256(config).hexdigest()}
    worker.verify_model(tmp_path, spec)
    with pytest.raises(ValueError, match='SHA-256'):
        worker.verify_model(tmp_path, {**spec, 'sha256': '0' * 64})


def test_model_switch_cancels_old_inference(tmp_path):
    app = AppService(store=Store(tmp_path))
    app.put_settings({'timestral_model': '209M', 'forecast_methods': {'local': True, 'v4': False, 'epex': False, 'timestral': True}})
    runtime = app._runtime_settings(app.get_settings(), 'timestral')
    assert not runtime['_cancel']()
    app.put_settings({'timestral_model': '5M'})
    assert runtime['_cancel']()


@pytest.mark.parametrize('size', list(MODELS))
def test_adapter_checks_selected_model_identity_and_keeps_negative_prices(size):
    from datetime import timedelta
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    rows = [{'start_utc': (now - timedelta(minutes=15 * (192-i))).isoformat(),
             'price': .1, 'published_at': (now - timedelta(seconds=1)).isoformat()}
            for i in range(192)]
    spec = model_spec(size)
    response = {'model': spec['repo'], 'revision': spec['revision'],
                'median': [-.1] * 4, 'lower': [-.2] * 4, 'upper': [.1] * 4}
    settings = {'timestral_model': size}
    result = timestral_forecast(rows, now, settings, 4, runner=lambda *a, **kw: response)
    assert result.points[0].price == -.1
    assert result.model_version == timestral_version(settings)
    with pytest.raises(ValueError, match='ingestelde model'):
        timestral_forecast(rows, now, settings, 4,
                          runner=lambda *a, **kw: {**response, 'revision': 'wrong'})


def test_child_receives_only_the_saved_model_selection(tmp_path, monkeypatch):
    captured = {}
    class Child:
        returncode = 0
        def __init__(self, command, **kwargs): captured.update(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, input=None, timeout=None):
            captured['request'] = json.loads(input)
            return '{}', ''
    monkeypatch.setattr('app.local_runtime.subprocess.Popen', Child)
    run_local('timestral', {'model_size': '../outside'},
              {'timestral_model': '5M', '_data_dir': str(tmp_path)})
    assert captured['request']['model_size'] == '5M'
    assert captured['env']['HF_HUB_OFFLINE'] == '1'
