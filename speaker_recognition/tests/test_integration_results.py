from __future__ import annotations

import sys
import types
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


homeassistant = types.ModuleType("homeassistant")
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = object


def callback(function):
    function._hass_callback = True
    return function


homeassistant_core.callback = callback
homeassistant.core = homeassistant_core
components = types.ModuleType("homeassistant.components")
conversation_component = types.ModuleType("homeassistant.components.conversation")
sensor_component = types.ModuleType("homeassistant.components.sensor")


class ConversationEntity:
    pass


class SensorEntity:
    pass


@dataclass(slots=True)
class ConversationInput:
    text: str
    context: object
    conversation_id: str | None
    device_id: str | None
    satellite_id: str | None
    language: str
    agent_id: str
    extra_system_prompt: str | None = None


class ConversationResult:
    pass


conversation_component.ConversationEntity = ConversationEntity
conversation_component.async_get_agent = lambda hass, _agent_id: hass.agent
sensor_component.SensorEntity = SensorEntity
conversation_models = types.ModuleType("homeassistant.components.conversation.models")
conversation_models.ConversationInput = ConversationInput
conversation_models.ConversationResult = ConversationResult
config_entries = types.ModuleType("homeassistant.config_entries")
config_entries.ConfigEntry = object
exceptions = types.ModuleType("homeassistant.exceptions")
exceptions.HomeAssistantError = RuntimeError
helpers = types.ModuleType("homeassistant.helpers")
dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
dispatcher.async_dispatcher_send = lambda hass, signal: hass.dispatched.append(signal)
dispatcher.async_dispatcher_connect = lambda hass, signal, target: lambda: None
event = types.ModuleType("homeassistant.helpers.event")


def async_call_later(hass, delay, callback):
    timer = SimpleNamespace(delay=delay, callback=callback, cancelled=False)
    hass.timers.append(timer)

    def cancel():
        timer.cancelled = True

    return cancel


event.async_call_later = async_call_later
device_registry = types.ModuleType("homeassistant.helpers.device_registry")
device_registry.DeviceInfo = lambda **kwargs: kwargs
entity = types.ModuleType("homeassistant.helpers.entity")
entity.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object
sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.core", homeassistant_core)
sys.modules.setdefault("homeassistant.components", components)
sys.modules.setdefault("homeassistant.components.conversation", conversation_component)
sys.modules.setdefault("homeassistant.components.conversation.models", conversation_models)
sys.modules.setdefault("homeassistant.components.sensor", sensor_component)
sys.modules.setdefault("homeassistant.config_entries", config_entries)
sys.modules.setdefault("homeassistant.exceptions", exceptions)
sys.modules.setdefault("homeassistant.helpers", helpers)
sys.modules.setdefault("homeassistant.helpers.device_registry", device_registry)
sys.modules.setdefault("homeassistant.helpers.dispatcher", dispatcher)
sys.modules.setdefault("homeassistant.helpers.entity", entity)
sys.modules.setdefault("homeassistant.helpers.entity_platform", entity_platform)
sys.modules.setdefault("homeassistant.helpers.event", event)
aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = OSError
aiohttp.ClientSession = object
sys.modules.setdefault("aiohttp", aiohttp)
integration_package = Path(__file__).parents[1] / "integration" / "speaker_recognition"
speaker_recognition_package = types.ModuleType("speaker_recognition")
speaker_recognition_package.__path__ = [str(integration_package)]
sys.modules.setdefault("speaker_recognition", speaker_recognition_package)

from speaker_recognition.const import SIGNAL_CONTEXT_UPDATED, SIGNAL_RESULT_UPDATED
from speaker_recognition.results import (
    consume_result,
    listening_satellite,
    remember_conversation_context,
    remember_result,
)
from speaker_recognition.conversation import SpeakerRecognitionConversation
from speaker_recognition.sensor import LastConversationContextSensor, LastRecognitionSensor


class FakeLoop:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class FakeStates:
    def __init__(self, states=None, person_exists=True) -> None:
        self._states = states or []
        self._person_exists = person_exists

    def async_all(self, _domain):
        return self._states

    def get(self, entity_id):
        if self._person_exists and entity_id == "person.alice":
            return SimpleNamespace(entity_id=entity_id, state="home")
        return None


def fake_hass(now=100.0, states=None, person_exists=True):
    return SimpleNamespace(
        data={},
        loop=FakeLoop(now),
        states=FakeStates(states, person_exists=person_exists),
        dispatched=[],
        timers=[],
    )


