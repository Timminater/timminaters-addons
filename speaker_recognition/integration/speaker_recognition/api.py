"""Async client for the bundled Speaker Recognition App."""

from __future__ import annotations

import base64
from typing import Any

from aiohttp import ClientError, ClientSession


class SpeakerRecognitionApiError(Exception):
    """Base API error."""


class SpeakerRecognitionApi:
    """Small dependency-free App API client."""

    def __init__(self, session: ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def async_health(self) -> dict[str, Any]:
        return await self._request("GET", "/health", authenticated=False)

    async def async_speakers(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/speakers")

    async def async_recognize(self, pcm: bytes, sample_rate: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/recognize",
            json={
                "audio": {
                    "audio_data": base64.b64encode(pcm).decode(),
                    "sample_rate": sample_rate,
                }
            },
        )

    async def async_claim_satellite_enrollment(self) -> dict[str, Any] | None:
        result = await self._request(
            "POST", "/api/satellite-enrollment/claim", json={}, timeout=2
        )
        return result.get("session")

    async def async_complete_satellite_enrollment(
        self, session_id: str, pcm: bytes, sample_rate: int
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/satellite-enrollment/{session_id}/complete",
            json={
                "audio": {
                    "audio_data": base64.b64encode(pcm).decode(),
                    "sample_rate": sample_rate,
                }
            },
        )

    async def async_fail_satellite_enrollment(self, session_id: str, error: str) -> None:
        await self._request(
            "POST",
            f"/api/satellite-enrollment/{session_id}/fail",
            json={"error": error},
            expect_json=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        json: dict | None = None,
        expect_json: bool = True,
        timeout: int = 30,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                headers=self._headers if authenticated else None,
                json=json,
                timeout=timeout,
            ) as response:
                if response.status >= 400:
                    detail = await response.text()
                    raise SpeakerRecognitionApiError(
                        f"App returned HTTP {response.status}: {detail[:200]}"
                    )
                return await response.json() if expect_json else None
        except (ClientError, TimeoutError) as error:
            raise SpeakerRecognitionApiError(str(error)) from error
