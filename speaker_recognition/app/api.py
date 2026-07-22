"""FastAPI service and ingress-safe web UI."""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import secrets
import socket
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings
from app.models import (
    AssistSatelliteInfo,
    EnrollmentRequest,
    EnrollmentResult,
    HealthResponse,
    HomeAssistantPersonInfo,
    RecognitionRequest,
    RecognitionResult,
    SpeakerInfo,
    SatelliteEnrollmentClaim,
    SatelliteEnrollmentCompleteRequest,
    SatelliteEnrollmentFailureRequest,
    SatelliteEnrollmentSession,
    SatelliteEnrollmentStartRequest,
)
from app.recognizer import SpeakerRecognizer
from app.satellite import (
    HomeAssistantApiError,
    HomeAssistantClient,
    SatelliteEnrollmentCoordinator,
)

_LOGGER = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent.parent / "web"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
settings = Settings.load()
recognizer = SpeakerRecognizer(
    data_dir=settings.data_dir,
    threshold=settings.recognition_threshold,
    max_audio_seconds=settings.max_audio_seconds,
)
home_assistant = HomeAssistantClient()
satellite_enrollment = SatelliteEnrollmentCoordinator()
satellite_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(recognizer.initialize)
    try:
        yield
    finally:
        for task in satellite_tasks:
            task.cancel()
        if satellite_tasks:
            await asyncio.gather(*satellite_tasks, return_exceptions=True)