def result(**overrides):
    value = {
        "timestamp": 99.0,
        "matched": True,
        "confidence": 0.91,
        "person_entity_id": "person.alice",
        "satellite_id": "assist_satellite.kitchen",
        "consumed": False,
    }
    value.update(overrides)
    return value


def test_result_matches_satellite_and_is_consumed_once():
    hass = fake_hass()
    remember_result(hass, result())
    assert consume_result(hass, "assist_satellite.kitchen", 0.8)[
        "person_entity_id"
    ] == "person.alice"
    assert hass.dispatched == [SIGNAL_RESULT_UPDATED, SIGNAL_RESULT_UPDATED]
    assert hass.data["speaker_recognition"]["last_result"]["consumed"] is True
    assert consume_result(hass, "assist_satellite.kitchen", 0.8) is None


def test_conversation_context_is_stored_and_dispatched():
    hass = fake_hass()
    context = {"forwarded": True, "person_entity_id": "person.alice"}

    remember_conversation_context(hass, context)

    assert hass.data["speaker_recognition"]["last_conversation_context"] == context
    assert hass.dispatched == [SIGNAL_CONTEXT_UPDATED]


def test_diagnostic_records_reset_after_30_seconds_and_new_values_win():
    hass = fake_hass()
    first = result(speaker_name="Alice")
    second = result(speaker_name="Bob")

    remember_result(hass, first)
    remember_result(hass, second)

    assert hass.timers[0].cancelled is True
    assert hass.timers[1].delay == 30
    assert hass.timers[1].callback._hass_callback is True
    hass.timers[0].callback(None)
    assert hass.data["speaker_recognition"]["last_result"] is second
    assert "last_result_reset_timer" in hass.data["speaker_recognition"]
    hass.timers[1].callback(None)
    assert hass.data["speaker_recognition"]["last_result"] is None
    assert "last_result_reset_timer" not in hass.data["speaker_recognition"]

    context = {"forwarded": True, "person_entity_id": "person.alice"}
    remember_conversation_context(hass, context)
    assert hass.timers[2].delay == 30
    hass.timers[2].callback(None)
    assert hass.data["speaker_recognition"]["last_conversation_context"] is None
    assert hass.dispatched[-2:] == [SIGNAL_CONTEXT_UPDATED, SIGNAL_CONTEXT_UPDATED]


def test_diagnostic_sensors_expose_recognition_and_forwarding_details():
    hass = fake_hass()
    recognized = result(
        recording_id="recording-1",
        speaker_id="voice-1",
        speaker_name="Alice",
        scores={"Alice": 0.91, "Bob": 0.12},
        entity_id="stt.speaker_recognition",
        observed_at="2026-07-22T19:00:00+00:00",
        outcome="matched",
        margin=0.33,
        threshold=0.7,
        threshold_source="global",
        best_segment={"start": 0.2, "end": 2.7},
        candidate_count=5,
        recognition_ms=31.0,
        extraction_ms=4.0,
        stt_ms=120.0,
        audio_processing_ms=35.0,
        total_ms=155.0,
        extraction_mode="compare",
        audio_variant="original",
        fallback=False,
        blocked=False,
    )
    remember_result(hass, recognized)
    consume_result(hass, "assist_satellite.kitchen", 0.8)
    remember_conversation_context(
        hass,
        {
            "forwarded": True,
            "reason": "person_context_submitted",
            "person_entity_id": "person.alice",
            "source_conversation_entity": "conversation.source",
        },
    )
    entry = SimpleNamespace(entry_id="main-entry")
    recognition_sensor = LastRecognitionSensor(entry)
    recognition_sensor.hass = hass
    context_sensor = LastConversationContextSensor(entry)
    context_sensor.hass = hass

    assert recognition_sensor.native_value == "Alice"
    assert recognition_sensor.extra_state_attributes["confidence"] == 0.91
    assert recognition_sensor.extra_state_attributes["consumed_for_conversation"] is True
    assert recognition_sensor.extra_state_attributes["scores"]["Bob"] == 0.12
    assert recognition_sensor.extra_state_attributes["recording_id"] == "recording-1"
    assert recognition_sensor.extra_state_attributes["margin"] == 0.33
    assert recognition_sensor.extra_state_attributes["candidate_count"] == 5
    assert recognition_sensor.extra_state_attributes["total_ms"] == 155.0
    assert recognition_sensor.extra_state_attributes["audio_processing_ms"] == 35.0
    assert "transcript" not in recognition_sensor.extra_state_attributes
    assert "audio_url" not in recognition_sensor.extra_state_attributes
    assert context_sensor.native_value == "person.alice"
    assert context_sensor.extra_state_attributes["forwarded"] is True
    assert (
        context_sensor.extra_state_attributes["source_conversation_entity"]
        == "conversation.source"
    )


