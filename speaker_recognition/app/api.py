"""FastAPI service and ingress-safe web UI."""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import queue
import secrets
import socket
import time
import uuid
import wave
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.audio_quality import assess_pcm16_mono
from app.config import Settings
from app.diarization import TimelineStore, analyze_timeline
from app.models import (
    AudioInput,
    AudioQualityRequest,
    AssistSatelliteInfo,
    AnalyzeRequest,
    BulkDeleteRequest,
    DeleteUnindexedAudioRequest,
    CalibrationApplyRequest,
    ConversationRecordingRequest,
    DeleteSpeakerRequest,
    ExtractRequest,
    FinalizeRecordingRequest,
    EnrollmentRequest,
    EnrollmentResult,
    HealthResponse,
    PipelinePolicy,
    PipelinePolicyPatch,
    ProcessTargetAudioRequest,
    PromoteRecordingRequest,
    HomeAssistantPersonInfo,
    MergeProfilesRequest,
    RecognitionRequest,
    RecognitionResult,
    SpeakerInfo,
    SampleActiveRequest,
    SatelliteEnrollmentClaimRequest,
    SatelliteEnrollmentClaim,
    SatelliteEnrollmentCompleteRequest,
    SatelliteEnrollmentFailureRequest,
    SatelliteEnrollmentSession,
    SatelliteEnrollmentStartRequest,
)
from app.multipart_pcm import MultipartPcmError, read_multipart_pcm
from app.recognizer import SpeakerRecognizer
from app.review_features import device_quality, list_review_inbox, preview_experiment, set_review
from app.satellite import (
    HomeAssistantApiError,
    HomeAssistantClient,
    SatelliteEnrollmentCoordinator,
)

_LOGGER = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent.parent / "web"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
STREAM_FINALIZE_TIMEOUT_SECONDS = 12
settings = Settings.load()
recognizer = SpeakerRecognizer(
    data_dir=settings.data_dir,
    threshold=settings.recognition_threshold,
    max_audio_seconds=settings.max_audio_seconds,
    audio_processing_backend=settings.audio_processing_backend,
)
timeline_store = TimelineStore(recognizer.catalog)
home_assistant = HomeAssistantClient()
satellite_enrollment = SatelliteEnrollmentCoordinator()
satellite_tasks: set[asyncio.Task] = set()
processing_tasks: dict[str, asyncio.Task] = {}
timeline_tasks: dict[str, asyncio.Task] = {}
timeline_slots = asyncio.Semaphore(1)
experiment_tasks: dict[str, asyncio.Task] = {}
experiment_results: dict[str, dict] = {}
maintenance_task: asyncio.Task | None = None
_policy: dict[str, object] = {
    "unknown_speaker_policy": "allow", "extraction_mode": "off",
    "min_margin": 0.0, "retention_days": 7,
    "max_storage_bytes": 2 * 1024 * 1024 * 1024,
    "audio_processing_backend": settings.audio_processing_backend,
    "analysis_audio_retention": "all",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global maintenance_task, timeline_store
    await asyncio.to_thread(recognizer.initialize)
    timeline_store = TimelineStore(recognizer.catalog)
    await asyncio.to_thread(timeline_store.initialize)
    await asyncio.to_thread(recognizer.expire_guest_profiles)
    saved_policy = recognizer.catalog.get_setting("pipeline_policy", {})
    first_policy_load = not isinstance(saved_policy, dict) or not saved_policy
    if isinstance(saved_policy, dict):
        candidate = {**_policy, **{key: value for key, value in saved_policy.items() if key in _policy}}
        try:
            validated = PipelinePolicy(
                recognition_threshold=settings.recognition_threshold, **candidate
            )
        except ValueError:
            _LOGGER.exception("Invalid saved pipeline policy; retaining safe defaults")
        else:
            _policy.update({key: getattr(validated, key) for key in _policy})
    recognizer.catalog.retention_days = int(_policy["retention_days"])
    recognizer.catalog.max_storage_bytes = int(_policy["max_storage_bytes"])
    if first_policy_load:
        recognizer.catalog.set_setting("pipeline_policy", _policy)
    await asyncio.to_thread(
        recognizer.catalog.reconcile_audio_retention,
        str(_policy["analysis_audio_retention"]),
    )
    await asyncio.to_thread(recognizer.catalog.cleanup)
    recognizer.configure_audio_processing_backend(
        str(_policy["audio_processing_backend"])
    )
    # Start the multiprocessing worker from the main server thread before the
    # app accepts traffic. Forking it from asyncio's thread pool is unsafe on
    # Linux once other worker threads exist.
    recognizer.warm_audio_processor()
    maintenance_task = asyncio.create_task(_catalogue_maintenance(), name="speaker-recognition-catalogue-cleanup")
    try:
        yield
    finally:
        if maintenance_task:
            maintenance_task.cancel()
            await asyncio.gather(maintenance_task, return_exceptions=True)
            maintenance_task = None
        for task in satellite_tasks:
            task.cancel()
        if satellite_tasks:
            await asyncio.gather(*satellite_tasks, return_exceptions=True)
        for task in processing_tasks.values():
            task.cancel()
        if processing_tasks:
            await asyncio.gather(*processing_tasks.values(), return_exceptions=True)
            processing_tasks.clear()
        for task in timeline_tasks.values():
            task.cancel()
        if timeline_tasks:
            await asyncio.gather(*timeline_tasks.values(), return_exceptions=True)
            timeline_tasks.clear()
        for task in experiment_tasks.values():
            task.cancel()
        if experiment_tasks:
            await asyncio.gather(*experiment_tasks.values(), return_exceptions=True)
            experiment_tasks.clear()
        await asyncio.to_thread(recognizer.close)


async def _catalogue_maintenance() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(recognizer.expire_guest_profiles)
            await asyncio.to_thread(
                recognizer.catalog.cleanup,
                None,
                set(processing_tasks) | set(timeline_tasks),
            )
        except Exception:  # cleanup must never take down recognition
            _LOGGER.exception("Could not clean up expired analysis recordings")


app = FastAPI(
    title="Speaker Recognition",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def structured_http_error(_request: Request, error: HTTPException) -> JSONResponse:
    """Stable error codes without breaking clients that still read detail."""
    detail = error.detail
    code = detail.get("code") if isinstance(detail, dict) else None
    message = detail.get("message") or detail.get("error") if isinstance(detail, dict) else detail
    if not isinstance(message, str):
        message = "The request could not be completed"
    return JSONResponse(
        status_code=error.status_code,
        content={
            "detail": detail,
            "error": {
                "code": code or f"http_{error.status_code}",
                "message": message,
            },
        },
        headers=error.headers,
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
    # This route enforces its limit while consuming ASGI chunks. Buffering it
    # in middleware would turn stateful processing back into disguised batch.
    if request.url.path == "/api/analyze-stream":
        return await call_next(request)
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
        speakers=recognizer.speaker_count,
    )


@app.get("/api/info", dependencies=[Depends(authorize_api)])
async def api_info() -> dict:
    """Version handshake and effective configuration for the companion."""
    return {
        "api_version": 2,
        "app_version": __version__,
        "capabilities": ["analysis_v2", "binary_analyze", "multipart_enroll", "audio_quality", "df3_streaming", "processing_status", "review_inbox", "guest_profiles", "profile_merge", "device_quality", "experiment_preview", "offline_diarization_experimental"],
        "configured_audio_processing_backend": _policy["audio_processing_backend"],
        "component_versions": {
            "app": __version__,
            "integration": __version__,
            "recognizer": _installed_version("resemblyzer"),
            "denoiser": _installed_version("deepfilternet"),
            "torch": _installed_version("torch"),
            "torchaudio": _installed_version("torchaudio"),
            "soxr": _installed_version("soxr"),
        },
        "recognition_model": "resemblyzer",
        "recognition_runtime": "pytorch_cpu",
        "max_audio_seconds": settings.max_audio_seconds,
    }


@lru_cache(maxsize=16)
def _installed_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


@app.get("/api/speakers", response_model=list[SpeakerInfo], dependencies=[Depends(authorize_api)])
async def list_speakers() -> list[SpeakerInfo]:
    return recognizer.list_speakers()


@app.post("/api/speakers/merge", response_model=SpeakerInfo, dependencies=[Depends(authorize_api)])
async def merge_speakers(request: MergeProfilesRequest) -> SpeakerInfo:
    try:
        return await asyncio.to_thread(recognizer.merge_profiles, request.source_id, request.target_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={"code": "speaker_not_found", "message": "Speaker profile not found"}) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail={"code": "profile_merge_failed", "message": str(error)}) from error


def _registration_quality_reports(samples: list[AudioInput], accept_warnings: bool) -> list[dict]:
    reports = [
        assess_pcm16_mono(
            recognizer._decode_pcm_bytes(sample), sample.sample_rate,
            purpose="registration",
        )
        for sample in samples
    ]
    if any(not report["accepted"] for report in reports):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "registration_audio_rejected",
                "message": "Recording rejected: add at least 0.6 seconds of clear speech and avoid clipping.",
                "quality_reports": reports,
            },
        )
    if not accept_warnings and any(report["decision"] == "review" for report in reports):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "registration_quality_review_required",
                "message": "Review the recording quality warnings before adding this voice sample.",
                "quality_reports": reports,
            },
        )
    return reports


