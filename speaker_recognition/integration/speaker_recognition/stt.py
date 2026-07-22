"""STT proxy that identifies the speaker while transcription runs."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import wave
from array import array
from collections.abc import AsyncIterable
from dataclasses import replace
from datetime import datetime, timezone

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
    async_get_speech_to_text_entity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import get_main_entry
from .api import SpeakerRecognitionApiError
from .const import CONF_STT_ENTITY, DOMAIN, EVENT_DETECTED, EVENT_ENROLLMENT_COMPLETED
from .results import listening_satellite, remember_result

_LOGGER = logging.getLogger(__name__)
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_ANALYSIS_BYTES = 16 * 1024 * 1024


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up an STT wrapper."""
    source = entry.options.get(CONF_STT_ENTITY, entry.data[CONF_STT_ENTITY])
    main = get_main_entry(hass)
    if main is None:
        return
    async_add_entities([SpeakerRecognitionSTT(source, entry.entry_id)])


def _pcm16_mono(data: bytes, metadata: SpeechMetadata) -> tuple[bytes, int]:
    """Convert a WAV/PCM STT stream to the App's signed 16-bit mono contract."""
    sample_rate = int(metadata.sample_rate)
    channels = int(metadata.channel)
    if data.startswith(b"RIFF"):
        with wave.open(io.BytesIO(data), "rb") as audio:
            if audio.getsampwidth() != 2:
                raise ValueError("Only 16-bit PCM STT audio is supported")
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            data = audio.readframes(audio.getnframes())
    if channels == 1:
        return data, sample_rate
    if channels < 1:
        raise ValueError("Invalid audio channel count")
    samples = array("h")
    samples.frombytes(data)
    mono = array("h")
    for offset in range(0, len(samples) - channels + 1, channels):
        mono.append(round(sum(samples[offset : offset + channels]) / channels))
    return mono.tobytes(), sample_rate


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap signed 16-bit mono PCM in a valid seekable WAV container."""
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm)
    return output.getvalue()


def _processed_audio(result: dict) -> tuple[bytes, int] | None:
    """Decode optional extracted PCM returned by old and new App contracts."""
    value = result.get("processed_audio") or result.get("extracted_audio")
    if not isinstance(value, dict) or not value.get("audio_data"):
        return None
    try:
        return base64.b64decode(value["audio_data"], validate=True), int(
            value["sample_rate"]
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _one_chunk(data: bytes):
    """Adapt one buffered audio document back to HA's streaming API."""
    yield data


