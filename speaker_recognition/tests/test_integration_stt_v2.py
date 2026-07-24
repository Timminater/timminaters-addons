from __future__ import annotations

import asyncio
import io
import sys
import types
import wave
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from types import SimpleNamespace


# Lightweight HA contracts keep these pipeline tests independent of a full HA
# installation while exercising the real integration implementation.
homeassistant = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
core.callback = lambda function: function
homeassistant.core = core
components = types.ModuleType("homeassistant.components")
stt_component = types.ModuleType("homeassistant.components.stt")


class AudioBitRates(IntEnum):
    BITRATE_16 = 16


class AudioChannels(IntEnum):
    MONO = 1
    STEREO = 2


class AudioCodecs(str, Enum):
    PCM = "pcm"


class AudioFormats(str, Enum):
    WAV = "wav"


class AudioSampleRates(IntEnum):
    RATE_16000 = 16000


class SpeechResultState(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


@dataclass(slots=True)
class SpeechMetadata:
    language: str = "nl"
    format: AudioFormats = AudioFormats.WAV
    codec: AudioCodecs = AudioCodecs.PCM
    bit_rate: AudioBitRates = AudioBitRates.BITRATE_16
    sample_rate: int = 16000
    channel: int = 1


@dataclass(slots=True)
class SpeechResult:
    text: str | None
    result: SpeechResultState


class SpeechToTextEntity:
    pass


stt_component.AudioBitRates = AudioBitRates
stt_component.AudioChannels = AudioChannels
stt_component.AudioCodecs = AudioCodecs
stt_component.AudioFormats = AudioFormats
stt_component.AudioSampleRates = AudioSampleRates
stt_component.SpeechMetadata = SpeechMetadata
stt_component.SpeechResult = SpeechResult
stt_component.SpeechResultState = SpeechResultState
stt_component.SpeechToTextEntity = SpeechToTextEntity
stt_component.async_get_speech_to_text_entity = lambda hass, _entity_id: hass.source
components.stt = stt_component
config_entries = types.ModuleType("homeassistant.config_entries")
config_entries.ConfigEntry = object
helpers = types.ModuleType("homeassistant.helpers")
entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object
dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
dispatcher.async_dispatcher_send = lambda hass, signal: hass.dispatched.append(signal)
event = types.ModuleType("homeassistant.helpers.event")
event.async_call_later = lambda hass, delay, callback: lambda: None
sys.modules.update(
    {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.components": components,
        "homeassistant.components.stt": stt_component,
        "homeassistant.config_entries": config_entries,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.helpers.event": event,
    }
)
integration_package = Path(__file__).parents[1] / "integration" / "speaker_recognition"
package = types.ModuleType("speaker_recognition")
package.__path__ = [str(integration_package)]
package.get_main_entry = lambda hass: hass.main
sys.modules["speaker_recognition"] = package
aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = OSError
aiohttp.ClientSession = object
aiohttp.ClientTimeout = lambda **kwargs: kwargs
sys.modules.setdefault("aiohttp", aiohttp)

from speaker_recognition.api import SpeakerRecognitionApiError
from speaker_recognition.stt import (
    SpeakerRecognitionSTT,
    _StreamingPcm16Mono,
    _pcm16_mono,
    _processed_audio,
)


class Clock:
    def __init__(self):
        self.value = 10.0

    def time(self):
        self.value += 0.001
        return self.value


class States:
    def async_all(self, _domain):
        return [SimpleNamespace(entity_id="assist_satellite.voice", state="listening")]

    def get(self, entity_id):
        return SimpleNamespace(entity_id=entity_id) if entity_id == "person.alice" else None


class Bus:
    def __init__(self):
        self.events = []

    def async_fire(self, event, data):
        self.events.append((event, data))


class Source:
    supported_languages = ["nl"]
    supported_formats = [AudioFormats.WAV]
    supported_codecs = [AudioCodecs.PCM]
    supported_bit_rates = [AudioBitRates.BITRATE_16]
    supported_sample_rates = [AudioSampleRates.RATE_16000]
    supported_channels = [AudioChannels.MONO, AudioChannels.STEREO]

    def __init__(self):
        self.calls = []

    async def async_process_audio_stream(self, metadata, stream):
        data = bytearray()
        async for chunk in stream:
            data.extend(chunk)
        self.calls.append((metadata, bytes(data)))
        return SpeechResult("hello", SpeechResultState.SUCCESS)


class Api:
    def __init__(
        self,
        mode="off",
        unknown="allow",
        result=None,
        backend="df2_batch",
    ):
        self.policy = {
            "extraction_mode": mode,
            "unknown_speaker_policy": unknown,
            "audio_processing_backend": backend,
        }
        self.result = result or matched_result()
        self.analyze_calls = []
        self.stream_analyze_calls = []
        self.finalize_calls = []
        self.policy_error = False
        self.analyze_error = False
        self.enrollment = None
        self.claim_calls = []
        self.complete_enrollment_calls = []
        self.fail_enrollment_calls = []

    @property
    def cached_pipeline_policy(self):
        return dict(self.policy)

    async def async_pipeline_policy(self):
        if self.policy_error:
            raise SpeakerRecognitionApiError("offline")
        return dict(self.policy)

    async def async_claim_satellite_enrollment(self, satellite_entity_id):
        self.claim_calls.append(satellite_entity_id)
        return self.enrollment

    async def async_complete_satellite_enrollment(
        self, session_id, pcm, sample_rate
    ):
        self.complete_enrollment_calls.append((session_id, pcm, sample_rate))

    async def async_fail_satellite_enrollment(self, session_id, error):
        self.fail_enrollment_calls.append((session_id, error))

    async def async_analyze(self, pcm, sample_rate, **details):
        if self.analyze_error:
            raise SpeakerRecognitionApiError("offline")
        self.analyze_calls.append((pcm, sample_rate, details))
        return dict(self.result)

    async def async_analyze_stream(self, pcm_stream, sample_rate, **details):
        pcm = bytearray()
        async for chunk in pcm_stream:
            pcm.extend(chunk)
        if self.analyze_error:
            raise SpeakerRecognitionApiError("offline")
        self.stream_analyze_calls.append((bytes(pcm), sample_rate, details))
        return dict(self.result)

    async def async_finalize_analysis(self, recording_id, details):
        self.finalize_calls.append((recording_id, details))


def matched_result(**changes):
    value = {
        "recording_id": "rec-1",
        "matched": True,
        "outcome": "matched",
        "confidence": 0.91,
        "scores": {"Alice": 0.91},
        "margin": 0.4,
        "threshold": 0.7,
        "threshold_source": "global",
        "best_segment": {"start": 0.1, "end": 2.0},
        "candidate_count": 3,
        "speaker": {
            "id": "alice",
            "name": "Alice",
            "person_entity_id": "person.alice",
        },
        "timings": {"recognition_ms": 25.0, "extraction_ms": 4.0},
    }
    value.update(changes)
    return value


def wav(pcm=b"\x01\x00" * 400, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()


async def chunks(data):
    midpoint = len(data) // 2
    yield data[:midpoint]
    yield data[midpoint:]


def make_proxy(api):
    source = Source()
    hass = SimpleNamespace(
        source=source,
        main=SimpleNamespace(runtime_data=api),
        data={},
        loop=Clock(),
        states=States(),
        bus=Bus(),
        dispatched=[],
        timers=[],
        async_create_task=asyncio.create_task,
    )
    proxy = SpeakerRecognitionSTT("stt.source", "proxy")
    proxy.hass = hass
    proxy.entity_id = "stt.speaker_recognition"
    return proxy, hass, source


def test_streaming_wav_parser_handles_split_header_and_stereo_frames():
    left_right = b"\xe8\x03\x18\xfc" * 137
    document = io.BytesIO()
    with wave.open(document, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(left_right)
    parser = _StreamingPcm16Mono(SpeechMetadata(channel=2))
    output = bytearray()
    payload = document.getvalue()
    for offset in range(0, len(payload), 7):
        output.extend(parser.feed(payload[offset : offset + 7]))
    parser.finish()

    assert output == b"\x00\x00" * 137


def test_before_stt_df3_streams_pcm_during_upload():
    original_pcm = b"\x01\x00" * 1200
    denoised_pcm = b"\x02\x00" * 1200
    api = Api(
        mode="before_stt",
        backend="df3_streaming",
        result=matched_result(
            denoised_audio={
                "audio_data": __import__("base64").b64encode(
                    denoised_pcm
                ).decode(),
                "sample_rate": 16000,
            },
            processing_quality={"denoised_passed": True},
        ),
    )
    proxy, _hass, source = make_proxy(api)

    result = asyncio.run(
        proxy.async_process_audio_stream(
            SpeechMetadata(),
            chunks(wav(original_pcm)),
        )
    )

    assert result.text == "hello"
    assert api.analyze_calls == []
    assert api.stream_analyze_calls[0][0] == original_pcm
    assert api.stream_analyze_calls[0][1] == 16000
    with wave.open(io.BytesIO(source.calls[0][1]), "rb") as audio:
        assert audio.readframes(audio.getnframes()) == denoised_pcm


def test_off_and_compare_keep_original_audio_and_finalize_recording():
    for mode in ("off", "compare"):
        api = Api(mode=mode)
        proxy, hass, source = make_proxy(api)
        original = wav()
        returned = asyncio.run(
            proxy.async_process_audio_stream(SpeechMetadata(), chunks(original))
        )
        assert returned.text == "hello"
        assert source.calls[0][1] == original
        assert api.analyze_calls[0][2]["extraction_mode"] == mode
        assert api.analyze_calls[0][2]["source_entity_id"] == "stt.source"
        assert api.finalize_calls[0][0] == "rec-1"
        assert api.finalize_calls[0][1]["transcript"] == "hello"
        result = hass.data["speaker_recognition"]["last_result"]
        assert result["recording_id"] == "rec-1"
        assert result["audio_variant"] == "original"


def test_voice_enrollment_claim_uses_pre_round_trip_satellite_snapshot():
    api = Api()
    api.enrollment = {
        "id": "capture-1",
        "satellite_entity_id": "assist_satellite.voice",
    }
    proxy, hass, source = make_proxy(api)
    original_pcm = b"\x01\x00" * 400

    returned = asyncio.run(
        proxy.async_process_audio_stream(
            SpeechMetadata(), chunks(wav(original_pcm))
        )
    )

    assert api.claim_calls == ["assist_satellite.voice"]
    assert api.complete_enrollment_calls == [
        ("capture-1", original_pcm, 16000)
    ]
    assert api.fail_enrollment_calls == []
    assert source.calls == []
    assert returned.result is SpeechResultState.SUCCESS
    assert returned.text == "speaker enrollment complete"
    assert hass.bus.events[0][1]["satellite_entity_id"] == (
        "assist_satellite.voice"
    )


def test_before_stt_ignores_legacy_extracted_audio():
    processed_pcm = b"\x02\x00" * 100
    import base64

    api = Api(
        mode="before_stt",
        result=matched_result(
            extraction_status="ready",
            processed_audio={
                "audio_data": base64.b64encode(processed_pcm).decode(),
                "sample_rate": 16000,
            }
        ),
    )
    proxy, hass, source = make_proxy(api)
    returned = asyncio.run(
        proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav()))
    )
    assert returned.result is SpeechResultState.SUCCESS
    metadata, sent = source.calls[0]
    pcm, rate = _pcm16_mono(sent, metadata)
    assert pcm == b"\x01\x00" * 400
    assert rate == 16000
    result = hass.data["speaker_recognition"]["last_result"]
    assert result["audio_variant"] == "original"
    assert result["fallback"] is True


def test_before_stt_ignores_isolated_and_uses_denoised():
    import base64

    isolated = b"\x03\x00" * 10
    denoised = b"\x04\x00" * 10
    api = Api(
        mode="before_stt",
        result=matched_result(
            processing_status="complete",
            audio_variants={
                "isolated": {
                    "audio_data": base64.b64encode(isolated).decode(),
                    "sample_rate": 16000,
                },
                "denoised": {
                    "audio_data": base64.b64encode(denoised).decode(),
                    "sample_rate": 16000,
                },
            },
            timings={
                "recognition_ms": 25.0,
                "audio_processing_ms": 42.0,
            },
            processing_stages={"denoise": {"ms": 10}, "model": "warm"},
            processing_quality={"passed": True, "model_was_loaded": True},
        ),
    )
    proxy, hass, source = make_proxy(api)
    asyncio.run(proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav())))

    pcm, _rate = _pcm16_mono(source.calls[0][1], source.calls[0][0])
    result = hass.data["speaker_recognition"]["last_result"]
    assert pcm == denoised
    assert result["audio_variant"] == "denoised"
    assert result["denoise_ms"] == 10.0
    assert result["audio_processing_ms"] == 42.0
    assert result["quality"] == {"passed": True, "model_was_loaded": True}


