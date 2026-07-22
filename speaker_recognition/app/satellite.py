"""Home Assistant satellite discovery and one-shot enrollment coordination."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from app.models import (
    AssistSatelliteInfo,
    AudioInput,
    HomeAssistantPersonInfo,
    SatelliteEnrollmentSession,
)

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
SESSION_TIMEOUT = timedelta(seconds=45)
COMPLETED_SESSION_TTL = timedelta(minutes=5)
START_CONVERSATION_FEATURE = 2


class HomeAssistantApiError(RuntimeError):
    """Raised when the Supervisor proxy to Home Assistant fails."""


class HomeAssistantClient:
    """Minimal client for the Home Assistant Core API via Supervisor."""

    def __init__(self, supervisor_url: str = SUPERVISOR_URL) -> None:
        self._url = f"{supervisor_url}/core/api"

    def _request(self, path: str, method: str = "GET", payload: dict | None = None):
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            raise HomeAssistantApiError("SUPERVISOR_TOKEN is unavailable")
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self._url}{path}",
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise HomeAssistantApiError(str(error)) from error

    def satellites(self) -> list[AssistSatelliteInfo]:
        states = self._request("/states")
        satellites = []
        for state in states:
            entity_id = str(state.get("entity_id", ""))
            attributes = state.get("attributes") or {}
            if not entity_id.startswith("assist_satellite."):
                continue
            try:
                supported_features = int(attributes.get("supported_features", 0))
            except (TypeError, ValueError):
                continue
            if not supported_features & START_CONVERSATION_FEATURE:
                continue
            satellites.append(
                AssistSatelliteInfo(
                    entity_id=entity_id,
                    name=attributes.get("friendly_name") or entity_id,
                    state=str(state.get("state", "unknown")),
                )
            )
        return sorted(satellites, key=lambda item: item.name.casefold())

    def persons(self) -> list[HomeAssistantPersonInfo]:
        """Return Home Assistant person entities available for optional mapping."""
        persons = []
        for state in self._request("/states"):
            entity_id = str(state.get("entity_id", ""))
            if not entity_id.startswith("person."):
                continue
            attributes = state.get("attributes") or {}
            persons.append(
                HomeAssistantPersonInfo(
                    entity_id=entity_id,
                    name=attributes.get("friendly_name") or entity_id,
                )
            )
        return sorted(persons, key=lambda item: item.name.casefold())

    def ask_for_enrollment_sample(self, satellite_entity_id: str) -> None:
        self._request(
            "/services/assist_satellite/ask_question?return_response",
            method="POST",
            payload={
                "entity_id": satellite_entity_id,
                "question": "Spreek nu.",
                "preannounce": True,
            },
        )

    def confirm_enrollment_sample(self, satellite_entity_id: str) -> None:
        """Audibly confirm completion and force the satellite back through idle."""
        self._request(
            "/services/assist_satellite/announce",
            method="POST",
            payload={
                "entity_id": satellite_entity_id,
                "message": "Opname voltooid.",
                "preannounce": False,
            },
        )


class SatelliteEnrollmentCoordinator:
    """Coordinate a single short-lived audio capture across App and integration."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._session: SatelliteEnrollmentSession | None = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _expire(self) -> None:
        session = self._session
        if session is None or session.status in {"expired", "cancelled"}:
            return
        now = self._now()
        deadline = (
            session.created_at + COMPLETED_SESSION_TTL
            if session.status in {"complete", "failed"}
            else session.expires_at
        )
        if now >= deadline:
            session.status = "expired"
            session.audio = None

    async def arm(self, satellite_entity_id: str) -> SatelliteEnrollmentSession:
        async with self._lock:
            self._expire()
            if self._session and self._session.status in {"armed", "capturing"}:
                raise ValueError("Er is al een Voice-opname actief")
            now = self._now()
            self._session = SatelliteEnrollmentSession(
                id=uuid.uuid4().hex,
                satellite_entity_id=satellite_entity_id,
                status="armed",
                created_at=now,
                expires_at=now + SESSION_TIMEOUT,
            )
            return self._session.model_copy(deep=True)

    async def claim(self) -> SatelliteEnrollmentSession | None:
        async with self._lock:
            self._expire()
            if self._session is None or self._session.status != "armed":
                return None
            self._session.status = "capturing"
            return self._session.model_copy(deep=True)

    async def peek_armed(self) -> SatelliteEnrollmentSession | None:
        """Return the current armed session without consuming its one-shot claim."""
        async with self._lock:
            self._expire()
            if self._session is None or self._session.status != "armed":
                return None
            return self._session.model_copy(deep=True)

    async def complete(self, session_id: str, audio: AudioInput) -> SatelliteEnrollmentSession:
        async with self._lock:
            self._expire()
            if self._session is None or self._session.id != session_id:
                raise KeyError(session_id)
            if self._session.status == "complete":
                return self._session.model_copy(deep=True)
            if self._session.status != "capturing":
                raise ValueError("Deze Voice-opname is niet actief")
            self._session.status = "complete"
            self._session.audio = audio
            return self._session.model_copy(deep=True)

    async def fail(self, session_id: str, error: str) -> None:
        async with self._lock:
            if self._session is None or self._session.id != session_id:
                return
            if self._session.status in {"armed", "capturing"}:
                self._session.status = "failed"
                self._session.error = error[:300]
                self._session.audio = None

    async def get(self, session_id: str) -> SatelliteEnrollmentSession:
        async with self._lock:
            self._expire()
            if self._session is None or self._session.id != session_id:
                raise KeyError(session_id)
            return self._session.model_copy(deep=True)

    async def cancel(self, session_id: str) -> None:
        async with self._lock:
            if self._session is None or self._session.id != session_id:
                raise KeyError(session_id)
            if self._session.status in {"armed", "capturing"}:
                self._session.status = "cancelled"
                self._session.audio = None