class SpeakerRecognitionSTT(SpeechToTextEntity):
    """Wrap an existing STT entity and recognize its incoming speaker."""

    _attr_should_poll = False
    _attr_icon = "mdi:account-voice"

    def __init__(self, source_entity_id: str, unique_id: str) -> None:
        self._source_entity_id = source_entity_id
        self._attr_unique_id = unique_id
        self._attr_name = f"{source_entity_id.split('.', 1)[-1]} Speaker Recognition"

    @property
    def _source(self):
        return async_get_speech_to_text_entity(self.hass, self._source_entity_id)

    @property
    def available(self) -> bool:
        return self._source is not None

    @property
    def supported_languages(self) -> list[str]:
        return self._source.supported_languages if self._source else []

    @property
    def supported_formats(self) -> list[AudioFormats]:
        source = self._source
        return [item for item in (source.supported_formats if source else []) if item == AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        source = self._source
        return [item for item in (source.supported_codecs if source else []) if item == AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        source = self._source
        return [item for item in (source.supported_bit_rates if source else []) if int(item) == 16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return self._source.supported_sample_rates if self._source else []

    @property
    def supported_channels(self) -> list[AudioChannels]:
        source = self._source
        return [item for item in (source.supported_channels if source else []) if int(item) in (1, 2)]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Apply the App policy while preserving the source STT contract."""
        source = self._source
        if source is None:
            return SpeechResult(None, SpeechResultState.ERROR)

        main = get_main_entry(self.hass)
        enrollment = None
        if main is not None:
            try:
                enrollment = await main.runtime_data.async_claim_satellite_enrollment()
            except SpeakerRecognitionApiError as error:
                _LOGGER.warning("Could not check satellite enrollment state: %s", error)
                # The App may have accepted a claim before the response was lost.
                # Fail closed so uncertain enrollment audio can never become an intent.
                return SpeechResult(None, SpeechResultState.ERROR)
        if enrollment is not None:
            return await self._async_capture_enrollment(metadata, stream, enrollment)

        satellite_id = listening_satellite(self.hass)
        active_streams = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            "active_stt_streams", []
        )
        stream_token = {"ambiguous": bool(active_streams)}
        if active_streams:
            for active_stream in active_streams:
                active_stream["ambiguous"] = True
        active_streams.append(stream_token)
        started = self.hass.loop.time()
        api = main.runtime_data if main is not None else None
        policy = api.cached_pipeline_policy if api is not None else {
            "extraction_mode": "off",
            "unknown_speaker_policy": "allow",
        }
        policy_available = False
        try:
            try:
                if api is not None:
                    policy = await api.async_pipeline_policy()
                    policy_available = True
            except SpeakerRecognitionApiError as error:
                _LOGGER.warning("Could not refresh Speaker Recognition policy: %s", error)

            mode = policy.get("extraction_mode", "off")
            unknown_policy = policy.get("unknown_speaker_policy", "allow")
            if not policy_available and unknown_policy == "block":
                # Drain the input so the Voice satellite receives AudioStop and
                # reliably returns to idle even when the fail-closed policy wins.
                async for _chunk in stream:
                    pass
                self._remember_analysis(
                    {"matched": False, "outcome": "backend_unavailable"},
                    satellite_id,
                    stream_token,
                    mode,
                    "original",
                    True,
                    0.0,
                    0.0,
                    started,
                    fallback=True,
                )
                return SpeechResult(None, SpeechResultState.ERROR)

            if mode == "before_stt":
                return await self._async_before_stt(
                    source,
                    api,
                    metadata,
                    stream,
                    satellite_id,
                    stream_token,
                    unknown_policy,
                    started,
                )
            return await self._async_parallel_stt(
                source,
                api,
                metadata,
                stream,
                satellite_id,
                stream_token,
                mode,
                unknown_policy,
                started,
            )
        finally:
            if stream_token in active_streams:
                active_streams.remove(stream_token)

    async def _async_parallel_stt(
        self,
        source,
        api,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        satellite_id: str | None,
        stream_token: dict,
        mode: str,
        unknown_policy: str,
        started: float,
    ) -> SpeechResult:
        """Run source STT and off/compare analysis concurrently."""
        audio = bytearray()
        stream_complete = asyncio.Event()

        async def tee_stream():
            try:
                async for chunk in stream:
                    audio.extend(chunk)
                    if len(audio) > MAX_ANALYSIS_BYTES:
                        raise ValueError("STT audio exceeds the analysis limit")
                    yield chunk
            finally:
                stream_complete.set()

        async def analyze() -> tuple[dict | None, float]:
            await stream_complete.wait()
            recognition_started = self.hass.loop.time()
            if not audio or api is None:
                return None, 0.0
            try:
                pcm, sample_rate = _pcm16_mono(bytes(audio), metadata)
                result = await api.async_analyze(
                    pcm,
                    sample_rate,
                    source_entity_id=self._source_entity_id,
                    satellite_id=(None if stream_token["ambiguous"] else satellite_id),
                    extraction_mode=mode,
                )
                return result, (self.hass.loop.time() - recognition_started) * 1000
            except (ValueError, SpeakerRecognitionApiError) as error:
                _LOGGER.warning("Speaker analysis failed: %s", error)
                return None, (self.hass.loop.time() - recognition_started) * 1000

        analysis_task = self.hass.async_create_task(analyze())
        source_started = self.hass.loop.time()
        try:
            transcript = await source.async_process_audio_stream(metadata, tee_stream())
        except asyncio.CancelledError:
            analysis_task.cancel()
            raise
        except Exception:
            stream_complete.set()
            result, recognition_ms = await analysis_task
            if result is not None:
                recognized = self._remember_analysis(
                    result,
                    satellite_id,
                    stream_token,
                    mode,
                    "original",
                    False,
                    recognition_ms,
                    (self.hass.loop.time() - source_started) * 1000,
                    started,
                )
                await self._finalize_stt(
                    api,
                    recognized,
                    SpeechResult(None, SpeechResultState.ERROR),
                    recognized["stt_ms"],
                    started,
                )
            raise
        finally:
            stream_complete.set()
        stt_ms = (self.hass.loop.time() - source_started) * 1000
        result, recognition_ms = await analysis_task
        blocked = self._is_blocked(result, unknown_policy)
        if result is not None:
            recognized = self._remember_analysis(
                result,
                satellite_id,
                stream_token,
                mode,
                "original",
                blocked,
                recognition_ms,
                stt_ms,
                started,
            )
            await self._finalize_stt(api, recognized, transcript, stt_ms, started)
        elif unknown_policy == "block":
            blocked = True
            self._remember_analysis(
                {"matched": False, "outcome": "backend_unavailable"},
                satellite_id,
                stream_token,
                mode,
                "original",
                True,
                recognition_ms,
                stt_ms,
                started,
                fallback=True,
            )
        return SpeechResult(None, SpeechResultState.ERROR) if blocked else transcript

    async def _async_before_stt(
        self,
        source,
        api,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        satellite_id: str | None,
        stream_token: dict,
        unknown_policy: str,
        started: float,
    ) -> SpeechResult:
        """Analyse a complete utterance before choosing audio for source STT."""
        audio = bytearray()
        try:
            async for chunk in stream:
                audio.extend(chunk)
                if len(audio) > MAX_ANALYSIS_BYTES:
                    raise ValueError("STT audio exceeds the analysis limit")
            if not audio:
                return SpeechResult(None, SpeechResultState.ERROR)
            pcm, sample_rate = _pcm16_mono(bytes(audio), metadata)
        except ValueError as error:
            _LOGGER.warning("Could not buffer audio for pre-STT analysis: %s", error)
            return SpeechResult(None, SpeechResultState.ERROR)

        result = None
        recognition_started = self.hass.loop.time()
        if api is not None:
            try:
                result = await api.async_analyze(
                    pcm,
                    sample_rate,
                    source_entity_id=self._source_entity_id,
                    satellite_id=(None if stream_token["ambiguous"] else satellite_id),
                    extraction_mode="before_stt",
                )
            except SpeakerRecognitionApiError as error:
                _LOGGER.warning("Pre-STT speaker analysis failed: %s", error)
        recognition_ms = (self.hass.loop.time() - recognition_started) * 1000
        blocked = self._is_blocked(result, unknown_policy)
        if result is None and unknown_policy == "block":
            blocked = True

        audio_variant = "original"
        fallback = False
        source_audio = bytes(audio)
        source_metadata = metadata
        processed = (
            _processed_audio(result or {})
            if (result or {}).get("extraction_status") == "ready"
            else None
        )
        if not blocked and processed is not None:
            processed_pcm, processed_rate = processed
            if processed_pcm:
                source_audio = _wav_bytes(processed_pcm, processed_rate)
                audio_variant = "extracted"
                try:
                    source_metadata = replace(
                        metadata, sample_rate=processed_rate, channel=1
                    )
                except (TypeError, ValueError):
                    # The App currently preserves the input sample rate. If a
                    # future HA metadata implementation is not a dataclass, use
                    # original audio instead of lying to the downstream STT.
                    if processed_rate != sample_rate or int(metadata.channel) != 1:
                        source_audio = bytes(audio)
                        audio_variant = "original"
                        fallback = True
            else:
                fallback = True
        elif not blocked:
            fallback = True

        recognized = None
        if result is not None:
            recognized = self._remember_analysis(
                result,
                satellite_id,
                stream_token,
                "before_stt",
                audio_variant,
                blocked,
                recognition_ms,
                0.0,
                started,
                fallback=fallback,
                publish=blocked,
            )
        if blocked:
            if recognized is None:
                recognized = self._remember_analysis(
                    {"matched": False, "outcome": "backend_unavailable"},
                    satellite_id,
                    stream_token,
                    "before_stt",
                    "original",
                    True,
                    recognition_ms,
                    0.0,
                    started,
                    fallback=True,
                )
            if recognized is not None:
                await self._finalize_stt(
                    api,
                    recognized,
                    SpeechResult(None, SpeechResultState.ERROR),
                    0.0,
                    started,
                )
            return SpeechResult(None, SpeechResultState.ERROR)

        source_started = self.hass.loop.time()
        try:
            transcript = await source.async_process_audio_stream(
                source_metadata, _one_chunk(source_audio)
            )
        except Exception:
            stt_ms = (self.hass.loop.time() - source_started) * 1000
            if recognized is not None:
                recognized["stt_ms"] = stt_ms
                recognized["total_ms"] = (self.hass.loop.time() - started) * 1000
                remember_result(self.hass, recognized)
                self.hass.bus.async_fire(EVENT_DETECTED, recognized.copy())
                await self._finalize_stt(
                    api,
                    recognized,
                    SpeechResult(None, SpeechResultState.ERROR),
                    stt_ms,
                    started,
                )
            raise
        stt_ms = (self.hass.loop.time() - source_started) * 1000
        if recognized is not None:
            recognized["stt_ms"] = stt_ms
            recognized["total_ms"] = (self.hass.loop.time() - started) * 1000
            remember_result(self.hass, recognized)
            self.hass.bus.async_fire(EVENT_DETECTED, recognized.copy())
            await self._finalize_stt(api, recognized, transcript, stt_ms, started)
        return transcript

    @staticmethod
    def _is_blocked(result: dict | None, unknown_policy: str) -> bool:
        if unknown_policy != "block" or result is None:
            return False
        return not bool(result.get("matched")) or result.get("outcome") in {
            "unmatched",
            "ambiguous",
            "blocked",
        }

    def _remember_analysis(
        self,
        result: dict,
        satellite_id: str | None,
        stream_token: dict,
        mode: str,
        audio_variant: str,
        blocked: bool,
        recognition_ms: float,
        stt_ms: float,
        started: float,
        *,
        fallback: bool = False,
        publish: bool = True,
    ) -> dict:
        """Publish bounded diagnostics and consume-once person context."""
        payload = result.get("result") if isinstance(result.get("result"), dict) else result
        speaker = payload.get("speaker") or {}
        timings = payload.get("timings") or result.get("timings") or {}
        recognized = {
            "recording_id": result.get("recording_id") or payload.get("recording_id"),
            "speaker_id": speaker.get("id"),
            "speaker_name": speaker.get("name"),
            "person_entity_id": speaker.get("person_entity_id"),
            "confidence": payload.get("confidence", 0.0),
            "matched": bool(payload.get("matched")),
            "outcome": payload.get("outcome", "matched" if payload.get("matched") else "unmatched"),
            "scores": payload.get("scores", {}),
            "margin": payload.get("margin"),
            "threshold": payload.get("threshold"),
            "threshold_source": payload.get("threshold_source"),
            "best_segment": payload.get("best_segment"),
            "candidate_count": payload.get("candidate_count"),
            "recognition_ms": timings.get("recognition_ms", recognition_ms),
            "extraction_ms": timings.get("extraction_ms"),
            "stt_ms": stt_ms,
            "total_ms": (self.hass.loop.time() - started) * 1000,
            "extraction_mode": mode,
            "audio_variant": audio_variant,
            "fallback": fallback,
            "blocked": blocked,
            "timestamp": self.hass.loop.time(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "consumed": False,
            "entity_id": self.entity_id,
            "source_entity_id": self._source_entity_id,
            "satellite_id": None if stream_token["ambiguous"] else satellite_id,
        }
        if publish:
            remember_result(self.hass, recognized)
            self.hass.bus.async_fire(EVENT_DETECTED, recognized.copy())
        return recognized

    async def _finalize_stt(
        self,
        api,
        recognized: dict,
        transcript: SpeechResult,
        stt_ms: float,
        started: float,
    ) -> None:
        recording_id = recognized.get("recording_id")
        if api is None or not recording_id:
            return
        text = getattr(transcript, "text", None)
        state = getattr(transcript, "result", None) or getattr(transcript, "state", None)
        details = {
            "transcript": text,
            "stt_entity_id": self._source_entity_id,
            "timings": {
                "stt_ms": stt_ms,
                "total_ms": (self.hass.loop.time() - started) * 1000,
            },
            "audio_variant": recognized.get("audio_variant"),
        }
        if recognized.get("fallback"):
            details["fallback"] = True
        if recognized.get("blocked"):
            details["outcome"] = "blocked"
        elif getattr(state, "value", state) == "error":
            details["outcome"] = "error"
        try:
            await api.async_finalize_analysis(
                recording_id,
                details,
            )
        except SpeakerRecognitionApiError:
            _LOGGER.debug("Could not finalize stored STT analysis", exc_info=True)

    async def _async_capture_enrollment(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        enrollment: dict,
    ) -> SpeechResult:
        """Consume one satellite utterance without forwarding it to intent handling."""
        session_id = str(enrollment["id"])
        audio = bytearray()
        try:
            async for chunk in stream:
                audio.extend(chunk)
                if len(audio) > MAX_CAPTURE_BYTES:
                    raise ValueError("De Voice-opname is te lang")
            if not audio:
                raise ValueError("De Voice-opname bevat geen audio")
            pcm, sample_rate = _pcm16_mono(bytes(audio), metadata)
            main = get_main_entry(self.hass)
            if main is None:
                raise SpeakerRecognitionApiError("Speaker Recognition backend is not loaded")
            await main.runtime_data.async_complete_satellite_enrollment(
                session_id, pcm, sample_rate
            )
        except asyncio.CancelledError:
            main = get_main_entry(self.hass)
            if main is not None:
                try:
                    await asyncio.shield(
                        main.runtime_data.async_fail_satellite_enrollment(
                            session_id, "Voice-opname geannuleerd"
                        )
                    )
                except SpeakerRecognitionApiError:
                    pass
            raise
        except Exception as error:
            _LOGGER.warning("Satellite enrollment failed: %s", error)
            main = get_main_entry(self.hass)
            if main is not None:
                try:
                    await main.runtime_data.async_fail_satellite_enrollment(
                        session_id, str(error)
                    )
                except SpeakerRecognitionApiError:
                    _LOGGER.debug("Could not report failed satellite enrollment", exc_info=True)
        else:
            self.hass.bus.async_fire(
                EVENT_ENROLLMENT_COMPLETED,
                {
                    "satellite_entity_id": enrollment["satellite_entity_id"],
                    "stt_entity_id": self.entity_id,
                    "session_id": session_id,
                },
            )
            # assist_satellite.ask_question runs this as an STT-only pipeline. A
            # successful non-empty result finishes without entering intent handling.
            return SpeechResult("speaker enrollment complete", SpeechResultState.SUCCESS)
        return SpeechResult(None, SpeechResultState.ERROR)
