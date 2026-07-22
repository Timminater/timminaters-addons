from __future__ import annotations

import os

os.environ["DATA_DIR"] = "/tmp/speaker-recognition-tests-unused"

from fastapi.testclient import TestClient

import app.api as api
from app.recognizer import SpeakerRecognizer
from conftest import FakeEncoder, audio


def test_api_contract_and_ingress_auth(tmp_path):
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


def test_ingress_prefix_is_rendered(tmp_path):
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    path = "/api/hassio_ingress/random-token"
    with TestClient(api.app, headers={"X-Ingress-Path": path}) as client:
        document = client.get("/").text
        assert f'{path}/assets/styles.css' in document
        assert f'content="{path}/"' in document
        assert 'href="/assets/styles.css"' not in document
