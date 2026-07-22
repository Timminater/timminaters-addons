"""STT proxy that identifies the speaker while transcription runs."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from array import array
from collections.abc import AsyncIterable

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
from .const import CONF_STT_ENTITY, DOMAIN, EVENT_DETECTED

_LOGGER = logging.getLogger(__name__)


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
        """Tee the stream to STT and speaker recognition."""
        source = self._source
        if source is None:
            return SpeechResult(None, SpeechResultState.ERROR)

        audio = bytearray()
        stream_complete = asyncio.Event()

        async def tee_stream():
            try:
                async for chunk in stream:
                    audio.extend(chunk)
                    yield chunk
            finally:
                stream_complete.set()

        async def recognize() -> None:
            await stream_complete.wait()
            if not audio:
                return
            try:
                pcm, sample_rate = _pcm16_mono(bytes(audio), metadata)
                main = get_main_entry(self.hass)
                if main is None:
                    _LOGGER.warning("Speaker Recognition backend is not loaded")
                    return
                result = await main.runtime_data.async_recognize(pcm, sample_rate)
            except (ValueError, SpeakerRecognitionApiError) as error:
                _LOGGER.warning("Speaker recognition failed: %s", error)
                return

            speaker = result.get("speaker") or {}
            recognized = {
                "speaker_id": speaker.get("id"),
                "speaker_name": speaker.get("name"),
                "confidence": result.get("confidence", 0.0),
                "matched": bool(result.get("matched")),
                "scores": result.get("scores", {}),
                "timestamp": self.hass.loop.time(),
                "consumed": False,
                "entity_id": self.entity_id,
            }
            self.hass.data.setdefault(DOMAIN, {})["last_result"] = recognized
            self.hass.bus.async_fire(EVENT_DETECTED, recognized.copy())

        recognition_task = self.hass.async_create_task(recognize())
        try:
            transcript = await source.async_process_audio_stream(metadata, tee_stream())
        finally:
            stream_complete.set()
        await recognition_task
        return transcript