app = FastAPI(
    title="Speaker Recognition",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return Response(content="Request body is too large", status_code=413)
        except ValueError:
            return Response(content="Invalid Content-Length", status_code=400)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            return Response(content="Request body is too large", status_code=413)
    request._body = bytes(body)  # Starlette's downstream parser reuses this bounded body.
    return await call_next(request)


@lru_cache(maxsize=1)
def _supervisor_addresses() -> frozenset[str]:
    """Resolve the trusted Supervisor proxy addresses on the internal network."""
    try:
        return frozenset(
            item[4][0] for item in socket.getaddrinfo("supervisor", None, type=socket.SOCK_STREAM)
        )
    except socket.gaierror:
        return frozenset()


def _is_supervisor_request(request: Request) -> bool:
    return bool(request.client and request.client.host in _supervisor_addresses())


def authorize_api(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Trust Supervisor ingress, otherwise require the configured API token."""
    via_ingress = _is_supervisor_request(request) and bool(
        request.headers.get("x-ingress-path")
        or request.headers.get("x-remote-user-id")
        or request.headers.get("x-hass-user-id")
    )
    if via_ingress:
        return
    accepted_tokens = {settings.companion_token}
    if settings.api_token:
        accepted_tokens.add(settings.api_token)
    if authorization and authorization.startswith("Bearer "):
        supplied_token = authorization[len("Bearer ") :]
        if any(secrets.compare_digest(supplied_token, token) for token in accepted_tokens):
            return
    if not settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Direct API access is disabled; use Home Assistant ingress or configure api_token",
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(
        status="healthy" if recognizer.ready else "starting",
        ready=recognizer.ready,
        speakers=len(recognizer.list_speakers()),
    )


@app.get("/api/speakers", response_model=list[SpeakerInfo], dependencies=[Depends(authorize_api)])
async def list_speakers() -> list[SpeakerInfo]:
    return recognizer.list_speakers()


@app.post("/api/enroll", response_model=EnrollmentResult, dependencies=[Depends(authorize_api)])
async def enroll(request: EnrollmentRequest) -> EnrollmentResult:
    try:
        speaker = await asyncio.to_thread(
            recognizer.enroll,
            request.speaker_name,
            [sample.audio for sample in request.samples],
            request.replace,
            request.person_entity_id,
            "person_entity_id" in request.model_fields_set,
        )
        return EnrollmentResult(speaker=speaker)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get(
    "/api/assist-satellites",
    response_model=list[AssistSatelliteInfo],
    dependencies=[Depends(authorize_api)],
)
async def assist_satellites() -> list[AssistSatelliteInfo]:
    try:
        return await asyncio.to_thread(home_assistant.satellites)
    except HomeAssistantApiError as error:
        raise HTTPException(
            status_code=502, detail=f"Home Assistant is niet bereikbaar: {error}"
        ) from error


@app.get(
    "/api/home-assistant-persons",
    response_model=list[HomeAssistantPersonInfo],
    dependencies=[Depends(authorize_api)],
)
async def home_assistant_persons() -> list[HomeAssistantPersonInfo]:
    """List people for an optional, non-authorizing speaker association."""
    try:
        return await asyncio.to_thread(home_assistant.persons)
    except HomeAssistantApiError as error:
        raise HTTPException(
            status_code=502, detail=f"Home Assistant is niet bereikbaar: {error}"
        ) from error


@app.post(
    "/api/satellite-enrollment",
    response_model=SatelliteEnrollmentSession,
    dependencies=[Depends(authorize_api)],
)
async def start_satellite_enrollment(
    request: SatelliteEnrollmentStartRequest,
) -> SatelliteEnrollmentSession:
    try:
        satellites = await asyncio.to_thread(home_assistant.satellites)
        satellite = next(
            (item for item in satellites if item.entity_id == request.satellite_entity_id), None
        )
        if satellite is None:
            raise HTTPException(status_code=404, detail="Voice-apparaat niet gevonden")
        if satellite.state != "idle":
            raise HTTPException(
                status_code=409,
                detail=f"Voice-apparaat is niet beschikbaar (status: {satellite.state})",
            )
        session = await satellite_enrollment.arm(request.satellite_entity_id)
        task = asyncio.create_task(
            _run_satellite_prompt(session.id, request.satellite_entity_id),
            name=f"speaker-recognition-enrollment-{session.id}",
        )
        satellite_tasks.add(task)
        task.add_done_callback(satellite_tasks.discard)
        return session
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except HomeAssistantApiError as error:
        raise HTTPException(
            status_code=502, detail=f"Home Assistant is niet bereikbaar: {error}"
        ) from error


async def _run_satellite_prompt(session_id: str, satellite_entity_id: str) -> None:
    """Run the blocking HA question while the GUI polls the enrollment session."""
    try:
        await asyncio.to_thread(
            home_assistant.ask_for_enrollment_sample, satellite_entity_id
        )
    except HomeAssistantApiError as error:
        await satellite_enrollment.fail(session_id, f"Kon Voice-apparaat niet starten: {error}")
        return
    try:
        result = await satellite_enrollment.get(session_id)
    except KeyError:
        return
    if result.status == "armed":
        await satellite_enrollment.fail(
            session_id,
            "Geen audio ontvangen. Gebruik in deze Assist-pipeline de Speaker "
            "Recognition STT-proxy.",
        )


@app.post(
    "/api/satellite-enrollment/claim",
    response_model=SatelliteEnrollmentClaim,
    dependencies=[Depends(authorize_api)],
)
async def claim_satellite_enrollment() -> SatelliteEnrollmentClaim:
    # SpeechMetadata does not identify its originating satellite. Only accept a
    # stream while the selected satellite is the sole listening satellite.
    try:
        armed = await satellite_enrollment.peek_armed()
        if armed is None:
            return SatelliteEnrollmentClaim()
        satellites = await asyncio.to_thread(home_assistant.satellites)
        listening = [item.entity_id for item in satellites if item.state == "listening"]
        if listening != [armed.satellite_entity_id]:
            return SatelliteEnrollmentClaim()
        return SatelliteEnrollmentClaim(session=await satellite_enrollment.claim())
    except HomeAssistantApiError as error:
        raise HTTPException(
            status_code=502, detail=f"Home Assistant is niet bereikbaar: {error}"
        ) from error


@app.post(
    "/api/satellite-enrollment/{session_id}/complete",
    response_model=SatelliteEnrollmentSession,
    dependencies=[Depends(authorize_api)],
)
async def complete_satellite_enrollment(
    session_id: str, request: SatelliteEnrollmentCompleteRequest
) -> SatelliteEnrollmentSession:
    try:
        try:
            pcm = base64.b64decode(request.audio.audio_data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Audio data is not valid base64") from error
        if not pcm or len(pcm) % 2:
            raise ValueError("Audio must contain signed 16-bit PCM samples")
        max_bytes = request.audio.sample_rate * settings.max_audio_seconds * 2
        if len(pcm) > max_bytes:
            raise ValueError(f"Audio exceeds the {settings.max_audio_seconds} second limit")
        return await satellite_enrollment.complete(session_id, request.audio)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Voice-opname niet gevonden") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post(
    "/api/satellite-enrollment/{session_id}/fail",
    status_code=204,
    dependencies=[Depends(authorize_api)],
)
async def fail_satellite_enrollment(
    session_id: str, request: SatelliteEnrollmentFailureRequest
) -> Response:
    await satellite_enrollment.fail(session_id, request.error)
    return Response(status_code=204)


@app.get(
    "/api/satellite-enrollment/{session_id}",
    response_model=SatelliteEnrollmentSession,
    dependencies=[Depends(authorize_api)],
)
async def get_satellite_enrollment(session_id: str) -> SatelliteEnrollmentSession:
    try:
        return await satellite_enrollment.get(session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Voice-opname niet gevonden") from error


@app.delete(
    "/api/satellite-enrollment/{session_id}",
    status_code=204,
    dependencies=[Depends(authorize_api)],
)
async def cancel_satellite_enrollment(session_id: str) -> Response:
    try:
        await satellite_enrollment.cancel(session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Voice-opname niet gevonden") from error
    return Response(status_code=204)


@app.delete("/api/speakers/{speaker_id}", status_code=204, dependencies=[Depends(authorize_api)])
async def delete_speaker(speaker_id: str) -> Response:
    if not await asyncio.to_thread(recognizer.delete, speaker_id):
        raise HTTPException(status_code=404, detail="Speaker not found")
    return Response(status_code=204)


@app.post("/api/recognize", response_model=RecognitionResult, dependencies=[Depends(authorize_api)])
async def recognize(request: RecognitionRequest) -> RecognitionResult:
    try:
        speaker, confidence, scores = await asyncio.to_thread(recognizer.recognize, request.audio)
        return RecognitionResult(
            matched=speaker is not None,
            speaker=speaker,
            confidence=confidence,
            threshold=settings.recognition_threshold,
            scores=scores,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/", include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    base_path = request.headers.get("x-ingress-path", "/").strip()
    if not base_path.startswith("/") or ".." in base_path:
        base_path = "/"
    base_path = f"{base_path.rstrip('/')}/"
    document = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    document = document.replace("__BASE_PATH__", html.escape(base_path, quote=True))
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
                "worker-src 'self' blob:; frame-ancestors 'self'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")