@app.post("/api/audio-quality", dependencies=[Depends(authorize_api)])
async def audio_quality(request: AudioQualityRequest) -> dict:
    try:
        return await asyncio.to_thread(
            assess_pcm16_mono,
            recognizer._decode_pcm_bytes(request.audio),
            request.audio.sample_rate,
            purpose=request.purpose,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/enroll", response_model=EnrollmentResult, dependencies=[Depends(authorize_api)])
async def enroll(request: EnrollmentRequest) -> EnrollmentResult:
    try:
        quality_reports = await asyncio.to_thread(
            _registration_quality_reports,
            [sample.audio for sample in request.samples],
            request.accept_quality_warnings,
        )
        speaker = await asyncio.to_thread(
            recognizer.enroll,
            request.speaker_name,
            [sample.audio for sample in request.samples],
            request.replace,
            request.person_entity_id,
            "person_entity_id" in request.model_fields_set,
            request.profile_kind,
            request.expires_at,
            "expires_at" in request.model_fields_set,
            "profile_kind" in request.model_fields_set,
        )
        return EnrollmentResult(speaker=speaker, quality_reports=quality_reports)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/enroll-multipart", response_model=EnrollmentResult, dependencies=[Depends(authorize_api)])
async def enroll_multipart(
    request: Request,
    speaker_name: str,
    x_sample_rate: int = Header(),
    x_channels: int = Header(),
    x_audio_format: str = Header(),
    replace_existing: bool = False,
    accept_quality_warnings: bool = False,
    person_entity_id: str | None = None,
    profile_kind: Literal["resident", "guest"] = "resident",
    expires_at: datetime | None = None,
    never_expires: bool = False,
) -> EnrollmentResult:
    """Enroll multiple bounded PCM clips without JSON/base64 transport."""
    if x_channels != 1 or x_audio_format != "pcm_s16le":
        raise HTTPException(status_code=415, detail="Expected mono pcm_s16le audio")
    try:
        recordings = await read_multipart_pcm(
            request.stream(), request.headers.get("content-type"), x_sample_rate,
        )
        enrollment_fields = dict(
            speaker_name=speaker_name,
            samples=[{"audio": {
                "audio_data": base64.b64encode(item.pcm).decode("ascii"),
                "sample_rate": item.sample_rate,
            }} for item in recordings],
            replace=replace_existing,
            accept_quality_warnings=accept_quality_warnings,
            person_entity_id=person_entity_id,
            profile_kind=profile_kind,
        )
        if never_expires:
            enrollment_fields["expires_at"] = None
        elif expires_at is not None:
            enrollment_fields["expires_at"] = expires_at
        validated = EnrollmentRequest(**enrollment_fields)
    except MultipartPcmError as error:
        code = str(error)
        raise HTTPException(
            status_code=413 if code in {"request_too_large", "recording_too_large", "recording_too_long"} else 400,
            detail=code,
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        quality_reports = await asyncio.to_thread(
            _registration_quality_reports,
            [sample.audio for sample in validated.samples],
            validated.accept_quality_warnings,
        )
        speaker = await asyncio.to_thread(
            recognizer.enroll,
            validated.speaker_name,
            [sample.audio for sample in validated.samples],
            validated.replace,
            validated.person_entity_id,
            "person_entity_id" in request.query_params,
            validated.profile_kind,
            validated.expires_at,
            "expires_at" in validated.model_fields_set,
            "profile_kind" in request.query_params,
        )
        return EnrollmentResult(speaker=speaker, quality_reports=quality_reports)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _policy_response() -> PipelinePolicy:
    calibration = recognizer.catalog.calibration()
    return PipelinePolicy(
        recognition_threshold=float(calibration["threshold"]) if calibration else settings.recognition_threshold,
        calibration=calibration,
        **_policy,
    )


@app.get("/api/pipeline-policy", response_model=PipelinePolicy, dependencies=[Depends(authorize_api)])
async def get_pipeline_policy() -> PipelinePolicy:
    return _policy_response()


@app.patch("/api/pipeline-policy", response_model=PipelinePolicy, dependencies=[Depends(authorize_api)])
async def patch_pipeline_policy(request: PipelinePolicyPatch) -> PipelinePolicy:
    changes = request.model_dump(exclude_none=True)
    for key, value in changes.items():
        _policy[key] = value
    if "audio_processing_backend" in changes:
        recognizer.configure_audio_processing_backend(
            str(_policy["audio_processing_backend"])
        )
    recognizer.catalog.retention_days = int(_policy["retention_days"])
    recognizer.catalog.max_storage_bytes = int(_policy["max_storage_bytes"])
    recognizer.catalog.set_setting("pipeline_policy", _policy)
    if {"retention_days", "max_storage_bytes"} & changes.keys():
        await asyncio.to_thread(
            recognizer.catalog.cleanup,
            None,
            set(processing_tasks),
        )
    return _policy_response()


def _analysis_payload(recording: dict, detailed=None, *, include_audio: bool = False) -> dict:
    result = dict(recording)
    result["timings"] = _merge_processing_timings(
        result.get("timings"), result.get("processing_timings")
    )
    original_path = result.pop("original_path", None)
    extracted_path = result.pop("extracted_path", None)
    denoised_path = result.pop("denoised_path", None)
    isolated_path = result.pop("isolated_path", None)
    result["original_available"] = bool(original_path and Path(original_path).is_file())
    result["denoised_available"] = bool(denoised_path and Path(denoised_path).is_file())
    result["isolated_available"] = bool(isolated_path and Path(isolated_path).is_file())
    result["legacy_extracted_available"] = bool(extracted_path and Path(extracted_path).is_file())
    # Compatibility flag: legacy clients still request the extracted player.
    result["extracted_available"] = (
        result["isolated_available"] or result["legacy_extracted_available"]
    )
    result["available_audio_variants"] = [
        variant
        for variant, available in (
            ("original", result["original_available"]),
            ("denoised", result["denoised_available"]),
            ("isolated", result["isolated_available"]),
        )
        if available
    ]
    labels = result.get("labels") if isinstance(result.get("labels"), dict) else {}
    result["audio_quality"] = labels.get("audio_quality")
    for key in (
        "audio_variant", "fallback", "conversation_reason", "person_entity_id",
        "person_entity_ids", "speaker_names",
    ):
        if key in labels:
            result[key] = labels[key]
    result["conversation_person_entity_id"] = labels.get("person_entity_id")
    result["conversation_person_entity_ids"] = labels.get("person_entity_ids", [])
    result["conversation_speaker_names"] = labels.get("speaker_names", [])
    detected_speakers = (
        detailed.detected_speakers
        if detailed is not None
        else labels.get("detected_speakers", [])
    )
    profiles = {item.id: item for item in recognizer.list_speakers()}
    result["detected_speakers"] = []
    for detected in detected_speakers if isinstance(detected_speakers, list) else []:
        if not isinstance(detected, dict):
            continue
        item = dict(detected)
        profile = profiles.get(str(item.get("speaker_id")))
        if profile is not None:
            item["speaker_name"] = profile.name
            item["person_entity_id"] = profile.person_entity_id
        result["detected_speakers"].append(item)
    result["multiple_speakers"] = (
        result.get("outcome") == "multiple_speakers"
        or len(result["detected_speakers"]) > 1
    )
    result["blocked"] = result.get("outcome") == "blocked"
    result["matched"] = result.get("outcome") == "matched"
    result["threshold_source"] = (
        "calibration" if recognizer.catalog.calibration() else "configuration"
    )
    segments = result.get("segments") if isinstance(result.get("segments"), list) else []
    result["candidate_count"] = len(segments)
    if segments and result.get("speaker_name"):
        speaker_name = result["speaker_name"]
        scored_segments = [
            item for item in segments
            if isinstance(item, dict) and isinstance(item.get("scores"), dict)
        ]
        if scored_segments:
            best = max(
                scored_segments,
                key=lambda item: float(item["scores"].get(speaker_name, -1.0)),
            )
            result["best_segment"] = {
                "start_seconds": best.get("start_seconds"),
                "end_seconds": best.get("end_seconds"),
            }
    result["recognized_person_entity_id"] = None
    if result.get("speaker_id"):
        profile = next(
            (item for item in recognizer.list_speakers() if item.id == result["speaker_id"]),
            None,
        )
        if profile is not None:
            result["recognized_person_entity_id"] = profile.person_entity_id
            if not result.get("person_entity_id"):
                result["person_entity_id"] = profile.person_entity_id
    if detailed is not None:
        result.update({
            "matched": detailed.speaker is not None, "speaker": detailed.speaker.model_dump(mode="json") if detailed.speaker else None,
            "confidence": detailed.confidence, "margin": detailed.margin, "threshold": detailed.threshold,
            "threshold_source": "calibration" if recognizer.catalog.calibration() else "configuration",
            "best_segment": detailed.best_segment, "candidate_count": len(detailed.candidates),
            "detected_speakers": detailed.detected_speakers,
            "multiple_speakers": len(detailed.detected_speakers) > 1,
        })
        if include_audio:
            pcm = detailed.extracted_pcm or detailed.canonical_pcm
            result["processed_audio"] = {"audio_data": base64.b64encode(pcm).decode(), "sample_rate": 16000}
    result["recording_id"] = result.pop("id")
    return result


def _processing_value(result: object, name: str, default=None):
    """Read a processor result provided as a mapping or a small result object."""
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _read_recording_audio(path: Path) -> AudioInput:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("Unsupported audio format")
        return AudioInput(
            audio_data=base64.b64encode(handle.readframes(handle.getnframes())).decode(),
            sample_rate=handle.getframerate(),
        )


def _recording_sample_rate(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("Unsupported audio format")
        return int(handle.getframerate())


def _iter_recording_pcm(path: Path, frames_per_chunk: int = 320):
    """Yield bounded PCM blocks so persisted WAV processing stays stateful."""
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("Unsupported audio format")
        while chunk := handle.readframes(frames_per_chunk):
            yield chunk


def _merge_processing_timings(
    existing: dict | None,
    processing: dict | None,
) -> dict:
    """Keep the original pipeline time and add optional audio processing."""
    merged = dict(existing or {})
    processing_timings = dict(processing or {})
    processing_ms = processing_timings.pop(
        "audio_processing_ms",
        processing_timings.pop("total_ms", None),
    )
    merged.update(processing_timings)
    if processing_ms is None:
        return merged

    previous_processing_ms = merged.get("audio_processing_ms")
    baseline_ms = merged.get("baseline_total_ms")
    if baseline_ms is None:
        previous_total_ms = merged.get("total_ms")
        if previous_total_ms is not None and previous_processing_ms is not None:
            baseline_ms = max(
                0.0,
                float(previous_total_ms) - float(previous_processing_ms),
            )
        elif previous_total_ms is not None:
            baseline_ms = float(previous_total_ms)
        else:
            baseline_ms = 0.0
        # Older 2.1.0 records may already have had total_ms overwritten by a
        # manual job. STT time is a safe lower bound for the original pipeline.
        if merged.get("stt_ms") is not None:
            baseline_ms = max(float(baseline_ms), float(merged["stt_ms"]))

    merged["baseline_total_ms"] = round(float(baseline_ms), 2)
    merged["audio_processing_ms"] = round(float(processing_ms), 2)
    merged["total_ms"] = round(float(baseline_ms) + float(processing_ms), 2)
    return merged


async def _run_target_processing(
    recording_id: str,
    _speaker_id: str | None = None,
    backend: str = "df2_batch",
) -> None:
    """Run optional denoising and persist a successful variant."""
    try:
        await asyncio.to_thread(
            recognizer.catalog.update_recording, recording_id,
            processing_status="running", processing_backend=backend,
            processing_stages={"queue": "running"},
        )
        path = await asyncio.to_thread(recognizer.catalog.audio_path, recording_id, "original")
        if not path:
            raise ValueError("Recording not found")
        audio = await asyncio.to_thread(_read_recording_audio, path)
        requested_backend = backend
        requested_fallback = None
        processing_started = time.perf_counter()
        if backend == "df3_streaming":
            result = await asyncio.to_thread(
                recognizer.denoise_audio_stream,
                _iter_recording_pcm(path),
                _recording_sample_rate(path),
                timeout_seconds=180,
            )
            requested_fallback = _processing_value(
                result, "fallback_reason"
            )
            if not _processing_value(result, "denoised_pcm"):
                result = await asyncio.to_thread(
                    recognizer.denoise_audio,
                    audio,
                    timeout_seconds=180,
                    priority="analysis",
                )
                backend = "df2_batch"
                quality = dict(_processing_value(result, "quality", {}))
                quality.update(
                    {
                        "requested_backend": requested_backend,
                        "fallback_backend": backend,
                        "df3_fallback_reason": requested_fallback,
                    }
                )
                if isinstance(result, dict):
                    result["quality"] = quality
                else:
                    result = replace(result, quality=quality)
        else:
            result = await asyncio.to_thread(
                recognizer.denoise_audio,
                audio,
                timeout_seconds=180,
                priority="analysis",
            )
        sample_rate = int(_processing_value(result, "sample_rate", 16000))
        denoised = _processing_value(result, "denoised_pcm")
        if denoised:
            await asyncio.to_thread(recognizer.catalog.save_audio_variant, recording_id, "denoised", denoised, sample_rate)
        fallback_reason = _processing_value(result, "fallback_reason")
        if requested_backend != backend and requested_fallback:
            fallback_reason = f"df3_to_df2:{requested_fallback}"
        processing_timings = dict(
            _processing_value(result, "timings", {})
        )
        if backend == "df3_streaming":
            processing_timings["audio_processing_ms"] = round(
                (time.perf_counter() - processing_started) * 1000, 2
            )
        variant = "denoised" if denoised else "original"
        current = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id) or {}
        await asyncio.to_thread(
            recognizer.catalog.update_recording,
            recording_id,
            processing_status="complete" if denoised else "failed",
            processing_backend=backend,
            processing_stages=_processing_value(result, "stages", {}),
            processing_quality=_processing_value(result, "quality", {}),
            processing_fallback_reason=fallback_reason,
            processing_timings=processing_timings,
            labels={
                **(current.get("labels") or {}),
                "audio_variant": variant,
                "fallback": (
                    variant != "denoised" or requested_backend != backend
                ),
                "fallback_reason": fallback_reason,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # processing must never make the recording disappear
        _LOGGER.warning("Denoise processing failed for %s: %s", recording_id, error)
        current = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id)
        if current:
            labels = dict(current.get("labels") or {})
            labels.update({"audio_variant": "original", "fallback": True})
            await asyncio.to_thread(
                recognizer.catalog.update_recording, recording_id,
                processing_status="failed", processing_stages={"error": str(error)},
                processing_fallback_reason=str(error), labels=labels,
            )
    finally:
        current = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id)
        policy = str((current or {}).get("labels", {}).get("retention_policy", _policy.get("analysis_audio_retention", "all")))
        if policy != "all":
            if current and (policy == "none" or (
                current.get("outcome") == "matched"
                and current.get("processing_status") == "complete"
            )):
                await asyncio.to_thread(recognizer.catalog.remove_analysis_audio, recording_id)
        processing_tasks.pop(recording_id, None)


@app.post("/api/analyze-stream", dependencies=[Depends(authorize_api)])
async def analyze_stream(
    request: Request,
    sample_rate: int = Header(alias="X-Audio-Sample-Rate"),
    source_entity_id: str = Header(alias="X-STT-Entity-ID"),
    satellite_id: str | None = Header(default=None, alias="X-Satellite-ID"),
) -> dict:
    """Process PCM during upload, then persist/recognize the drained utterance."""
    if _policy["audio_processing_backend"] != "df3_streaming":
        raise HTTPException(
            status_code=409,
            detail="Stateful DF3 is not configured",
        )
    if sample_rate < 8_000 or sample_rate > 48_000:
        raise HTTPException(status_code=400, detail="Unsupported sample rate")

    sentinel = object()
    # Cap producer memory while a slow model drains a long utterance. Keep
    # individual queue items small even when the HTTP server emits a large part.
    chunks: queue.Queue[bytes | object] = queue.Queue(maxsize=16)
    upload_deadline = time.perf_counter() + settings.max_audio_seconds + STREAM_FINALIZE_TIMEOUT_SECONDS

    async def enqueue(item: bytes | object, *, deadline: float | None = None) -> bool:
        until = min(upload_deadline, deadline) if deadline is not None else upload_deadline
        while time.perf_counter() < until and not processor_task.done():
            try:
                chunks.put_nowait(item)
                return True
            except queue.Full:
                await asyncio.sleep(0.005)
        return False

    def incoming():
        while True:
            item = chunks.get()
            if item is sentinel:
                return
            yield bytes(item)

    processor_task = asyncio.create_task(
        asyncio.to_thread(
            recognizer.denoise_audio_stream,
            incoming(),
            sample_rate,
            timeout_seconds=STREAM_FINALIZE_TIMEOUT_SECONDS,
        ),
        name="speaker-recognition-df3-stream",
    )
    original = bytearray()
    pending = b""
    body_error: HTTPException | None = None
    max_audio_bytes = min(
        MAX_REQUEST_BYTES,
        settings.max_audio_seconds * sample_rate * 2,
    )
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            combined = pending + chunk
            complete = len(combined) - (len(combined) % 2)
            pending = combined[complete:]
            pcm = combined[:complete]
            if pcm:
                original.extend(pcm)
                if len(original) > max_audio_bytes:
                    body_error = HTTPException(
                        status_code=413,
                        detail="Streaming audio exceeds the configured limit",
                    )
                    break
                for offset in range(0, len(pcm), 32 * 1024):
                    if not await enqueue(pcm[offset:offset + 32 * 1024]):
                        body_error = HTTPException(
                            status_code=503, detail="Streaming processor could not keep up",
                        )
                        break
                if body_error is not None:
                    break
        if pending and body_error is None:
            body_error = HTTPException(
                status_code=400,
                detail="Streaming audio ended with an incomplete PCM16 sample",
            )
    finally:
        if not processor_task.done():
            sent = await enqueue(sentinel, deadline=time.perf_counter() + 1.0)
            if not sent:
                # A wedged consumer must not leave an unbounded producer wait.
                try:
                    chunks.get_nowait()
                    chunks.put_nowait(sentinel)
                except queue.Empty:
                    pass
                body_error = body_error or HTTPException(
                    status_code=503, detail="Streaming processor stalled",
                )

    try:
        processed = await asyncio.wait_for(
            processor_task, timeout=STREAM_FINALIZE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as error:
        raise HTTPException(status_code=503, detail="Streaming processor timed out") from error
    if body_error is not None:
        raise body_error
    if not original:
        raise HTTPException(status_code=400, detail="Streaming audio is empty")

    audio_input = AudioInput(
        audio_data=base64.b64encode(original).decode(),
        sample_rate=sample_rate,
    )
    requested_fallback = processed.fallback_reason
    if processed.denoised_pcm is None:
        # The resident DF2/PyTorch path remains the rollback for any startup,
        # stream, drain or quality failure in the explicitly selected route.
        processed = await asyncio.to_thread(
            recognizer.denoise_audio,
            audio_input,
            timeout_seconds=STREAM_FINALIZE_TIMEOUT_SECONDS,
        )
        processed.quality["requested_backend"] = "df3_streaming"
        processed.quality["fallback_backend"] = "df2_batch"
        if requested_fallback:
            processed.quality["df3_fallback_reason"] = requested_fallback

    # Reuse the established persistence and original-audio recognition path,
    # but do not run its batch denoiser a second time.
    payload = await analyze(
        AnalyzeRequest(
            audio=audio_input,
            source="pipeline",
            satellite_id=satellite_id,
            stt_entity_id=source_entity_id,
            extraction_mode="off",
        )
    )
    recording_id = str(payload["recording_id"])
    detailed = None
    if (
        processed.denoised_pcm
        and payload.get("outcome") not in {"matched", "multiple_speakers"}
    ):
        calibration = recognizer.catalog.calibration()
        threshold = (
            float(calibration["threshold"])
            if calibration
            else settings.recognition_threshold
        )
        margin = (
            float(calibration["margin"])
            if calibration
            else float(_policy["min_margin"])
        )
        denoised_input = AudioInput(
            audio_data=base64.b64encode(processed.denoised_pcm).decode(),
            sample_rate=processed.sample_rate,
        )
        detailed, stream_revision = await asyncio.to_thread(
            recognizer.recognize_detailed_with_snapshot,
            denoised_input,
            threshold=threshold,
            min_margin=margin,
        )
        if detailed.outcome in {"matched", "multiple_speakers"}:
            labels = dict(
                (
                    await asyncio.to_thread(
                        recognizer.catalog.get_recording, recording_id
                    )
                    or {}
                ).get("labels")
                or {}
            )
            labels["detected_speakers"] = detailed.detected_speakers
            await asyncio.to_thread(
                recognizer.catalog.update_recording,
                recording_id,
                outcome=detailed.outcome,
                speaker_id=detailed.speaker.id if detailed.speaker else None,
                speaker_name=detailed.speaker.name if detailed.speaker else None,
                confidence=detailed.confidence,
                threshold=detailed.threshold,
                margin=detailed.margin,
                scores=detailed.scores,
                segments=detailed.candidates,
                profile_revision=stream_revision,
                labels=labels,
            )
            await asyncio.to_thread(
                recognizer.catalog.record_recognition_run,
                recording_id, stream_revision,
                {
                    "outcome": detailed.outcome,
                    "speaker_id": detailed.speaker.id if detailed.speaker else None,
                    "confidence": detailed.confidence,
                    "scores": detailed.scores,
                    "threshold": detailed.threshold,
                    "margin": detailed.margin,
                    "settings": {"recognition_audio": "denoised", "requested_backend": "df3_streaming"},
                },
            )

    if processed.denoised_pcm:
        await asyncio.to_thread(
            recognizer.catalog.save_audio_variant,
            recording_id,
            "denoised",
            processed.denoised_pcm,
            processed.sample_rate,
        )
    current = (
        await asyncio.to_thread(
            recognizer.catalog.get_recording, recording_id
        )
        or {}
    )
    variant = "denoised" if processed.denoised_pcm else "original"
    labels = dict(current.get("labels") or {})
    labels.update(
        {
            "audio_variant": variant,
            "fallback": requested_fallback is not None or variant == "original",
            "fallback_reason": requested_fallback,
            "quality": processed.quality,
        }
    )
    recording = await asyncio.to_thread(
        recognizer.catalog.update_recording,
        recording_id,
        extraction_mode="before_stt",
        extraction_status="ready" if variant == "denoised" else "failed",
        processing_status="complete",
        processing_backend=(
            "df2_batch"
            if processed.quality.get("fallback_backend") == "df2_batch"
            else "df3_streaming"
        ),
        processing_speaker_id=(
            detailed.speaker.id
            if detailed is not None and detailed.speaker is not None
            else current.get("speaker_id")
        ),
        processing_stages=processed.stages,
        processing_quality=processed.quality,
        processing_fallback_reason=requested_fallback,
        processing_timings={
            **processed.timings,
            **(
                {
                    "audio_processing_ms": processed.timings.get(
                        "post_utterance_ms", 0.0
                    )
                }
                if processed.quality.get("fallback_backend") != "df2_batch"
                else {}
            ),
        },
        labels=labels,
    )
    if (
        _policy["analysis_audio_retention"] == "errors"
        and (recording or current).get("outcome") == "matched"
    ):
        recording = await asyncio.to_thread(
            recognizer.catalog.remove_analysis_audio, recording_id
        ) or recording
    response = _analysis_payload(recording or current, detailed)
    if processed.denoised_pcm:
        response["denoised_audio"] = {
            "audio_data": base64.b64encode(processed.denoised_pcm).decode(),
            "sample_rate": processed.sample_rate,
        }
    return response


@app.post("/api/analyze", dependencies=[Depends(authorize_api)])
async def analyze(request: AnalyzeRequest) -> dict:
    """Persist then inspect a pipeline/test clip; generic /recognize remains ephemeral."""
    try:
        raw = recognizer._decode_pcm_bytes(request.audio)
        mode = request.extraction_mode or str(_policy["extraction_mode"])
        audio_policy = str(_policy["analysis_audio_retention"])
        quality_report = await asyncio.to_thread(
            assess_pcm16_mono, raw, request.audio.sample_rate, purpose="analysis",
        )
        recording = await asyncio.to_thread(
            recognizer.catalog.create_recording, raw, request.audio.sample_rate,
            retain_audio=audio_policy == "all" or mode == "compare",
            source=request.source, satellite_id=request.satellite_id, stt_entity_id=request.stt_entity_id,
            extraction_mode=mode,
            labels={
                "audio_quality": quality_report,
                "retention_pending": mode == "compare" and audio_policy != "all",
                "retention_policy": audio_policy,
            },
        )
        calibration = recognizer.catalog.calibration()
        threshold = float(calibration["threshold"]) if calibration else settings.recognition_threshold
        margin = float(calibration["margin"]) if calibration else float(_policy["min_margin"])
        try:
            live_deadline = (
                time.perf_counter() + 11.5 if mode == "before_stt" else None
            )
            recognition_call = asyncio.to_thread(
                recognizer.recognize_detailed_with_snapshot,
                request.audio,
                threshold=threshold,
                min_margin=margin,
            )
            if live_deadline is not None:
                try:
                    detailed, revision_snapshot = await asyncio.wait_for(
                        recognition_call,
                        timeout=max(0.1, live_deadline - time.perf_counter()),
                    )
                except asyncio.TimeoutError:
                    if audio_policy == "errors":
                        recording = await asyncio.to_thread(
                            recognizer.catalog.save_original_audio,
                            recording["id"], raw, request.audio.sample_rate,
                        ) or recording
                    labels = {
                        **(recording.get("labels") or {}),
                        "audio_variant": "original",
                        "fallback": True,
                        "fallback_reason": "live_budget_exhausted",
                    }
                    recording = await asyncio.to_thread(
                        recognizer.catalog.update_recording,
                        recording["id"],
                        outcome="error",
                        processing_status="failed",
                        processing_stages={
                            "recognition": "timeout",
                            "denoise": "skipped_deadline",
                        },
                        processing_fallback_reason="live_budget_exhausted",
                        labels=labels,
                    ) or recording
                    return _analysis_payload(recording)
            else:
                detailed, revision_snapshot = await recognition_call
            processed = None
            if mode == "before_stt":
                remaining = live_deadline - time.perf_counter()
                if remaining > 0.1:
                    processed = await asyncio.to_thread(
                        recognizer.denoise_audio,
                        request.audio,
                        timeout_seconds=remaining,
                    )
                if (
                    detailed.outcome not in {"matched", "multiple_speakers"}
                    and processed is not None
                    and processed.denoised_pcm
                ):
                    denoised_input = AudioInput(
                        audio_data=base64.b64encode(processed.denoised_pcm).decode(),
                        sample_rate=processed.sample_rate,
                    )
                    remaining = live_deadline - time.perf_counter()
                    denoised_result = None
                    if remaining > 0.1:
                        try:
                            denoised_result = await asyncio.wait_for(
                                asyncio.to_thread(
                                    recognizer.recognize_detailed_with_snapshot,
                                    denoised_input,
                                    threshold=threshold,
                                    min_margin=margin,
                                ),
                                timeout=remaining,
                            )
                        except asyncio.TimeoutError:
                            denoised_result = None
                    # Enhancement may rescue an otherwise unknown recording.
                    if denoised_result and denoised_result[0].speaker is not None:
                        detailed, revision_snapshot = denoised_result
            outcome = detailed.outcome
            if (
                outcome not in {"matched", "multiple_speakers"}
                and _policy["unknown_speaker_policy"] == "block"
            ):
                outcome = "blocked"
            processing_problem = mode == "before_stt" and (
                processed is None or not processed.denoised_pcm
            )
            if audio_policy == "errors" and mode != "compare" and (
                outcome != "matched" or processing_problem
            ):
                recording = await asyncio.to_thread(
                    recognizer.catalog.save_original_audio,
                    recording["id"], raw, request.audio.sample_rate,
                ) or recording
            labels = dict(recording.get("labels") or {})
            labels["detected_speakers"] = detailed.detected_speakers
            updates = {
                "outcome": outcome, "speaker_id": detailed.speaker.id if detailed.speaker else None,
                "speaker_name": detailed.speaker.name if detailed.speaker else None, "confidence": detailed.confidence,
                "threshold": detailed.threshold, "margin": detailed.margin, "scores": detailed.scores,
                "segments": detailed.candidates, "timings": detailed.timings, "labels": labels,
                "profile_revision": revision_snapshot,
                "extraction_status": "disabled" if mode == "off" else "processing" if processed else "queued" if mode == "compare" else "not_processed",
            }
            if processed is not None:
                updates.update({
                    "processing_status": "complete" if processed.denoised_pcm else "failed",
                    "processing_backend": "df2_batch",
                    "processing_speaker_id": detailed.speaker.id if detailed.speaker else None,
                    "processing_stages": processed.stages,
                    "processing_quality": processed.quality,
                    "processing_fallback_reason": processed.fallback_reason,
                    "processing_timings": processed.timings,
                })
            elif mode == "before_stt":
                updates.update(
                    {
                        "processing_status": "failed",
                        "processing_stages": {
                            "denoise": "skipped_deadline",
                        },
                        "processing_fallback_reason": "live_budget_exhausted",
                    }
                )
            recording = await asyncio.to_thread(recognizer.catalog.update_recording, recording["id"], **updates) or recording
            await asyncio.to_thread(
                recognizer.catalog.record_recognition_run,
                recording["id"], revision_snapshot,
                {
                    "outcome": outcome,
                    "speaker_id": detailed.speaker.id if detailed.speaker else None,
                    "confidence": detailed.confidence,
                    "scores": detailed.scores,
                    "threshold": detailed.threshold,
                    "margin": detailed.margin,
                    "settings": {
                        "unknown_speaker_policy": _policy["unknown_speaker_policy"],
                        "extraction_mode": mode,
                        "audio_processing_backend": _policy["audio_processing_backend"],
                    },
                },
            )
            if processed is not None:
                if processed.denoised_pcm and audio_policy == "all":
                    recording = await asyncio.to_thread(
                        recognizer.catalog.save_audio_variant,
                        recording["id"], "denoised", processed.denoised_pcm,
                        processed.sample_rate,
                    ) or recording
                variant = "denoised" if processed.denoised_pcm else "original"
                labels = dict(recording.get("labels") or {})
                labels.update({
                    "audio_variant": variant if audio_policy == "all" else "original",
                    "fallback": variant != "denoised",
                    "fallback_reason": processed.fallback_reason,
                    "quality": processed.quality,
                })
                recording = await asyncio.to_thread(
                    recognizer.catalog.update_recording,
                    recording["id"],
                    labels=labels,
                    extraction_status="ready" if variant != "original" else "failed",
                ) or recording
            elif mode == "compare":
                recording = await asyncio.to_thread(
                    recognizer.catalog.update_recording,
                    recording["id"],
                    processing_status="queued",
                    processing_backend=str(
                        _policy["audio_processing_backend"]
                    ),
                    processing_speaker_id=None,
                    processing_stages={"queue": "queued"},
                ) or recording
                task = asyncio.create_task(
                    _run_target_processing(
                        recording["id"],
                        backend=str(_policy["audio_processing_backend"]),
                    ),
                    name=f"speaker-recognition-compare-{recording['id']}",
                )
                processing_tasks[recording["id"]] = task
            payload = _analysis_payload(recording, detailed)
            if processed is not None:
                if processed.denoised_pcm:
                    payload["denoised_audio"] = {
                        "audio_data": base64.b64encode(processed.denoised_pcm).decode(),
                        "sample_rate": processed.sample_rate,
                    }
            return payload
        except (ValueError, RuntimeError) as error:
            if audio_policy == "errors" and mode != "compare":
                await asyncio.to_thread(
                    recognizer.catalog.save_original_audio,
                    recording["id"], raw, request.audio.sample_rate,
                )
            recording = await asyncio.to_thread(
                recognizer.catalog.update_recording, recording["id"],
                outcome="error", labels={**(recording.get("labels") or {}), "error": str(error)},
            ) or recording
            raise HTTPException(status_code=409, detail={"recording_id": recording["id"], "error": str(error)}) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/analyze-binary", dependencies=[Depends(authorize_api)])
async def analyze_binary(
    request: Request,
    x_sample_rate: int = Header(),
    x_channels: int = Header(),
    x_audio_format: str = Header(),
    source: str = "pipeline",
    satellite_id: str | None = None,
    stt_entity_id: str | None = None,
    extraction_mode: str | None = None,
) -> dict:
    """Versioned binary transport; the existing analysis response is unchanged."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/octet-stream":
        raise HTTPException(status_code=415, detail="Expected application/octet-stream")
    if x_channels != 1 or x_audio_format != "pcm_s16le":
        raise HTTPException(status_code=415, detail="Expected mono pcm_s16le audio")
    pcm = await request.body()
    if not pcm or len(pcm) % 2 or len(pcm) > x_sample_rate * settings.max_audio_seconds * 2:
        raise HTTPException(status_code=400, detail="Invalid PCM length")
    try:
        payload = AnalyzeRequest(
            audio=AudioInput(audio_data=base64.b64encode(pcm).decode("ascii"), sample_rate=x_sample_rate),
            source=source,
            satellite_id=satellite_id,
            stt_entity_id=stt_entity_id,
            extraction_mode=extraction_mode,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await analyze(payload)


@app.post("/api/recordings/{recording_id}/finalize", dependencies=[Depends(authorize_api)])
async def finalize_recording(recording_id: str, request: FinalizeRecordingRequest) -> dict:
    current = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id)
    if not current: raise HTTPException(status_code=404, detail="Recording not found")
    payload = request.model_dump(exclude_none=True)
    labels = dict(current.get("labels") or {})
    for key in ("audio_variant", "fallback", "fallback_reason", "quality"):
        if key in payload: labels[key] = payload.pop(key)
    if "timings" in payload:
        payload["timings"] = {**(current.get("timings") or {}), **payload["timings"]}
    if labels: payload["labels"] = labels
    recording = await asyncio.to_thread(recognizer.catalog.update_recording, recording_id, **payload)
    if not recording: raise HTTPException(status_code=404, detail="Recording not found")
    return _analysis_payload(recording)


@app.post("/api/recordings/{recording_id}/conversation", dependencies=[Depends(authorize_api)])
async def finalize_conversation(recording_id: str, request: ConversationRecordingRequest) -> dict:
    current = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id)
    if not current: raise HTTPException(status_code=404, detail="Recording not found")
    labels = dict(current.get("labels") or {})
    labels.update({
        "person_entity_id": request.person_entity_id,
        "person_entity_ids": request.person_entity_ids,
        "speaker_names": request.speaker_names,
        "conversation_reason": request.conversation_reason,
    })
    recording = await asyncio.to_thread(recognizer.catalog.update_recording, recording_id, conversation_forwarded=request.conversation_forwarded, timings=request.timings or current.get("timings", {}), labels=labels)
    return _analysis_payload(recording or current)


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
        if request.start_mode == "remote":
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
    elif result.status == "complete":
        try:
            await asyncio.to_thread(
                home_assistant.confirm_enrollment_sample, satellite_entity_id
            )
        except HomeAssistantApiError:
            # The audio is already safely captured. A confirmation failure must
            # not discard it, but is useful when diagnosing satellite firmware.
            _LOGGER.warning("Could not reset Voice satellite after enrollment", exc_info=True)


@app.post(
    "/api/satellite-enrollment/claim",
    response_model=SatelliteEnrollmentClaim,
    dependencies=[Depends(authorize_api)],
)
async def claim_satellite_enrollment(
    request: SatelliteEnrollmentClaimRequest,
) -> SatelliteEnrollmentClaim:
    # SpeechMetadata does not carry its originating satellite. The integration
    # therefore snapshots Home Assistant's local state synchronously when the
    # STT stream starts and submits that identity here. Re-querying HA from the
    # App races the satellite's listening -> processing transition and can miss
    # the only claim opportunity.
    armed = await satellite_enrollment.peek_armed()
    if (
        armed is None
        or request.satellite_entity_id is None
        or request.satellite_entity_id != armed.satellite_entity_id
    ):
        return SatelliteEnrollmentClaim()
    return SatelliteEnrollmentClaim(session=await satellite_enrollment.claim())


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
async def delete_speaker(speaker_id: str, request: DeleteSpeakerRequest | None = None) -> Response:
    if not await asyncio.to_thread(recognizer.delete, speaker_id, not request or request.audio_action == "delete"):
        raise HTTPException(status_code=404, detail="Speaker not found")
    return Response(status_code=204)


@app.post("/api/recognize", response_model=RecognitionResult, dependencies=[Depends(authorize_api)])
async def recognize(request: RecognitionRequest) -> RecognitionResult:
    try:
        calibration = recognizer.catalog.calibration()
        threshold = (
            float(calibration["threshold"])
            if calibration
            else settings.recognition_threshold
        )
        margin = (
            float(calibration["margin"])
            if calibration
            else float(_policy["min_margin"])
        )
        detailed = await asyncio.to_thread(
            recognizer.recognize_detailed,
            request.audio,
            threshold=threshold,
            min_margin=margin,
        )
        return RecognitionResult(
            matched=detailed.speaker is not None,
            speaker=detailed.speaker,
            confidence=detailed.confidence,
            threshold=detailed.threshold,
            scores=detailed.scores,
            outcome=detailed.outcome,
            detected_speakers=detailed.detected_speakers,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/overview", dependencies=[Depends(authorize_api)])
async def overview() -> dict:
    return {
        "storage_used_bytes": await asyncio.to_thread(recognizer.catalog.storage_usage),
        "storage_limit_bytes": recognizer.catalog.max_storage_bytes,
        "retention_days": recognizer.catalog.retention_days,
        "profiles": [item.model_dump(mode="json") for item in recognizer.list_speakers()],
        "calibration": recognizer.catalog.calibration(),
        "storage_breakdown": await asyncio.to_thread(recognizer.catalog.storage_breakdown),
    }


@app.get("/api/storage", dependencies=[Depends(authorize_api)])
async def storage_details() -> dict:
    return {
        "categories": await asyncio.to_thread(recognizer.catalog.storage_breakdown),
        "orphans": await asyncio.to_thread(recognizer.catalog.scan_orphans),
    }


@app.post("/api/storage/orphans/delete", dependencies=[Depends(authorize_api)])
async def delete_orphan_audio(request: DeleteUnindexedAudioRequest) -> dict:
    try:
        removed = await asyncio.to_thread(
            recognizer.catalog.delete_unindexed_wav_files, request.paths,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"deleted": removed}


@app.get("/api/diagnostics", dependencies=[Depends(authorize_api)])
async def privacy_diagnostics() -> dict:
    """A default export without tokens, audio, transcripts, or names."""
    info = await api_info()
    return {
        "schema_version": 1,
        "api_info": info,
        "engine_ready": recognizer.ready,
        "storage": await asyncio.to_thread(recognizer.catalog.storage_breakdown),
        "policy": dict(_policy),
    }


@app.get("/api/archived-samples", dependencies=[Depends(authorize_api)])
async def archived_samples() -> dict:
    items = await asyncio.to_thread(recognizer.catalog.list_archived_samples)
    return {"items": [{key: value for key, value in item.items() if key != "path"} for item in items]}


@app.delete("/api/archived-samples/{sample_id}", status_code=204, dependencies=[Depends(authorize_api)])
async def delete_archived_sample(sample_id: str) -> Response:
    try:
        deleted = await asyncio.to_thread(recognizer.catalog.delete_archived_sample, sample_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="Archived sample not found")
    return Response(status_code=204)


class ReviewUpdateRequest(BaseModel):
    status: Literal["pending", "resolved", "ignored"]
    truth_speaker_id: str | None = Field(default=None, min_length=1, max_length=64)
    truth_unknown: bool = False


class ExperimentPreviewRequest(BaseModel):
    threshold: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=2)


@app.get("/api/review-inbox", dependencies=[Depends(authorize_api)])
async def review_inbox(page: int = 1, page_size: int = 50, review_status: str | None = "pending") -> dict:
    try:
        return await asyncio.to_thread(
            list_review_inbox, recognizer.catalog,
            page=page, page_size=page_size, status=review_status,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.patch("/api/analysis/{recording_id}/review", dependencies=[Depends(authorize_api)])
async def review_recording(recording_id: str, request: ReviewUpdateRequest) -> dict:
    if request.status == "resolved" and not (request.truth_unknown or request.truth_speaker_id):
        raise HTTPException(status_code=400, detail={"code": "truth_required", "message": "Choose a speaker or unknown"})
    if request.truth_speaker_id and request.truth_speaker_id not in {item.id for item in recognizer.list_speakers()}:
        raise HTTPException(status_code=404, detail={"code": "speaker_not_found", "message": "Speaker profile not found"})
    try:
        updated = await asyncio.to_thread(
            set_review, recognizer.catalog, recording_id, request.status,
            request.truth_speaker_id, request.truth_unknown,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Recording not found") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _analysis_payload(updated)


@app.get("/api/devices/quality", dependencies=[Depends(authorize_api)])
async def device_quality_report(days: int = 30) -> dict:
    try:
        return await asyncio.to_thread(device_quality, recognizer.catalog, days)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


async def _run_experiment(job_id: str, threshold: float, margin: float) -> None:
    experiment_results[job_id] = {"id": job_id, "status": "running"}
    try:
        result = await asyncio.to_thread(
            preview_experiment, recognizer.catalog, recognizer, threshold, margin,
        )
        experiment_results[job_id] = {"id": job_id, "status": "complete", **result}
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _LOGGER.warning("Recognition experiment failed: %s", error)
        experiment_results[job_id] = {"id": job_id, "status": "failed", "reason": str(error)}
    finally:
        experiment_tasks.pop(job_id, None)


@app.post("/api/experiments/preview", status_code=202, dependencies=[Depends(authorize_api)])
async def start_experiment(request: ExperimentPreviewRequest) -> dict:
    if any(not task.done() for task in experiment_tasks.values()):
        raise HTTPException(status_code=429, detail={"code": "experiment_busy", "message": "Another experiment is still running"})
    job_id = uuid.uuid4().hex
    experiment_results.clear()
    experiment_results[job_id] = {"id": job_id, "status": "queued"}
    experiment_tasks[job_id] = asyncio.create_task(
        _run_experiment(job_id, request.threshold, request.margin),
        name=f"speaker-experiment-{job_id}",
    )
    return experiment_results[job_id]


@app.get("/api/experiments/{job_id}", dependencies=[Depends(authorize_api)])
async def get_experiment(job_id: str) -> dict:
    if job_id not in experiment_results:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment_results[job_id]


@app.get("/api/recordings", dependencies=[Depends(authorize_api)])
@app.get("/api/analysis", dependencies=[Depends(authorize_api)])
async def list_recordings(page: int = 1, page_size: int = 50, offset: int | None = None, limit: int | None = None, outcome: str | None = None, source: str | None = None, speaker_id: str | None = None, q: str | None = None, since: str | None = None) -> dict:
    if offset is not None:
        page_size = limit or page_size; page = offset // max(1, page_size) + 1
    items, total = await asyncio.to_thread(recognizer.catalog.list_recordings, page=page, page_size=page_size, outcome=outcome, source=source, speaker_id=speaker_id, query=q, since=since)
    return {"items": [_analysis_payload(item) for item in items], "total": total, "page": page, "page_size": page_size, "offset": (page-1)*page_size, "limit": page_size}


@app.get("/api/analysis/available-for-enrollment", dependencies=[Depends(authorize_api)])
async def recordings_available_for_enrollment(q: str = "", page: int = 1) -> dict:
    """Search retained originals for the new-profile picker."""
    if page < 1 or len(q) > 100:
        raise HTTPException(status_code=400, detail="Invalid recording search")

    def query() -> tuple[list[dict], bool]:
        pattern = f"%{q.strip()}%"
        with recognizer.catalog._lock, recognizer.catalog._connect() as db:
            rows = db.execute(
                "SELECT * FROM recordings WHERE audio_retained=1 AND original_path<>'' "
                "AND (COALESCE(transcript,'') LIKE ? OR COALESCE(satellite_id,'') LIKE ? "
                "OR COALESCE(speaker_name,'') LIKE ?) "
                "ORDER BY created_at DESC LIMIT 51 OFFSET ?",
                (pattern, pattern, pattern, (page - 1) * 50),
            ).fetchall()
        items = []
        for row in rows[:50]:
            item = recognizer.catalog._row(row)
            if recognizer.catalog.audio_path(item["id"], "original"):
                items.append(item)
        return items, len(rows) > 50

    items, has_more = await asyncio.to_thread(query)
    return {"items": [_analysis_payload(item) for item in items], "page": page, "has_more": has_more}


@app.get("/api/recordings/{recording_id}", dependencies=[Depends(authorize_api)])
@app.get("/api/analysis/{recording_id}", dependencies=[Depends(authorize_api)])
async def get_recording(recording_id: str) -> dict:
    recording = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id)
    if not recording: raise HTTPException(status_code=404, detail="Recording not found")
    return _analysis_payload(recording)


@app.post(
    "/api/recordings/{recording_id}/reanalyze",
    dependencies=[Depends(authorize_api)],
)
@app.post(
    "/api/analysis/{recording_id}/reanalyze",
    dependencies=[Depends(authorize_api)],
)
async def reanalyze_recording(recording_id: str) -> dict:
    """Re-run speaker recognition against the current enrolled profiles."""
    recording = await asyncio.to_thread(
        recognizer.catalog.get_recording, recording_id
    )
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    path = await asyncio.to_thread(
        recognizer.catalog.audio_path, recording_id, "original"
    )
    if not path:
        raise HTTPException(status_code=404, detail="Original audio not found")

    calibration = recognizer.catalog.calibration()
    threshold = (
        float(calibration["threshold"])
        if calibration
        else settings.recognition_threshold
    )
    margin = (
        float(calibration["margin"])
        if calibration
        else float(_policy["min_margin"])
    )
    try:
        audio = await asyncio.to_thread(_read_recording_audio, path)
        detailed, revision_snapshot = await asyncio.to_thread(
            recognizer.recognize_detailed_with_snapshot,
            audio,
            threshold=threshold,
            min_margin=margin,
        )
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    outcome = detailed.outcome
    if (
        outcome not in {"matched", "multiple_speakers"}
        and _policy["unknown_speaker_policy"] == "block"
    ):
        outcome = "blocked"
    timings = {
        **(recording.get("timings") or {}),
        **detailed.timings,
    }
    labels = dict(recording.get("labels") or {})
    labels["detected_speakers"] = detailed.detected_speakers
    updated = await asyncio.to_thread(
        recognizer.catalog.update_recording,
        recording_id,
        outcome=outcome,
        speaker_id=detailed.speaker.id if detailed.speaker else None,
        speaker_name=detailed.speaker.name if detailed.speaker else None,
        confidence=detailed.confidence,
        threshold=detailed.threshold,
        margin=detailed.margin,
        scores=detailed.scores,
        segments=detailed.candidates,
        timings=timings,
        labels=labels,
        profile_revision=revision_snapshot,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Recording not found")
    await asyncio.to_thread(
        recognizer.catalog.record_recognition_run,
        recording_id, revision_snapshot,
        {
            "outcome": outcome,
            "speaker_id": detailed.speaker.id if detailed.speaker else None,
            "confidence": detailed.confidence,
            "scores": detailed.scores,
            "threshold": detailed.threshold,
            "margin": detailed.margin,
            "settings": {
                "unknown_speaker_policy": _policy["unknown_speaker_policy"],
                "extraction_mode": recording.get("extraction_mode"),
                "audio_processing_backend": _policy["audio_processing_backend"],
            },
        },
    )
    return _analysis_payload(updated, detailed)


@app.get("/api/analysis/{recording_id}/runs", dependencies=[Depends(authorize_api)])
async def analysis_runs(recording_id: str) -> dict:
    if not await asyncio.to_thread(recognizer.catalog.get_recording, recording_id):
        raise HTTPException(status_code=404, detail="Recording not found")
    return {"items": await asyncio.to_thread(recognizer.catalog.list_recognition_runs, recording_id)}


def _public_timeline(result: dict) -> dict:
    profiles = {item.id: item.name for item in recognizer.list_speakers()}
    public = dict(result)
    public_segments = []
    for stored in result.get("segments", []):
        segment = dict(stored)
        speaker_id = segment.get("speaker_id")
        if speaker_id in profiles:
            segment["speaker_name"] = profiles[speaker_id]
        else:
            segment.update(speaker_id=None, speaker_name=None, status="unknown")
        public_segments.append(segment)
    public["segments"] = public_segments
    return public


@app.get("/api/analysis/{recording_id}/diarization", dependencies=[Depends(authorize_api)])
async def get_diarization(recording_id: str) -> dict:
    if not await asyncio.to_thread(recognizer.catalog.get_recording, recording_id):
        raise HTTPException(status_code=404, detail="Recording not found")
    result = await asyncio.to_thread(timeline_store.get, recording_id)
    return _public_timeline(result) if result else {
        "recording_id": recording_id, "status": "idle", "experimental": True,
        "segments": [],
    }


async def _run_diarization(recording_id: str, path: Path) -> None:
    try:
        async with timeline_slots:
            await asyncio.to_thread(timeline_store.set, recording_id, "running", {"experimental": True})
            calibration = recognizer.catalog.calibration()
            threshold = float(calibration["threshold"]) if calibration else settings.recognition_threshold
            margin = float(calibration["margin"]) if calibration else float(_policy["min_margin"])
            audio = await asyncio.to_thread(_read_recording_audio, path)
            result = await asyncio.to_thread(analyze_timeline, recognizer, audio, threshold, margin)
            await asyncio.to_thread(timeline_store.set, recording_id, "complete", result)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _LOGGER.warning("Offline speaker timeline failed for %s: %s", recording_id, error)
        try:
            await asyncio.to_thread(timeline_store.set, recording_id, "failed", {
                "experimental": True, "reason": str(error), "segments": [],
            })
        except KeyError:
            pass
    finally:
        timeline_tasks.pop(recording_id, None)


@app.post("/api/analysis/{recording_id}/diarization", status_code=202, dependencies=[Depends(authorize_api)])
async def start_diarization(recording_id: str) -> dict:
    if not await asyncio.to_thread(recognizer.catalog.get_recording, recording_id):
        raise HTTPException(status_code=404, detail="Recording not found")
    path = await asyncio.to_thread(recognizer.catalog.audio_path, recording_id, "original")
    if not path:
        raise HTTPException(status_code=409, detail={"code": "audio_not_retained", "message": "Original audio is no longer available"})
    existing = timeline_tasks.get(recording_id)
    if existing and not existing.done():
        return await get_diarization(recording_id)
    if len(timeline_tasks) >= 2:
        raise HTTPException(status_code=429, detail={"code": "timeline_queue_full", "message": "Try again after another timeline finishes"})
    result = await asyncio.to_thread(timeline_store.set, recording_id, "queued", {"experimental": True, "segments": []})
    timeline_tasks[recording_id] = asyncio.create_task(
        _run_diarization(recording_id, path), name=f"speaker-timeline-{recording_id}",
    )
    return result


@app.get("/api/recordings/{recording_id}/audio", dependencies=[Depends(authorize_api)])
@app.get("/api/analysis/{recording_id}/audio", dependencies=[Depends(authorize_api)])
async def recording_audio(recording_id: str, variant: str = "original") -> FileResponse:
    path = await asyncio.to_thread(recognizer.catalog.audio_path, recording_id, variant)
    if not path: raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/wav", filename=f"{recording_id}-{variant}.wav")


@app.post(
    "/api/recordings/{recording_id}/process",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(authorize_api)],
)
@app.post(
    "/api/analysis/{recording_id}/process",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(authorize_api)],
)
async def process_target_audio(recording_id: str, request: ProcessTargetAudioRequest) -> dict:
    """Queue optional DF2 batch or true stateful DF3 denoising."""
    recording = await asyncio.to_thread(recognizer.catalog.get_recording, recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    existing = processing_tasks.get(recording_id)
    if existing and not existing.done():
        return _analysis_payload(recording)
    if recording.get("denoised_path"):
        raise HTTPException(
            status_code=409,
            detail="Wis de bestaande ruisonderdrukking voordat je opnieuw verwerkt",
        )
    backend = request.backend or str(_policy["audio_processing_backend"])
    recording = await asyncio.to_thread(
        recognizer.catalog.update_recording,
        recording_id,
        processing_status="queued",
        processing_backend=backend,
        processing_speaker_id=None,
        processing_stages={"queue": "queued"},
        processing_quality={},
        processing_timings={},
        processing_fallback_reason=None,
    ) or recording
    task = asyncio.create_task(
        _run_target_processing(recording_id, backend=backend),
        name=f"speaker-recognition-process-{recording_id}",
    )
    processing_tasks[recording_id] = task
    return _analysis_payload(recording)


@app.delete(
    "/api/recordings/{recording_id}/processing",
    dependencies=[Depends(authorize_api)],
)
@app.delete(
    "/api/analysis/{recording_id}/processing",
    dependencies=[Depends(authorize_api)],
)
async def reset_target_processing(recording_id: str) -> dict:
    """Remove only reproducible denoise output and processor measurements."""
    existing = processing_tasks.get(recording_id)
    if existing and not existing.done():
        raise HTTPException(
            status_code=409,
            detail="Ruisonderdrukking is nog actief",
        )
    try:
        recording = await asyncio.to_thread(
            recognizer.catalog.reset_processing, recording_id
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return _analysis_payload(recording)


@app.post("/api/recordings/{recording_id}/extract", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(authorize_api)])
@app.post("/api/analysis/{recording_id}/extract", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(authorize_api)])
async def extract_recording(recording_id: str, request: ExtractRequest) -> dict:
    """Deprecated compatibility route; new processing never creates VAD clips."""
    return await process_target_audio(
        recording_id, ProcessTargetAudioRequest(speaker_id=request.speaker_id)
    )


def _trim_wav(path: Path, start_seconds: float, end_seconds: float | None) -> AudioInput:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2: raise ValueError("Unsupported audio format")
        rate = handle.getframerate(); total = handle.getnframes()
        start = min(total, int(start_seconds * rate)); end = min(total, int((end_seconds if end_seconds is not None else total/rate) * rate))
        if end <= start or end-start < rate // 10: raise ValueError("Selected audio is too short")
        handle.setpos(start); pcm = handle.readframes(end-start)
    return AudioInput(audio_data=base64.b64encode(pcm).decode(), sample_rate=rate)


@app.post("/api/recordings/{recording_id}/promote", dependencies=[Depends(authorize_api)])
@app.post("/api/analysis/{recording_id}/promote", dependencies=[Depends(authorize_api)])
async def promote_recording(recording_id: str, request: PromoteRecordingRequest) -> dict:
    if bool(request.speaker_id) == bool(request.new_speaker_name): raise HTTPException(status_code=400, detail="Choose an existing or a new speaker")
    path = await asyncio.to_thread(recognizer.catalog.audio_path, recording_id, "original")
    if not path: raise HTTPException(status_code=404, detail="Recording not found")
    try:
        audio = await asyncio.to_thread(_trim_wav, path, request.start_seconds, request.end_seconds)
        quality_reports = await asyncio.to_thread(
            _registration_quality_reports,
            [audio], request.accept_quality_warnings,
        )
        if request.speaker_id:
            profile = next((item for item in recognizer.list_speakers() if item.id == request.speaker_id), None)
            if not profile: raise HTTPException(status_code=404, detail="Speaker not found")
            speaker = await asyncio.to_thread(
                recognizer.enroll, profile.name, [audio], False,
                profile.person_entity_id, False, profile.profile_kind,
                profile.expires_at, True, True, recording_id,
            )
        else:
            speaker = await asyncio.to_thread(
                recognizer.enroll, request.new_speaker_name or "", [audio], False,
                request.person_entity_id, request.person_entity_id is not None,
                request.profile_kind, request.expires_at,
                "expires_at" in request.model_fields_set,
                "profile_kind" in request.model_fields_set,
                recording_id,
            )
        return {"speaker": speaker.model_dump(mode="json"), "quality_reports": quality_reports}
    except HTTPException:
        raise
    except ValueError as error: raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/recordings/{recording_id}", status_code=204, dependencies=[Depends(authorize_api)])
@app.delete("/api/analysis/{recording_id}", status_code=204, dependencies=[Depends(authorize_api)])
async def delete_recording(recording_id: str) -> Response:
    task = processing_tasks.get(recording_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if not await asyncio.to_thread(recognizer.catalog.delete_recording, recording_id): raise HTTPException(status_code=404, detail="Recording not found")
    return Response(status_code=204)


@app.post("/api/recordings/delete", dependencies=[Depends(authorize_api)])
@app.post("/api/analysis/delete", dependencies=[Depends(authorize_api)])
async def bulk_delete_recordings(request: BulkDeleteRequest) -> dict:
    ids = request.ids or []
    if request.all_filtered:
        filters = request.filters or {}
        ids = await asyncio.to_thread(
            recognizer.catalog.recording_ids,
            outcome=filters.get("outcome"), source=filters.get("source"),
            speaker_id=filters.get("speaker_id"), query=filters.get("q"),
            since=filters.get("since"),
        )
    deleted = 0
    for item in ids:
        task = processing_tasks.get(item)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        deleted += bool(
            await asyncio.to_thread(recognizer.catalog.delete_recording, item)
        )
    return {"deleted": deleted}


@app.get("/api/speakers/{speaker_id}/samples", dependencies=[Depends(authorize_api)])
async def list_samples(speaker_id: str) -> list[dict]:
    samples = await asyncio.to_thread(recognizer.catalog.list_samples, speaker_id)
    return [{key: value for key, value in sample.items() if key != "path"} for sample in samples]


@app.get("/api/speakers/{speaker_id}/samples/{sample_id}/audio", dependencies=[Depends(authorize_api)])
async def sample_audio(speaker_id: str, sample_id: str) -> FileResponse:
    sample = await asyncio.to_thread(recognizer.catalog.get_sample, sample_id)
    path = await asyncio.to_thread(recognizer.catalog.sample_path, sample_id)
    if not sample or sample["speaker_id"] != speaker_id or not path: raise HTTPException(status_code=404, detail="Sample not found")
    return FileResponse(path, media_type="audio/wav", filename=f"{sample_id}.wav")


@app.patch("/api/speakers/{speaker_id}/samples/{sample_id}", dependencies=[Depends(authorize_api)])
async def set_sample_active(speaker_id: str, sample_id: str, request: SampleActiveRequest) -> dict:
    try:
        updated, speaker = await asyncio.to_thread(
            recognizer.set_sample_active_and_retrain,
            speaker_id, sample_id, request.active,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Sample not found") from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    public_sample = {
        key: value for key, value in (updated or {}).items() if key != "path"
    }
    return {"sample": public_sample, "speaker": speaker.model_dump(mode="json")}


@app.delete("/api/speakers/{speaker_id}/samples/{sample_id}", status_code=204, dependencies=[Depends(authorize_api)])
async def delete_sample(speaker_id: str, sample_id: str) -> Response:
    try:
        await asyncio.to_thread(recognizer.delete_sample_and_retrain, speaker_id, sample_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Sample not found") from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=204)


@app.get("/api/calibration", dependencies=[Depends(authorize_api)])
async def calibration_preview() -> dict:
    preview = await asyncio.to_thread(recognizer.calibration_preview)
    return {"preview": preview, "applied": recognizer.catalog.calibration(), "base_threshold": settings.recognition_threshold}


@app.post("/api/calibration", dependencies=[Depends(authorize_api)])
async def apply_calibration(request: CalibrationApplyRequest) -> dict:
    preview = await asyncio.to_thread(recognizer.calibration_preview)
    if not preview.get("ready"):
        raise HTTPException(status_code=409, detail=preview.get("reason", "Calibration data is insufficient"))
    applied = await asyncio.to_thread(recognizer.catalog.set_calibration, request.threshold, request.margin, preview)
    return {"applied": applied, "preview": preview}


@app.delete("/api/calibration", status_code=204, dependencies=[Depends(authorize_api)])
async def reset_calibration() -> Response:
    await asyncio.to_thread(recognizer.catalog.set_calibration, None, None, {})
    return Response(status_code=204)


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
