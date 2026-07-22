"""FastAPI service and ingress-safe web UI."""

from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings
from app.models import (
    EnrollmentRequest,
    EnrollmentResult,
    HealthResponse,
    RecognitionRequest,
    RecognitionResult,
    SpeakerInfo,
)
from app.recognizer import SpeakerRecognizer

_LOGGER = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent.parent / "web"
settings = Settings.load()
recognizer = SpeakerRecognizer(
    data_dir=settings.data_dir,
    threshold=settings.recognition_threshold,
    max_audio_seconds=settings.max_audio_seconds,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(recognizer.initialize)
    yield


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
            if int(content_length) > 64 * 1024 * 1024:
                return Response(content="Request body is too large", status_code=413)
        except ValueError:
            return Response(content="Invalid Content-Length", status_code=400)
    return await call_next(request)


def authorize_api(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Trust Supervisor ingress, otherwise require the configured API token."""
    via_ingress = bool(
        request.headers.get("x-ingress-path")
        or request.headers.get("x-remote-user-id")
        or request.headers.get("x-hass-user-id")
    )
    if via_ingress:
        return
    if settings.api_token and authorization == f"Bearer {settings.api_token}":
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
        )
        return EnrollmentResult(speaker=speaker)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


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
