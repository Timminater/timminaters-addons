"""Short-lived, consume-once recognition results for conversation context."""

from __future__ import annotations

from collections import deque
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

RESULT_TTL_SECONDS = 8.0
MAX_RESULTS = 20


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
    return selected.copy()