def test_before_stt_uses_denoised_when_isolated_is_unavailable():
    import base64

    denoised = b"\x04\x00" * 10
    api = Api(
        mode="before_stt",
        result=matched_result(
            audio_variants={
                "isolated": {"audio_data": "not-base64", "sample_rate": 16000},
                "denoised": {
                    "audio_data": base64.b64encode(denoised).decode(),
                    "sample_rate": 16000,
                },
            },
            fallback_reason="isolation_quality_rejected",
        ),
    )
    proxy, hass, source = make_proxy(api)
    asyncio.run(proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav())))

    pcm, _rate = _pcm16_mono(source.calls[0][1], source.calls[0][0])
    result = hass.data["speaker_recognition"]["last_result"]
    assert pcm == denoised
    assert result["audio_variant"] == "denoised"
    assert result["fallback"] is False


def test_processed_audio_ignores_v20_extracted_payload():
    import base64

    pcm = b"\x05\x00" * 10
    assert _processed_audio(
        {
            "processed_audio": {
                "audio_data": base64.b64encode(pcm).decode(),
                "sample_rate": 16000,
            }
        }
    ) is None


def test_processed_audio_ignores_boolean_availability_flags():
    import base64

    pcm = b"\x06\x00" * 10
    assert _processed_audio(
        {
            "audio_variants": {"isolated": True, "denoised": True},
            "isolated_audio": {
                "audio_data": base64.b64encode(pcm).decode(),
                "sample_rate": 16000,
            },
        }
    ) is None