def test_result_fails_closed_for_wrong_source_stale_or_low_confidence():
    hass = fake_hass()
    remember_result(hass, result())
    assert consume_result(hass, "assist_satellite.office", 0.8) is None

    hass = fake_hass(now=200.0)
    remember_result(hass, result(timestamp=100.0))
    assert consume_result(hass, "assist_satellite.kitchen", 0.8) is None

    hass = fake_hass()
    remember_result(hass, result(confidence=0.4))
    assert consume_result(hass, "assist_satellite.kitchen", 0.8) is None

    hass = fake_hass(person_exists=False)
    remember_result(hass, result())
    assert consume_result(hass, "assist_satellite.kitchen", 0.8) is None


def test_unattributed_conversation_never_receives_personalization():
    hass = fake_hass()
    remember_result(hass, result(satellite_id=None))
    assert consume_result(hass, None, 0.8) is None


def test_listening_satellite_requires_an_unambiguous_source():
    kitchen = SimpleNamespace(entity_id="assist_satellite.kitchen", state="listening")
    office = SimpleNamespace(entity_id="assist_satellite.office", state="idle")
    assert listening_satellite(fake_hass(states=[kitchen, office])) == kitchen.entity_id
    office.state = "listening"
    assert listening_satellite(fake_hass(states=[kitchen, office])) is None


def test_conversation_personalization_preserves_original_context_and_fields():
    class Source:
        supported_languages = "*"

        async def async_process(self, user_input):
            self.received = user_input
            return ConversationResult()

    hass = fake_hass()
    hass.agent = Source()
    remember_result(hass, result())
    proxy = SpeakerRecognitionConversation("conversation.source", 0.8, "proxy")
    proxy.hass = hass
    context = object()
    original = ConversationInput(
        text="Turn on the light",
        context=context,
        conversation_id="conversation-1",
        device_id="device-1",
        satellite_id="assist_satellite.kitchen",
        language="nl",
        agent_id="conversation.proxy",
        extra_system_prompt="Existing prompt",
    )

    returned = asyncio.run(proxy.async_process(original))

    assert isinstance(returned, ConversationResult)
    routed = hass.agent.received
    assert routed.context is context
    assert routed.text == original.text
    assert routed.conversation_id == original.conversation_id
    assert routed.device_id == original.device_id
    assert routed.satellite_id == original.satellite_id
    assert routed.language == original.language
    assert routed.agent_id == "conversation.source"
    assert routed.extra_system_prompt.startswith("Existing prompt")
    assert "person.alice" in routed.extra_system_prompt
    assert "never authentication" in routed.extra_system_prompt
    forwarding = hass.data["speaker_recognition"]["last_conversation_context"]
    assert forwarding["forwarded"] is True
    assert forwarding["person_entity_id"] == "person.alice"
    assert forwarding["source_conversation_entity"] == "conversation.source"
    assert forwarding["satellite_id"] == "assist_satellite.kitchen"


def test_conversation_finalizes_correlated_recording_without_changing_context():
    class Source:
        supported_languages = "*"

        async def async_process(self, user_input):
            self.received = user_input
            return ConversationResult()

    class Api:
        def __init__(self):
            self.calls = []

        async def async_finalize_conversation(self, recording_id, **details):
            self.calls.append((recording_id, details))

    hass = fake_hass()
    hass.agent = Source()
    api = Api()
    hass.main = SimpleNamespace(runtime_data=api)
    previous = getattr(speaker_recognition_package, "get_main_entry", None)
    speaker_recognition_package.get_main_entry = lambda value: value.main
    remember_result(hass, result(recording_id="recording-1"))
    proxy = SpeakerRecognitionConversation("conversation.source", 0.8, "proxy")
    proxy.hass = hass
    original_context = object()
    original = ConversationInput(
        text="Hello",
        context=original_context,
        conversation_id="conversation-2",
        device_id="device-1",
        satellite_id="assist_satellite.kitchen",
        language="nl",
        agent_id="conversation.proxy",
    )
    try:
        asyncio.run(proxy.async_process(original))
    finally:
        if previous is None:
            delattr(speaker_recognition_package, "get_main_entry")
        else:
            speaker_recognition_package.get_main_entry = previous

    assert hass.agent.received.context is original_context
    assert api.calls == [
        (
            "recording-1",
            {
                "forwarded": True,
                "reason": "person_context_submitted",
                "person_entity_id": "person.alice",
            },
        )
    ]
