from __future__ import annotations

import hashlib
import io
import json

import app.companion as companion


class JsonResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_installs_and_updates_companion_integration(tmp_path, monkeypatch):
    source = tmp_path / "bundle" / companion.DOMAIN
    source.mkdir(parents=True)
    (source / "manifest.json").write_text('{"version":"1"}', encoding="utf-8")
    config_root = tmp_path / "homeassistant"
    monkeypatch.setattr(companion, "SOURCE", source)
    monkeypatch.setattr(companion, "CONFIG_ROOT", config_root)

    assert companion.install_integration() is True
    target = config_root / "custom_components" / companion.DOMAIN
    assert json.loads((target / "manifest.json").read_text()) == {"version": "1"}
    assert companion.install_integration() is False

    (source / "manifest.json").write_text('{"version":"2"}', encoding="utf-8")
    assert companion.install_integration() is True
    assert json.loads((target / "manifest.json").read_text()) == {"version": "2"}
    expected = hashlib.sha256(b"manifest.json" + b'{"version":"2"}').hexdigest()
    assert (target / companion.MARKER).read_text() == expected


def test_preserves_an_unmanaged_existing_integration(tmp_path, monkeypatch):
    source = tmp_path / "bundle" / companion.DOMAIN
    source.mkdir(parents=True)
    (source / "manifest.json").write_text('{"version":"2"}', encoding="utf-8")
    config_root = tmp_path / "homeassistant"
    target = config_root / "custom_components" / companion.DOMAIN
    target.mkdir(parents=True)
    (target / "manifest.json").write_text('{"version":"manual"}', encoding="utf-8")
    monkeypatch.setattr(companion, "SOURCE", source)
    monkeypatch.setattr(companion, "CONFIG_ROOT", config_root)

    assert companion.install_integration() is True
    backup = target.with_name(f"{companion.DOMAIN}.pre-app-backup")
    assert json.loads((backup / "manifest.json").read_text()) == {"version": "manual"}
    assert json.loads((target / "manifest.json").read_text()) == {"version": "2"}


def test_publishes_internal_hostname_and_secret(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/addons/self/info"):
            payload = {"result": "ok", "data": {"hostname": "abc-speaker-recognition", "slug": "abc_speaker_recognition"}}
        else:
            payload = {"result": "ok", "data": {"uuid": "discovery-id"}}
        return JsonResponse(json.dumps(payload).encode())

    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
    monkeypatch.setattr(companion.urllib.request, "urlopen", fake_urlopen)
    assert companion.publish_discovery("companion-secret", 8099)
    discovery = json.loads(requests[1][0].data)
    assert discovery == {
        "service": companion.DOMAIN,
        "config": {
            "host": "abc-speaker-recognition",
            "port": 8099,
            "token": "companion-secret",
            "instance_id": "abc_speaker_recognition",
        },
    }
    assert requests[1][0].get_header("Authorization") == "Bearer supervisor-secret"
