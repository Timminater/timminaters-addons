"""Async client for the bundled Speaker Recognition App."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterable
from typing import Any

from aiohttp import ClientError, ClientSession


class SpeakerRecognitionApiError(Exception):
    """Base API error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = (
            "authentication_failed"
            if status in {401, 403}
            else "endpoint_not_found"
            if status == 404
            else "api_error"
        )


class SpeakerRecognitionApi:
    """Small dependency-free App API client."""

    def __init__(self, session: ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._policy: dict[str, Any] | None = None
        self._policy_cached_at = 0.0
        self._info: dict[str, Any] | None = None
        self._info_checked = False

    @property
    def server_info(self) -> dict[str, Any] | None:
        """Return cached optional capability metadata when supported."""
        return dict(self._info) if self._info is not None else None

    async def async_info(self) -> dict[str, Any] | None:
        """Read the versioned capability document; older APIs remain supported."""
        if self._info_checked:
            return self.server_info
        try:
            info = await self._request("GET", "/api/info", timeout=3)
        except SpeakerRecognitionApiError as error:
            if error.status == 404:
                self._info_checked = True
                return None
            raise
        if not isinstance(info, dict) or not isinstance(info.get("api_version"), (str, int)):
            raise SpeakerRecognitionApiError("Backend returned an invalid /api/info document")
        try:
            version = int(info["api_version"])
        except (TypeError, ValueError) as error:
            raise SpeakerRecognitionApiError(
                "Backend returned an invalid API version"
            ) from error
        if version < 1:
            raise SpeakerRecognitionApiError("Backend returned an invalid API version")
        self._info = info
        self._info_checked = True
        return self.server_info

    @property
    def cached_pipeline_policy(self) -> dict[str, Any]:
        """Return the last authenticated policy, or safe compatibility defaults."""
        return dict(
            self._policy
            or {
                "extraction_mode": "off",
                "unknown_speaker_policy": "allow",
                "audio_processing_backend": "df2_batch",
            }
        )

    async def async_pipeline_policy(self, *, max_age: float = 5.0) -> dict[str, Any]:
        """Fetch and briefly cache the global audio-pipeline policy."""
        now = time.monotonic()
        if self._policy is not None and now - self._policy_cached_at < max_age:
            return dict(self._policy)
        policy = await self._request("GET", "/api/pipeline-policy", timeout=3)
        extraction_mode = str(policy.get("extraction_mode", "off"))
        unknown_policy = str(policy.get("unknown_speaker_policy", "allow"))
        backend = str(policy.get("audio_processing_backend", "df2_batch"))
        if extraction_mode not in {"off", "compare", "before_stt"}:
            extraction_mode = "off"
        if unknown_policy not in {"allow", "block"}:
            unknown_policy = "allow"
        if backend not in {"df2_batch", "df3_streaming"}:
            backend = "df2_batch"
        self._policy = {
            **policy,
            "extraction_mode": extraction_mode,
            "unknown_speaker_policy": unknown_policy,
            "audio_processing_backend": backend,
        }
        self._policy_cached_at = now
        return dict(self._policy)

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

    async def async_analyze(
        self,
        pcm: bytes,
        sample_rate: int,
        *,
        source_entity_id: str,
        satellite_id: str | None,
        extraction_mode: str,
    ) -> dict[str, Any]:
        """Persist and analyse one normal Assist utterance."""
        if self._has_capability("binary_analyze"):
            params = {
                "source": "pipeline",
                "stt_entity_id": source_entity_id,
                "extraction_mode": extraction_mode,
            }
            if satellite_id:
                params["satellite_id"] = satellite_id
            return await self._request(
                "POST",
                "/api/analyze-binary",
                data=pcm,
                params=params,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Sample-Rate": str(sample_rate),
                    "X-Channels": "1",
                    "X-Audio-Format": "pcm_s16le",
                },
                timeout=45,
            )
        return await self._request(
            "POST",
            "/api/analyze",
            json={
                "audio": {
                    "audio_data": base64.b64encode(pcm).decode(),
                    "sample_rate": sample_rate,
                },
                "source": "pipeline",
                "stt_entity_id": source_entity_id,
                "satellite_id": satellite_id,
                "extraction_mode": extraction_mode,
            },
            timeout=45,
        )

    def _has_capability(self, capability: str) -> bool:
        """Read a capability from either supported `/api/info` shape."""
        info = self._info or {}
        capabilities = info.get("capabilities", [])
        if isinstance(capabilities, dict):
            return capabilities.get(capability) is True
        return isinstance(capabilities, list) and capability in capabilities

    async def async_analyze_stream(
        self,
        pcm_stream: AsyncIterable[bytes],
        sample_rate: int,
        *,
        source_entity_id: str,
        satellite_id: str | None,
    ) -> dict[str, Any]:
        """Upload PCM incrementally while the App advances stateful DF3."""
        headers = {
            **self._headers,
            "Content-Type": "application/octet-stream",
            "X-Audio-Sample-Rate": str(sample_rate),
            "X-STT-Entity-ID": source_entity_id,
        }
        if satellite_id:
            headers["X-Satellite-ID"] = satellite_id
        try:
            async with self._session.post(
                f"{self._url}/api/analyze-stream",
                headers=headers,
                data=pcm_stream,
                timeout=150,
            ) as response:
                if response.status >= 400:
                    detail = await response.text()
                    raise SpeakerRecognitionApiError(
                        f"App returned HTTP {response.status}: {detail[:200]}",
                        status=response.status,
                    )
                return await response.json()
        except (ClientError, TimeoutError) as error:
            raise SpeakerRecognitionApiError(str(error)) from error

    async def async_finalize_analysis(
        self, recording_id: str, details: dict[str, Any]
    ) -> None:
        """Attach STT/pipeline outcome metadata to a stored recording."""
        try:
            await self._request(
                "POST",
                f"/api/recordings/{recording_id}/finalize",
                json=details,
                expect_json=False,
                timeout=5,
            )
        except SpeakerRecognitionApiError as error:
            # A 2.0 App validates the old ``original|extracted`` enum. Retry
            # only that schema mismatch with its closest legacy terminology;
            # other API failures still reach the caller unchanged.
            if "HTTP 422" not in str(error):
                raise
            compatibility_details = dict(details)
            if compatibility_details.get("audio_variant") in {
                "isolated",
                "denoised",
            }:
                compatibility_details["audio_variant"] = "extracted"
            compatibility_details.pop("fallback_reason", None)
            compatibility_details.pop("quality", None)
            if compatibility_details == details:
                raise
            await self._request(
                "POST",
                f"/api/recordings/{recording_id}/finalize",
                json=compatibility_details,
                expect_json=False,
                timeout=5,
            )

    async def async_process_analysis(
        self, recording_id: str, speaker_id: str | None = None
    ) -> dict[str, Any]:
        """Start asynchronous denoising for a stored analysis.

        This is intentionally separate from ``async_analyze``: 2.0 backends do
        not expose the endpoint, while 2.1 backends can return a 202 job
        document without making the live STT path wait for UI processing.
        """
        return await self._request(
            "POST",
            f"/api/analysis/{recording_id}/process",
            json=({"speaker_id": speaker_id} if speaker_id else {}),
            timeout=5,
        )

    async def async_finalize_conversation(
        self,
        recording_id: str,
        *,
        forwarded: bool,
        reason: str,
        person_entity_id: str | None = None,
        person_entity_ids: list[str] | None = None,
        speaker_names: list[str] | None = None,
    ) -> None:
        """Record whether mapped person context reached the conversation agent."""
        await self._request(
            "POST",
            f"/api/recordings/{recording_id}/conversation",
            json={
                "conversation_forwarded": forwarded,
                "conversation_reason": reason,
                "person_entity_id": person_entity_id,
                "person_entity_ids": person_entity_ids or [],
                "speaker_names": speaker_names or [],
            },
            expect_json=False,
            timeout=5,
        )

    async def async_claim_satellite_enrollment(
        self, satellite_entity_id: str | None
    ) -> dict[str, Any] | None:
        result = await self._request(
            "POST",
            "/api/satellite-enrollment/claim",
            json={"satellite_entity_id": satellite_entity_id},
            timeout=2,
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
        data: bytes | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
        timeout: int = 30,
    ) -> Any:
        try:
            request_headers = self._headers.copy() if authenticated else {}
            if headers:
                request_headers.update(headers)
            request_kwargs: dict[str, Any] = {
                "headers": request_headers or None,
                "timeout": timeout,
            }
            if json is not None:
                request_kwargs["json"] = json
            if data is not None:
                request_kwargs["data"] = data
            if params is not None:
                request_kwargs["params"] = params
            async with self._session.request(
                method,
                f"{self._url}{path}",
                **request_kwargs,
            ) as response:
                if response.status >= 400:
                    detail = await response.text()
                    raise SpeakerRecognitionApiError(
                        f"App returned HTTP {response.status}: {detail[:200]}",
                        status=response.status,
                    )
                return await response.json() if expect_json else None
        except (ClientError, TimeoutError) as error:
            raise SpeakerRecognitionApiError(str(error)) from error
