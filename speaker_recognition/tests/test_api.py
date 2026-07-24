from __future__ import annotations

import os
import time

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
            "person_entity_id": "person.alice",
            "samples": [{"audio": audio(12000).model_dump()}],
            "replace": False,
        }
        enrolled = client.post("/api/enroll", json=payload)
        assert enrolled.status_code == 200
        speaker_id = enrolled.json()["speaker"]["id"]
        assert enrolled.json()["speaker"]["person_entity_id"] == "person.alice"
        result = client.post("/api/recognize", json={"audio": audio(10000).model_dump()})
        assert result.json()["speaker"]["name"] == "Alice"
        payload["person_entity_id"] = None
        cleared = client.post("/api/enroll", json=payload)
        assert cleared.status_code == 200
        assert cleared.json()["speaker"]["person_entity_id"] is None
        assert client.delete(f"/api/speakers/{speaker_id}").status_code == 204


def test_person_endpoint_uses_home_assistant_entities(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    monkeypatch.setattr(
        api.home_assistant,
        "persons",
        lambda: [{"entity_id": "person.alice", "name": "Alice"}],
    )
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    with TestClient(api.app, headers={"X-Ingress-Path": "/api/hassio_ingress/test"}) as client:
        response = client.get("/api/home-assistant-persons")
        assert response.status_code == 200
        assert response.json() == [{"entity_id": "person.alice", "name": "Alice"}]


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


def test_v2_analysis_filters_bulk_delete_and_public_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
    with TestClient(api.app, headers=headers) as client:
        enrolled = client.post(
            "/api/enroll",
            json={
                "speaker_name": "Alice",
                "samples": [{"audio": audio(12000).model_dump()}],
            },
        ).json()["speaker"]
        samples = client.get(f"/api/speakers/{enrolled['id']}/samples").json()
        assert samples and "path" not in samples[0]

        test_recording_id = None
        for source in ("test", "pipeline"):
            response = client.post(
                "/api/analyze",
                json={"audio": audio(11000).model_dump(), "source": source},
            )
            assert response.status_code == 200
            assert "original_path" not in response.json()
            assert response.json()["original_available"] is True
            if source == "test":
                test_recording_id = response.json()["recording_id"]

        promoted = client.post(
            f"/api/analysis/{test_recording_id}/promote",
            json={"speaker_id": enrolled["id"], "start_seconds": 0, "end_seconds": 1},
        )
        assert promoted.status_code == 200
        assert promoted.json()["speaker"]["sample_count"] == 2

        assert client.get("/api/analysis?source=test").json()["total"] == 1
        assert client.get("/api/analysis?since=2999-01-01T00:00:00Z").json()["total"] == 0
        deleted = client.post(
            "/api/analysis/delete",
            json={"ids": None, "filters": {"source": "test"}, "all_filtered": True},
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": 1}
        assert client.get("/api/analysis").json()["total"] == 1


def test_target_audio_process_is_queued_and_exposes_new_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)

    def process_target_audio(
        payload,
        _speaker_id,
        *,
        timeout_seconds=12,
        priority="live",
        min_margin=0.0,
    ):
        assert priority == "analysis"
        raw = __import__("base64").b64decode(payload.audio_data)
        return {
            "denoised_pcm": raw,
            "isolated_pcm": raw,
            "sample_rate": payload.sample_rate,
            "stages": {"denoise": "complete", "isolation": "complete"},
            "timings": {
                "denoise_ms": 1,
                "isolation_ms": 2,
                "audio_processing_ms": 300,
                "total_ms": 300,
            },
            "quality": {"status": "accepted"},
        }

    api.recognizer.process_target_audio = process_target_audio
    with TestClient(api.app, headers={"X-Ingress-Path": "/api/hassio_ingress/test"}) as client:
        speaker = client.post("/api/enroll", json={"speaker_name": "Alice", "samples": [{"audio": audio(12000).model_dump()}]}).json()["speaker"]
        recording = client.post("/api/analyze", json={"audio": audio(11000).model_dump(), "source": "test"}).json()
        api.recognizer.catalog.update_recording(
            recording["recording_id"],
            timings={"stt_ms": 800, "total_ms": 1000},
        )
        response = client.post(f"/api/analysis/{recording['recording_id']}/process", json={"speaker_id": speaker["id"]})
        assert response.status_code == 202
        for _ in range(20):
            detail = client.get(f"/api/analysis/{recording['recording_id']}").json()
            if detail["processing_status"] == "complete":
                break
            time.sleep(0.01)
        assert detail["processing_status"] == "complete"
        assert detail["denoised_available"] is True
        assert detail["isolated_available"] is True
        assert detail["timings"]["baseline_total_ms"] == 1000
        assert detail["timings"]["audio_processing_ms"] == 300
        assert detail["timings"]["total_ms"] == 1300
        assert client.get(f"/api/analysis/{recording['recording_id']}/audio?variant=isolated").status_code == 200
        # Existing clients still use extracted; it resolves to isolated first.
        assert client.get(f"/api/analysis/{recording['recording_id']}/audio?variant=extracted").status_code == 200
