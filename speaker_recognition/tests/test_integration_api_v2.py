from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import types
from pathlib import Path


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = OSError
aiohttp.ClientSession = object
sys.modules.setdefault("aiohttp", aiohttp)
api_path = (
    Path(__file__).parents[1]
    / "integration"
    / "speaker_recognition"
    / "api.py"
)
spec = importlib.util.spec_from_file_location("speaker_recognition_api_v2_test", api_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
SpeakerRecognitionApi = module.SpeakerRecognitionApi


class Response:
    status = 200

    def __init__(self, value=None):
        self.value = value or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.value

    async def text(self):
        return ""


class ErrorResponse(Response):
    status = 422

    async def text(self):
        return "unsupported audio variant"


class Session:
    def __init__(self):
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0) if self.responses else {}
        return response if isinstance(response, Response) else Response(response)


def test_policy_is_authenticated_validated_and_cached():
    session = Session()
    session.responses.append(
        {"extraction_mode": "compare", "unknown_speaker_policy": "block"}
    )
    api = SpeakerRecognitionApi(session, "http://app/", "secret")

    first = asyncio.run(api.async_pipeline_policy())
    second = asyncio.run(api.async_pipeline_policy())

    assert first == second
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "http://app/api/pipeline-policy")
    assert kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert kwargs["timeout"] == 3


def test_analysis_and_finalize_use_v2_contract():
    session = Session()
    session.responses.append({"recording_id": "rec-1", "matched": False})
    api = SpeakerRecognitionApi(session, "http://app", "secret")

    result = asyncio.run(
        api.async_analyze(
            b"\x01\x00",
            16000,
            source_entity_id="stt.source",
            satellite_id="assist_satellite.voice",
            extraction_mode="before_stt",
        )
    )
    asyncio.run(
        api.async_finalize_analysis(
            "rec-1", {"transcript": "hello", "timings": {"stt_ms": 12.0}}
        )
    )
    asyncio.run(
        api.async_finalize_conversation(
            "rec-1",
            forwarded=False,
            reason="no_eligible_fresh_satellite_match",
        )
    )

    assert result["recording_id"] == "rec-1"
    analyze = session.calls[0]
    assert analyze[0:2] == ("POST", "http://app/api/analyze")
    assert analyze[2]["json"] == {
        "audio": {
            "audio_data": base64.b64encode(b"\x01\x00").decode(),
            "sample_rate": 16000,
        },
        "source": "pipeline",
        "stt_entity_id": "stt.source",
        "satellite_id": "assist_satellite.voice",
        "extraction_mode": "before_stt",
    }
    assert session.calls[1][0:2] == (
        "POST",
        "http://app/api/recordings/rec-1/finalize",
    )
    assert session.calls[2][0:2] == (
        "POST",
        "http://app/api/recordings/rec-1/conversation",
    )
    assert session.calls[2][2]["json"]["conversation_forwarded"] is False
    assert session.calls[2][2]["json"]["conversation_reason"] == (
        "no_eligible_fresh_satellite_match"
    )
    assert session.calls[2][2]["json"]["person_entity_ids"] == []
    assert session.calls[2][2]["json"]["speaker_names"] == []


def test_enrollment_claim_sends_the_locally_observed_satellite():
    session = Session()
    session.responses.append({"session": {"id": "capture-1"}})
    api = SpeakerRecognitionApi(session, "http://app", "secret")

    claimed = asyncio.run(
        api.async_claim_satellite_enrollment("assist_satellite.voice")
    )

    assert claimed == {"id": "capture-1"}
    assert session.calls[0][0:2] == (
        "POST",
        "http://app/api/satellite-enrollment/claim",
    )
    assert session.calls[0][2]["json"] == {
        "satellite_entity_id": "assist_satellite.voice"
    }


def test_process_analysis_uses_the_v21_async_processing_endpoint():
    session = Session()
    session.responses.append({"job_id": "job-1", "status": "queued"})
    api = SpeakerRecognitionApi(session, "http://app", "secret")

    result = asyncio.run(api.async_process_analysis("rec-1", "alice"))

    assert result == {"job_id": "job-1", "status": "queued"}
    assert session.calls[0][0:2] == (
        "POST",
        "http://app/api/analysis/rec-1/process",
    )
    assert session.calls[0][2]["json"] == {"speaker_id": "alice"}


def test_finalize_retries_v21_variants_with_the_v20_schema():
    session = Session()
    session.responses.extend([ErrorResponse(), {}])
    api = SpeakerRecognitionApi(session, "http://app", "secret")

    asyncio.run(
        api.async_finalize_analysis(
            "rec-1",
            {
                "audio_variant": "isolated",
                "fallback_reason": "none",
                "quality": {"passed": True},
            },
        )
    )

    assert len(session.calls) == 2
    assert session.calls[1][2]["json"] == {"audio_variant": "extracted"}