def test_processed_audio_rejects_conflicting_denoised_variant():
    import base64

    pcm = b"\x07\x00" * 10
    assert (
        _processed_audio(
            {
                "denoised_audio": {
                    "audio_data": base64.b64encode(pcm).decode(),
                    "sample_rate": 16000,
                },
                "processing_quality": {"denoised_safe_for_stt": False},
            }
        )
        is None
    )


def test_processed_audio_rejects_failed_denoised_quality():
    import base64

    pcm = b"\x08\x00" * 10
    for quality in ({"denoised_passed": False}, {"passed": False}):
        assert (
            _processed_audio(
                {
                    "denoised_audio": {
                        "audio_data": base64.b64encode(pcm).decode(),
                        "sample_rate": 16000,
                    },
                    "processing_quality": quality,
                }
            )
            is None
        )


def test_before_stt_extraction_failure_falls_back_to_original():
    api = Api(mode="before_stt", result=matched_result(extraction_status="failed"))
    proxy, hass, source = make_proxy(api)
    original = wav()
    asyncio.run(proxy.async_process_audio_stream(SpeechMetadata(), chunks(original)))
    assert source.calls[0][1] == original
    result = hass.data["speaker_recognition"]["last_result"]
    assert result["audio_variant"] == "original"
    assert result["fallback"] is True


