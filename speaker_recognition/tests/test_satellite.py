from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from datetime import timedelta

from fastapi.testclient import TestClient

import app.api as api
from app.models import AssistSatelliteInfo, AudioInput
from app.recognizer import SpeakerRecognizer
from app.satellite import HomeAssistantClient, SatelliteEnrollmentCoordinator
from conftest import FakeEncoder


def run(coroutine):
    return asyncio.run(coroutine)


def test_coordinator_is_atomic_and_terminal_states_are_monotonic():
    coordinator = SatelliteEnrollmentCoordinator()
    armed = run(coordinator.arm("assist_satellite.voice"))

    async def claim_many():
        return await asyncio.gather(*(coordinator.claim() for _ in range(20)))

    claims = run(claim_many())
    assert sum(item is not None for item in claims) == 1

    sample = AudioInput(audio_data="AAE=", sample_rate=16000)
    completed = run(coordinator.complete(armed.id, sample))
    assert completed.status == "complete"
    run(coordinator.fail(armed.id, "late network failure"))
    run(coordinator.cancel(armed.id))
    assert run(coordinator.get(armed.id)).status == "complete"


def test_cancelled_session_cannot_be_completed_or_failed():
    coordinator = SatelliteEnrollmentCoordinator()
    armed = run(coordinator.arm("assist_satellite.voice"))
    run(coordinator.claim())
    run(coordinator.cancel(armed.id))
    try:
        run(coordinator.complete(armed.id, AudioInput(audio_data="AAE=", sample_rate=16000)))
    except ValueError:
        pass
    else:
        raise AssertionError("cancelled capture was completed")
    run(coordinator.fail(armed.id, "late failure"))
    assert run(coordinator.get(armed.id)).status == "cancelled"


def test_session_expires_and_discards_audio(monkeypatch):
    coordinator = SatelliteEnrollmentCoordinator()
    now = coordinator._now()
    monkeypatch.setattr(coordinator, "_now", lambda: now)
    armed = run(coordinator.arm("assist_satellite.voice"))
    monkeypatch.setattr(coordinator, "_now", lambda: now + timedelta(seconds=45))
    expired = run(coordinator.get(armed.id))
    assert expired.status == "expired"
    assert expired.audio is None


def test_home_assistant_client_filters_and_uses_stt_only_question(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    states = [
        {
            "entity_id": "assist_satellite.voice_b",
            "state": "idle",
            "attributes": {"friendly_name": "Voice B", "supported_features": 2},
        },
        {
            "entity_id": "assist_satellite.voice_a",
            "state": "idle",
            "attributes": {"friendly_name": "Voice A", "supported_features": "2"},
        },
        {
            "entity_id": "assist_satellite.invalid",
            "state": "idle",
            "attributes": {"supported_features": None},
        },
    ]

    def urlopen(request: urllib.request.Request, timeout: int):
        calls.append((request.full_url, request.method, request.data, timeout))
        return Response(states if request.method == "GET" else [])

    monkeypatch.setenv("SUPERVISOR_TOKEN", "secret")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = HomeAssistantClient("http://supervisor")
    assert [item.name for item in client.satellites()] == ["Voice A", "Voice B"]
    client.ask_for_enrollment_sample("assist_satellite.voice_a")
    url, method, body, timeout = calls[-1]
    assert url.endswith("/services/assist_satellite/ask_question?return_response")
    assert "start_conversation" not in url
    assert method == "POST"
    assert timeout == 70
    assert json.loads(body)["entity_id"] == "assist_satellite.voice_a"
    client.confirm_enrollment_sample("assist_satellite.voice_a")
    url, method, body, timeout = calls[-1]
    assert url.endswith("/services/assist_satellite/announce")
    assert method == "POST"
    assert timeout == 70
    assert json.loads(body) == {
        "entity_id": "assist_satellite.voice_a",
        "message": "Opname voltooid.",
        "preannounce": False,
    }


def test_completed_prompt_confirms_and_resets_satellite(monkeypatch):
    class CompletedHomeAssistant:
        def __init__(self):
            self.asked = []
            self.confirmed = []

        def ask_for_enrollment_sample(self, entity_id):
            self.asked.append(entity_id)

        def confirm_enrollment_sample(self, entity_id):
            self.confirmed.append(entity_id)

    fake = CompletedHomeAssistant()
    coordinator = SatelliteEnrollmentCoordinator()
    monkeypatch.setattr(api, "home_assistant", fake)
    monkeypatch.setattr(api, "satellite_enrollment", coordinator)

    async def scenario():
        session = await coordinator.arm("assist_satellite.voice")
        await coordinator.claim()
        await coordinator.complete(
            session.id, AudioInput(audio_data="AAE=", sample_rate=16000)
        )
        await api._run_satellite_prompt(session.id, "assist_satellite.voice")

    run(scenario())

    assert fake.asked == ["assist_satellite.voice"]
    assert fake.confirmed == ["assist_satellite.voice"]


class FakeHomeAssistant:
    def __init__(self):
        self.states = {"assist_satellite.voice": "idle"}
        self.asked = []
        self.release = threading.Event()

    def satellites(self):
        return [
            AssistSatelliteInfo(entity_id=entity_id, name="Voice", state=state)
            for entity_id, state in self.states.items()
        ]

    def ask_for_enrollment_sample(self, entity_id):
        self.asked.append(entity_id)
        self.release.wait(timeout=2)


def test_satellite_api_requires_auth_and_rejects_ambiguous_claim(monkeypatch, tmp_path):
    fake = FakeHomeAssistant()
    monkeypatch.setattr(api, "home_assistant", fake)
    api.recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, FakeEncoder, lambda wav, _rate: wav)
    api.satellite_enrollment = SatelliteEnrollmentCoordinator()
    with TestClient(api.app) as client:
        assert client.get("/api/assist-satellites").status_code == 403

    monkeypatch.setattr(api, "_is_supervisor_request", lambda _request: True)
    headers = {"X-Ingress-Path": "/api/hassio_ingress/test-token"}
    with TestClient(api.app, headers=headers) as client:
        started = client.post(
            "/api/satellite-enrollment",
            json={"satellite_entity_id": "assist_satellite.voice"},
        )
        assert started.status_code == 200
        assert started.json()["status"] == "armed"
        armed_id = started.json()["id"]

        fake.states = {
            "assist_satellite.voice": "listening",
            "assist_satellite.other": "listening",
        }
        claim = client.post("/api/satellite-enrollment/claim", json={}).json()
        assert claim["session"] is None
        session = client.get(f"/api/satellite-enrollment/{armed_id}").json()
        assert session["status"] == "armed"
        fake.release.set()
