from __future__ import annotations

import os

os.environ["DATA_DIR"] = "/tmp/speaker-recognition-tests-unused"

from fastapi.testclient import TestClient

import app.api as api
from app.recognizer import SpeakerRecognizer
from conftest import FakeEncoder, audio


def test_api_contract_and_ingress_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    ingress_headers = {"X-Ingress-Path": "/api/hassio_ingress/test-token"}
    with TestClient(api.app, headers=ingress_headers) as client:
        assert client.get("/health").json()["ready"] is True
        assert client.get("/api/speakers").json() == []
        payload = {
            "speaker_name": "Alice",
            "samples": [{"audio": audio(12000).model_dump()}],
            "replace": False,
        }
        enrolled = client.post("/api/enroll", json=payload)
        assert enrolled.status_code == 200
        speaker_id = enrolled.json()["speaker"]["id"]
        result = client.post("/api/recognize", json={"audio": audio(10000).model_dump()})
        assert result.json()["speaker"]["name"] == "Alice"
        assert client.delete(f"/api/speakers/{speaker_id}").status_code == 204


def test_direct_api_is_forbidden_without_token(tmp_path):
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/speakers").status_code == 403
        assert client.get(
            "/api/speakers", headers={"X-Ingress-Path": "/forged"}
        ).status_code == 403


def test_companion_token_authenticates_direct_api(tmp_path):
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    with TestClient(
        api.app, headers={"Authorization": f"Bearer {api.settings.companion_token}"}
    ) as client:
        assert client.get("/api/speakers").status_code == 200

    with TestClient(
        api.app, headers={"Authorization": api.settings.companion_token}
    ) as client:
        assert client.get("/api/speakers").status_code == 403


def test_ingress_prefix_is_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    path = "/api/hassio_ingress/random-token"
    with TestClient(api.app, headers={"X-Ingress-Path": path}) as client:
        document = client.get("/").text
        assert f'{path}/assets/styles.css' in document
        assert f'content="{path}/"' in document
        assert 'href="/assets/styles.css"' not in document


def test_chunked_request_size_is_limited(tmp_path, monkeypatch):
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    monkeypatch.setattr(api, "MAX_REQUEST_BYTES", 16)
    headers = {"Authorization": f"Bearer {api.settings.companion_token}"}
    with TestClient(api.app) as client:
        response = client.post(
            "/api/enroll", content=iter([b"1234567890", b"abcdefghij"]), headers=headers
        )
        assert response.status_code == 413
