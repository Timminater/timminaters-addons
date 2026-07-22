"""Short-lived, consume-once recognition results for conversation context."""

from __future__ import annotations

from collections import deque
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .const import (
    DIAGNOSTIC_RESET_SECONDS,
    DOMAIN,
    SIGNAL_CONTEXT_UPDATED,
    SIGNAL_RESULT_UPDATED,
)

RESULT_TTL_SECONDS = 8.0
MAX_RESULTS = 20
_RESET_TIMER_KEYS = {
    "last_result": "last_result_reset_timer",
    "last_conversation_context": "last_conversation_context_reset_timer",
}


def _schedule_diagnostic_reset(
    hass: HomeAssistant, key: str, signal: str, value: dict[str, Any]
) -> None:
    """Clear one diagnostic record after its short display window."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    timer_key = _RESET_TIMER_KEYS[key]
    if cancel := domain_data.pop(timer_key, None):
        cancel()

    reset_cancel = None

    def reset(_now) -> None:
        if domain_data.get(timer_key) is not reset_cancel:
            return
        domain_data.pop(timer_key, None)
        # The identity check above also ignores a stale callback if a newer
        # timer and record replaced this value.
        if domain_data.get(key) is not value:
            return
        domain_data[key] = None
        async_dispatcher_send(hass, signal)

    reset_cancel = async_call_later(
        hass, DIAGNOSTIC_RESET_SECONDS, reset
    )
    domain_data[timer_key] = reset_cancel


def cancel_diagnostic_timers(hass: HomeAssistant) -> None:
    """Cancel pending diagnostic resets when the backend entry unloads."""
    domain_data = hass.data.get(DOMAIN, {})
    for timer_key in _RESET_TIMER_KEYS.values():
        if cancel := domain_data.pop(timer_key, None):
            cancel()


def listening_satellite(hass: HomeAssistant) -> str | None:
    """Return the sole listening Assist satellite, if it is unambiguous."""
    listening = [
        state.entity_id
        for state in hass.states.async_all(["assist_satellite"])
        if state.state == "listening"
    ]
    return listening[0] if len(listening) == 1 else None


def remember_result(hass: HomeAssistant, result: dict[str, Any]) -> None:
    """Store bounded recognition metadata; audio is never retained here."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    results = domain_data.setdefault("recognition_results", deque(maxlen=MAX_RESULTS))
    results.append(result)
    domain_data["last_result"] = result
    async_dispatcher_send(hass, SIGNAL_RESULT_UPDATED)
    _schedule_diagnostic_reset(hass, "last_result", SIGNAL_RESULT_UPDATED, result)


def remember_conversation_context(hass: HomeAssistant, context: dict[str, Any]) -> None:
    """Expose whether speaker personalization was submitted to the source agent."""
    hass.data.setdefault(DOMAIN, {})["last_conversation_context"] = context
    async_dispatcher_send(hass, SIGNAL_CONTEXT_UPDATED)
    _schedule_diagnostic_reset(
        hass, "last_conversation_context", SIGNAL_CONTEXT_UPDATED, context
    )


def consume_result(
    hass: HomeAssistant, satellite_id: str | None, min_confidence: float
) -> dict[str, Any] | None:
    """Atomically consume one fresh result matching the conversation source."""
    results = hass.data.setdefault(DOMAIN, {}).setdefault(
        "recognition_results", deque(maxlen=MAX_RESULTS)
    )
    now = hass.loop.time()
    fresh = [item for item in results if now - item["timestamp"] <= RESULT_TTL_SECONDS]
    results.clear()
    results.extend(fresh)

    candidates = [
        item
        for item in fresh
        if item.get("matched")
        and float(item.get("confidence", 0.0)) >= min_confidence
        and item.get("person_entity_id")
        and not item.get("consumed")
    ]
    # A voice match is only safe personalization metadata when both pipeline
    # stages identify the same Assist satellite. Browser/mobile conversations
    # without a satellite id must never inherit a nearby voice result.
    if not satellite_id:
        return None
    candidates = [
        item for item in candidates if item.get("satellite_id") == satellite_id
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: item["timestamp"])
    if hass.states.get(selected["person_entity_id"]) is None:
        return None
    selected["consumed"] = True
    selected["conversation_claimed"] = True
    async_dispatcher_send(hass, SIGNAL_RESULT_UPDATED)
    return selected.copy()


def claim_result_for_conversation(
    hass: HomeAssistant, satellite_id: str | None
) -> dict[str, Any] | None:
    """Claim fresh exact-source diagnostics when no person context was eligible."""
    if not satellite_id:
        return None
    results = hass.data.setdefault(DOMAIN, {}).setdefault(
        "recognition_results", deque(maxlen=MAX_RESULTS)
    )
    now = hass.loop.time()
    candidates = [
        item
        for item in results
        if now - item["timestamp"] <= RESULT_TTL_SECONDS
        and item.get("satellite_id") == satellite_id
        and not item.get("conversation_claimed")
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: item["timestamp"])
    selected["conversation_claimed"] = True
    return selected.copy()
