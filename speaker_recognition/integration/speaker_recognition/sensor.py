"""Diagnostic entities for speaker recognition and LLM personalization."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_CONTEXT_UPDATED, SIGNAL_RESULT_UPDATED


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up diagnostic sensors on the backend entry."""
    async_add_entities(
        [
            LastRecognitionSensor(entry),
            LastConversationContextSensor(entry),
        ]
    )


class SpeakerRecognitionDiagnosticSensor(SensorEntity):
    """Base class for event-driven Speaker Recognition diagnostics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:account-voice"

    def __init__(self, entry: ConfigEntry, suffix: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Speaker Recognition",
            manufacturer="Timminater",
            model="Speaker Recognition companion",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe after the entity is registered."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.signal, self.async_write_ha_state)
        )

    @property
    def signal(self) -> str:
        """Return the dispatcher signal used by this sensor."""
        raise NotImplementedError

    @property
    def data(self) -> dict[str, Any] | None:
        """Return the latest in-memory diagnostic record."""
        raise NotImplementedError


class LastRecognitionSensor(SpeakerRecognitionDiagnosticSensor):
    """Show the latest recognition result and all useful metadata."""

    _attr_translation_key = "last_recognition"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "last_recognition")

    @property
    def signal(self) -> str:
        return SIGNAL_RESULT_UPDATED

    @property
    def data(self) -> dict[str, Any] | None:
        return self.hass.data.get(DOMAIN, {}).get("last_result")

    @property
    def native_value(self) -> str | None:
        result = self.data
        if result is None:
            return None
        if not result.get("matched"):
            return "not_recognized"
        return result.get("speaker_name") or result.get("person_entity_id") or "matched"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.data
        if result is None:
            return {}
        return {
            "recording_id": result.get("recording_id"),
            "outcome": result.get("outcome"),
            "matched": bool(result.get("matched")),
            "confidence": float(result.get("confidence", 0.0)),
            "speaker_id": result.get("speaker_id"),
            "speaker_name": result.get("speaker_name"),
            "person_entity_id": result.get("person_entity_id"),
            "satellite_id": result.get("satellite_id"),
            "stt_entity_id": result.get("entity_id"),
            "scores": result.get("scores", {}),
            "margin": result.get("margin"),
            "threshold": result.get("threshold"),
            "threshold_source": result.get("threshold_source"),
            "best_segment": result.get("best_segment"),
            "candidate_count": result.get("candidate_count"),
            "recognition_ms": result.get("recognition_ms"),
            "extraction_ms": result.get("extraction_ms"),
            "stt_ms": result.get("stt_ms"),
            "total_ms": result.get("total_ms"),
            "extraction_mode": result.get("extraction_mode"),
            "audio_variant": result.get("audio_variant"),
            "fallback": bool(result.get("fallback")),
            "blocked": bool(result.get("blocked")),
            "observed_at": result.get("observed_at"),
            "consumed_for_conversation": bool(result.get("consumed")),
        }


class LastConversationContextSensor(SpeakerRecognitionDiagnosticSensor):
    """Show whether mapped person context was submitted to the source LLM."""

    _attr_translation_key = "last_conversation_context"
    _attr_icon = "mdi:account-arrow-right"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "last_conversation_context")

    @property
    def signal(self) -> str:
        return SIGNAL_CONTEXT_UPDATED

    @property
    def data(self) -> dict[str, Any] | None:
        return self.hass.data.get(DOMAIN, {}).get("last_conversation_context")

    @property
    def native_value(self) -> str | None:
        context = self.data
        if context is None:
            return None
        return (
            context.get("person_entity_id")
            if context.get("forwarded")
            else "not_forwarded"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        context = self.data
        return dict(context) if context is not None else {}
