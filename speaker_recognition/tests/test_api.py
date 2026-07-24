from __future__ import annotations

import base64
import os
import time
from dataclasses import replace

import numpy as np

os.environ["DATA_DIR"] = "/tmp/speaker-recognition-tests-unused"

from fastapi.testclient import TestClient

import app.api as api
from app.audio_processor import ProcessedAudioResult
from app.recognizer import SpeakerRecognizer
from conftest import FakeEncoder, audio


def mixed_speakers_audio():
    pcm = np.concatenate(
        (
            np.full(16000, 12000, dtype="<i2"),
            np.zeros(8000, dtype="<i2"),
            np.full(16000, -12000, dtype="<i2"),
        )
    )
    return {
        "audio_data": base64.b64encode(pcm.tobytes()).decode(),
        "sample_rate": 16000,
    }


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


def test_streaming_analysis_consumes_chunks_and_persists_drained_audio(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    monkeypatch.setattr(
        api,
        "settings",
        replace(api.settings, audio_processing_backend="df3_streaming"),
    )
    monkeypatch.setitem(
        api._policy, "audio_processing_backend", "df3_streaming"
    )
    api.recognizer = SpeakerRecognizer(
        tmp_path,
        0.8,
        10,
        FakeEncoder,
        lambda wav, _rate: wav,
        audio_processing_backend="df3_streaming",
    )
    received = bytearray()

    def denoise_stream(chunks, sample_rate, *, timeout_seconds=12):
        assert sample_rate == 16000
        for chunk in chunks:
            received.extend(chunk)
        return ProcessedAudioResult(
            denoised_pcm=bytes(received),
            isolated_pcm=None,
            sample_rate=sample_rate,
            stages={
                "denoise": "ready",
                "model": "warm",
                "streaming": "drained",
            },
            timings={
                "audio_processing_ms": 40.0,
                "post_utterance_ms": 8.0,
            },
            quality={
                "denoised_passed": True,
                "stateful": True,
                "duration_delta_ms": 0.0,
            },
        )

    monkeypatch.setattr(
        api.recognizer, "denoise_audio_stream", denoise_stream
    )
    pcm = b"\x10\x00" * 1600
    headers = {
        "X-Ingress-Path": "/api/hassio_ingress/test",
        "X-Audio-Sample-Rate": "16000",
        "X-STT-Entity-ID": "stt.source",
    }
    with TestClient(api.app, headers=headers) as client:
        client.post(
            "/api/enroll",
            json={
                "speaker_name": "Alice",
                "samples": [{"audio": audio(12000).model_dump()}],
            },
        )
        response = client.post(
            "/api/analyze-stream",
            content=iter([pcm[:999], pcm[999:]]),
        )

    assert response.status_code == 200
    result = response.json()
    assert received == pcm
    assert result["denoised_available"] is True
    assert result["processing_stages"]["streaming"] == "drained"
    assert result["processing_quality"]["duration_delta_ms"] == 0.0
    assert result["timings"]["post_utterance_ms"] == 8.0


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


def test_analysis_persists_multiple_speakers_without_blocking_known_voices(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    monkeypatch.setitem(api._policy, "unknown_speaker_policy", "block")
    monkeypatch.setitem(api._policy, "min_margin", 0.1)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
    with TestClient(api.app, headers=headers) as client:
        client.post(
            "/api/enroll",
            json={
                "speaker_name": "Testspreker A",
                "person_entity_id": "person.test_speaker_a",
                "samples": [{"audio": audio(12000).model_dump()}],
            },
        )
        client.post(
            "/api/enroll",
            json={
                "speaker_name": "Testspreker B",
                "person_entity_id": "person.test_speaker_b",
                "samples": [{"audio": audio(-12000).model_dump()}],
            },
        )

        response = client.post(
            "/api/analyze",
            json={"audio": mixed_speakers_audio(), "source": "test"},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["outcome"] == "multiple_speakers"
        assert result["multiple_speakers"] is True
        assert result["matched"] is False
        assert result["blocked"] is False
        assert [item["speaker_name"] for item in result["detected_speakers"]] == [
            "Testspreker A",
            "Testspreker B",
        ]
        ephemeral = client.post(
            "/api/recognize", json={"audio": mixed_speakers_audio()}
        ).json()
        assert ephemeral["outcome"] == "multiple_speakers"
        assert len(ephemeral["detected_speakers"]) == 2
        detail = client.get(
            f"/api/analysis/{result['recording_id']}"
        ).json()
        assert detail["outcome"] == "multiple_speakers"
        assert detail["detected_speakers"] == result["detected_speakers"]
        assert client.get(
            f"/api/analysis?speaker_id={result['detected_speakers'][0]['speaker_id']}"
        ).json()["total"] == 1


def test_reanalyze_uses_current_profiles_and_preserves_pipeline_history(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    monkeypatch.setitem(api._policy, "unknown_speaker_policy", "allow")
    monkeypatch.setitem(api._policy, "min_margin", 0.0)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
    sample = audio(12000)
    with TestClient(api.app, headers=headers) as client:
        recording = api.recognizer.catalog.create_recording(
            api.recognizer._decode_pcm_bytes(sample),
            sample.sample_rate,
            source="test",
            transcript="Bewaard transcript",
            outcome="unmatched",
            conversation_forwarded=True,
            timings={"stt_ms": 12.0, "total_ms": 30.0},
            labels={
                "person_entity_id": "person.historisch",
                "conversation_reason": "oude context",
            },
        )
        api.recognizer.catalog.save_audio_variant(
            recording["id"],
            "denoised",
            api.recognizer._decode_pcm_bytes(sample),
            sample.sample_rate,
        )
        enrolled = client.post(
            "/api/enroll",
            json={
                "speaker_name": "Alice",
                "person_entity_id": "person.alice",
                "samples": [{"audio": sample.model_dump()}],
            },
        ).json()["speaker"]
        api.recognizer.catalog.set_calibration(
            0.7, 0.1, {"source": "test"}
        )

        response = client.post(
            f"/api/analysis/{recording['id']}/reanalyze"
        )

        assert response.status_code == 200
        detail = response.json()
        assert detail["outcome"] == "matched"
        assert detail["speaker_id"] == enrolled["id"]
        assert detail["speaker_name"] == "Alice"
        assert detail["recognized_person_entity_id"] == "person.alice"
        assert detail["conversation_person_entity_id"] == "person.historisch"
        assert detail["person_entity_id"] == "person.historisch"
        assert detail["threshold"] == 0.7
        assert detail["margin"] >= 0.1
        assert detail["threshold_source"] == "calibration"
        assert detail["transcript"] == "Bewaard transcript"
        assert detail["conversation_forwarded"] is True
        assert detail["denoised_available"] is True
        assert detail["timings"]["stt_ms"] == 12.0
        assert detail["timings"]["total_ms"] == 30.0
        assert detail["timings"]["recognition_ms"] >= 0
        assert detail["labels"]["conversation_reason"] == "oude context"
        assert client.post(
            f"/api/recordings/{recording['id']}/reanalyze"
        ).status_code == 200


def test_reanalyze_applies_block_policy_without_changing_other_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    monkeypatch.setitem(api._policy, "unknown_speaker_policy", "block")
    monkeypatch.setitem(api._policy, "min_margin", 0.0)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
    with TestClient(api.app, headers=headers) as client:
        client.post(
            "/api/enroll",
            json={
                "speaker_name": "Alice",
                "samples": [{"audio": audio(12000).model_dump()}],
            },
        )
        opposite = audio(-12000)
        recording = api.recognizer.catalog.create_recording(
            api.recognizer._decode_pcm_bytes(opposite),
            opposite.sample_rate,
            source="pipeline",
            transcript="Niet wijzigen",
            outcome="matched",
        )

        response = client.post(
            f"/api/analysis/{recording['id']}/reanalyze"
        )

        assert response.status_code == 200
        detail = response.json()
        assert detail["outcome"] == "blocked"
        assert detail["blocked"] is True
        assert detail["speaker_id"] is None
        assert detail["speaker_name"] is None
        assert detail["transcript"] == "Niet wijzigen"


def test_reanalyze_errors_leave_existing_result_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
    sample = audio(12000)
    with TestClient(api.app, headers=headers) as client:
        recording = api.recognizer.catalog.create_recording(
            api.recognizer._decode_pcm_bytes(sample),
            sample.sample_rate,
            source="test",
            transcript="Blijft staan",
            outcome="error",
            confidence=0.25,
        )
        response = client.post(
            f"/api/analysis/{recording['id']}/reanalyze"
        )
        assert response.status_code == 409
        unchanged = api.recognizer.catalog.get_recording(recording["id"])
        assert unchanged["outcome"] == "error"
        assert unchanged["confidence"] == 0.25
        assert unchanged["transcript"] == "Blijft staan"

        assert client.post(
            "/api/analysis/0" + "/reanalyze"
        ).status_code == 404
        original = api.recognizer.catalog.audio_path(
            recording["id"], "original"
        )
        assert original is not None
        original.unlink()
        assert client.post(
            f"/api/analysis/{recording['id']}/reanalyze"
        ).status_code == 404


def test_target_audio_process_is_queued_and_exposes_new_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)

    def denoise_audio(
        payload,
        *,
        timeout_seconds=12,
        priority="live",
    ):
        assert priority == "analysis"
        raw = __import__("base64").b64decode(payload.audio_data)
        return {
            "denoised_pcm": raw,
            "sample_rate": payload.sample_rate,
            "stages": {"denoise": "complete", "model": "warm"},
            "timings": {
                "denoise_ms": 1,
                "audio_processing_ms": 300,
                "total_ms": 300,
            },
            "quality": {"status": "accepted"},
        }

    api.recognizer.denoise_audio = denoise_audio
    with TestClient(api.app, headers={"X-Ingress-Path": "/api/hassio_ingress/test"}) as client:
        speaker = client.post("/api/enroll", json={"speaker_name": "Alice", "samples": [{"audio": audio(12000).model_dump()}]}).json()["speaker"]
        recording = client.post("/api/analyze", json={"audio": audio(11000).model_dump(), "source": "test"}).json()
        api.recognizer.catalog.update_recording(
            recording["recording_id"],
            timings={"stt_ms": 800, "total_ms": 1000},
        )
        response = client.post(f"/api/analysis/{recording['recording_id']}/process", json={})
        assert response.status_code == 202
        for _ in range(20):
            detail = client.get(f"/api/analysis/{recording['recording_id']}").json()
            if detail["processing_status"] == "complete":
                break
            time.sleep(0.01)
        assert detail["processing_status"] == "complete"
        assert detail["denoised_available"] is True
        assert detail["isolated_available"] is False
        assert detail["timings"]["baseline_total_ms"] == 1000
        assert detail["timings"]["audio_processing_ms"] == 300
        assert detail["timings"]["total_ms"] == 1300
        assert detail["processing_backend"] == "df2_batch"
        assert detail["processing_timings"]["audio_processing_ms"] == 300
        rerun = client.post(
            f"/api/analysis/{recording['recording_id']}/process",
            json={"backend": "df3_streaming"},
        )
        assert rerun.status_code == 409
        reset = client.delete(
            f"/api/analysis/{recording['recording_id']}/processing"
        )
        assert reset.status_code == 200
        reset_detail = reset.json()
        assert reset_detail["denoised_available"] is False
        assert reset_detail["processing_status"] == "idle"
        assert reset_detail["processing_backend"] is None
        assert reset_detail["processing_timings"] == {}
        assert reset_detail["timings"] == {
            "stt_ms": 800,
            "total_ms": 1000,
        }
        assert client.get(f"/api/analysis/{recording['recording_id']}/audio?variant=isolated").status_code == 404


def test_manual_df3_streams_persisted_wav_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )
    chunks_seen: list[bytes] = []

    def denoise_stream(chunks, sample_rate, *, timeout_seconds=12):
        chunks_seen.extend(chunks)
        pcm = b"".join(chunks_seen)
        return ProcessedAudioResult(
            denoised_pcm=pcm,
            isolated_pcm=None,
            sample_rate=sample_rate,
            stages={"streaming": "drained"},
            timings={
                "audio_processing_ms": 20,
                "post_utterance_ms": 7,
            },
            quality={"stateful": True, "duration_preserved": True},
        )

    monkeypatch.setattr(
        api.recognizer, "denoise_audio_stream", denoise_stream
    )
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
    with TestClient(api.app, headers=headers) as client:
        client.post(
            "/api/enroll",
            json={
                "speaker_name": "Alice",
                "samples": [{"audio": audio(12000).model_dump()}],
            },
        )
        recording = client.post(
            "/api/analyze",
            json={
                "audio": audio(11000, seconds=1.1).model_dump(),
                "source": "test",
                "extraction_mode": "off",
            },
        ).json()
        response = client.post(
            f"/api/analysis/{recording['recording_id']}/process",
            json={"backend": "df3_streaming"},
        )
        assert response.status_code == 202
        for _ in range(40):
            detail = client.get(
                f"/api/analysis/{recording['recording_id']}"
            ).json()
            if detail["processing_status"] == "complete":
                break
            time.sleep(0.01)

    assert len(chunks_seen) > 1
    assert all(len(chunk) <= 640 for chunk in chunks_seen)
    assert detail["processing_backend"] == "df3_streaming"
    assert detail["denoised_available"] is True
    assert detail["timings"]["post_utterance_ms"] == 7


def test_pipeline_policy_persists_runtime_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )
    with TestClient(
        api.app, headers={"X-Ingress-Path": "/api/hassio_ingress/test"}
    ) as client:
        response = client.patch(
            "/api/pipeline-policy",
            json={
                "audio_processing_backend": "df3_streaming",
                "retention_days": 21,
                "max_storage_bytes": 123456789,
            },
        )
        assert response.status_code == 200
        policy = response.json()
        assert policy["audio_processing_backend"] == "df3_streaming"
        assert policy["retention_days"] == 21
        assert policy["max_storage_bytes"] == 123456789
        assert api.recognizer.catalog.retention_days == 21
        assert api.recognizer.catalog.max_storage_bytes == 123456789
        assert (
            api.recognizer.catalog.get_setting("pipeline_policy")[
                "audio_processing_backend"
            ]
            == "df3_streaming"
        )


def test_processing_reset_rejects_an_active_job(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    api.recognizer = SpeakerRecognizer(
        tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav
    )

    class ActiveJob:
        @staticmethod
        def done():
            return False

    with TestClient(
        api.app, headers={"X-Ingress-Path": "/api/hassio_ingress/test"}
    ) as client:
        recording = api.recognizer.catalog.create_recording(
            b"\x01\x00" * 16_000, 16_000, source="test"
        )
        api.processing_tasks[recording["id"]] = ActiveJob()
        try:
            response = client.delete(
                f"/api/analysis/{recording['id']}/processing"
            )
        finally:
            api.processing_tasks.pop(recording["id"], None)

    assert response.status_code == 409