def test_block_skips_pre_stt_or_discards_parallel_transcript():
    unmatched = matched_result(matched=False, outcome="unmatched", speaker=None)
    for mode, expected_calls in (("before_stt", 0), ("off", 1)):
        api = Api(mode=mode, unknown="block", result=unmatched)
        proxy, hass, source = make_proxy(api)
        returned = asyncio.run(
            proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav()))
        )
        assert returned.result is SpeechResultState.ERROR
        assert len(source.calls) == expected_calls
        assert hass.data["speaker_recognition"]["last_result"]["blocked"] is True


def test_multiple_known_speakers_are_not_blocked_and_are_published():
    multiple = matched_result(
        matched=False,
        outcome="multiple_speakers",
        speaker=None,
        detected_speakers=[
            {
                "speaker_id": "eline",
                "speaker_name": "Eline",
                "person_entity_id": "person.eline",
                "confidence": 0.93,
            },
            {
                "speaker_id": "anne-marie",
                "speaker_name": "Anne-Marie",
                "person_entity_id": "person.anne_marie",
                "confidence": 0.91,
            },
        ],
    )
    api = Api(mode="off", unknown="block", result=multiple)
    proxy, hass, source = make_proxy(api)

    returned = asyncio.run(
        proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav()))
    )

    assert returned.result is SpeechResultState.SUCCESS
    assert len(source.calls) == 1
    recognized = hass.data["speaker_recognition"]["last_result"]
    assert recognized["blocked"] is False
    assert recognized["multiple_speakers"] is True
    assert recognized["speaker_names"] == ["Eline", "Anne-Marie"]


def test_backend_failure_is_fail_open_for_allow_and_closed_for_block():
    for unknown, expected_calls, expected_state in (
        ("allow", 1, SpeechResultState.SUCCESS),
        ("block", 0, SpeechResultState.ERROR),
    ):
        api = Api(unknown=unknown)
        api.policy_error = True
        proxy, _hass, source = make_proxy(api)
        returned = asyncio.run(
            proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav()))
        )
        assert returned.result is expected_state
        assert len(source.calls) == expected_calls


def test_analysis_failure_obeys_policy_after_source_stt():
    for unknown, expected_state in (
        ("allow", SpeechResultState.SUCCESS),
        ("block", SpeechResultState.ERROR),
    ):
        api = Api(unknown=unknown)
        api.analyze_error = True
        proxy, _hass, source = make_proxy(api)
        returned = asyncio.run(
            proxy.async_process_audio_stream(SpeechMetadata(), chunks(wav()))
        )
        assert returned.result is expected_state
        assert len(source.calls) == 1
