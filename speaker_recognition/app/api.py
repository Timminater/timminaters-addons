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
import wave
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings
from app.models import (
    AudioInput,
    AssistSatelliteInfo,
    AnalyzeRequest,
    BulkDeleteRequest,
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
from app.recognizer import SpeakerRecognizer
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
home_assistant = HomeAssistantClient()
satellite_enrollment = SatelliteEnrollmentCoordinator()
satellite_tasks: set[asyncio.Task] = set()
processing_tasks: dict[str, asyncio.Task] = {}
maintenance_task: asyncio.Task | None = None
_policy: dict[str, object] = {
    "unknown_speaker_policy": "allow", "extraction_mode": "off",
    "min_margin": 0.0, "retention_days": 7,
    "max_storage_bytes": 2 * 1024 * 1024 * 1024,
    "audio_processing_backend": settings.audio_processing_backend,
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global maintenance_task
    await asyncio.to_thread(recognizer.initialize)
    saved_policy = recognizer.catalog.get_setting("pipeline_policy", {})
    if isinstance(saved_policy, dict):
        _policy.update({key: value for key, value in saved_policy.items() if key in _policy})
    recognizer.configure_audio_processing_backend(
        str(_policy["audio_processing_backend"])
    )
    # Start the multiprocessing worker from the main server thread before the
    # app accepts traffic. Forking it from asyncio's thread pool is unsafe on
    # Linux once other worker threads exist.
    recognizer.warm_audio_processor()
    recognizer.catalog.retention_days = int(_policy["retention_days"])
    recognizer.catalog.max_storage_bytes = int(_policy["max_storage_bytes"])
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
        await asyncio.to_thread(recognizer.close)


async def _catalogue_maintenance() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(
                recognizer.catalog.cleanup,
                None,
                set(processing_tasks),
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
    result["original_available"] = bool(original_path)
    result["denoised_available"] = bool(denoised_path)
    result["isolated_available"] = bool(isolated_path)
    result["legacy_extracted_available"] = bool(extracted_path)
    # Compatibility flag: legacy clients still request the extracted player.
    result["extracted_available"] = bool(isolated_path or extracted_path)
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
                    result.quality = quality
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
            processing_status="complete",
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
    chunks: queue.Queue[bytes | object] = queue.Queue()

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
                chunks.put(pcm)
        if pending and body_error is None:
            body_error = HTTPException(
                status_code=400,
                detail="Streaming audio ended with an incomplete PCM16 sample",
            )
    finally:
        chunks.put(sentinel)

    processed = await processor_task
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
        detailed = await asyncio.to_thread(
            recognizer.recognize_detailed,
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
                labels=labels,
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
        recording = await asyncio.to_thread(
            recognizer.catalog.create_recording, raw, request.audio.sample_rate,
            source=request.source, satellite_id=request.satellite_id, stt_entity_id=request.stt_entity_id,
            extraction_mode=mode,
        )
        calibration = recognizer.catalog.calibration()
        threshold = float(calibration["threshold"]) if calibration else settings.recognition_threshold
        margin = float(calibration["margin"]) if calibration else float(_policy["min_margin"])
        try:
            live_deadline = (
                time.perf_counter() + 11.5 if mode == "before_stt" else None
            )
            recognition_call = asyncio.to_thread(
                recognizer.recognize_detailed,
                request.audio,
                threshold=threshold,
                min_margin=margin,
            )
            if live_deadline is not None:
                try:
                    detailed = await asyncio.wait_for(
                        recognition_call,
                        timeout=max(0.1, live_deadline - time.perf_counter()),
                    )
                except asyncio.TimeoutError:
                    labels = {
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
                detailed = await recognition_call
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
                                    recognizer.recognize_detailed,
                                    denoised_input,
                                    threshold=threshold,
                                    min_margin=margin,
                                ),
                                timeout=remaining,
                            )
                        except asyncio.TimeoutError:
                            denoised_result = None
                    # Enhancement may rescue an otherwise unknown recording.
                    if denoised_result and denoised_result.speaker is not None:
                        detailed = denoised_result
            outcome = detailed.outcome
            if (
                outcome not in {"matched", "multiple_speakers"}
                and _policy["unknown_speaker_policy"] == "block"
            ):
                outcome = "blocked"
            labels = dict(recording.get("labels") or {})
            labels["detected_speakers"] = detailed.detected_speakers
            updates = {
                "outcome": outcome, "speaker_id": detailed.speaker.id if detailed.speaker else None,
                "speaker_name": detailed.speaker.name if detailed.speaker else None, "confidence": detailed.confidence,
                "threshold": detailed.threshold, "margin": detailed.margin, "scores": detailed.scores,
                "segments": detailed.candidates, "timings": detailed.timings, "labels": labels,
                "extraction_status": "disabled" if mode == "off" else "processing" if processed else "queued" if mode == "compare" else "not_processed",
            }
            if processed is not None:
                updates.update({
                    "processing_status": "complete",
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
            if processed is not None:
                if processed.denoised_pcm:
                    recording = await asyncio.to_thread(
                        recognizer.catalog.save_audio_variant,
                        recording["id"], "denoised", processed.denoised_pcm,
                        processed.sample_rate,
                    ) or recording
                variant = "denoised" if processed.denoised_pcm else "original"
                labels = dict(recording.get("labels") or {})
                labels.update({
                    "audio_variant": variant,
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
            recording = await asyncio.to_thread(recognizer.catalog.update_recording, recording["id"], outcome="error", labels={"error": str(error)}) or recording
            raise HTTPException(status_code=409, detail={"recording_id": recording["id"], "error": str(error)}) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


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
    }


@app.get("/api/recordings", dependencies=[Depends(authorize_api)])
@app.get("/api/analysis", dependencies=[Depends(authorize_api)])
async def list_recordings(page: int = 1, page_size: int = 50, offset: int | None = None, limit: int | None = None, outcome: str | None = None, source: str | None = None, speaker_id: str | None = None, q: str | None = None, since: str | None = None) -> dict:
    if offset is not None:
        page_size = limit or page_size; page = offset // max(1, page_size) + 1
    items, total = await asyncio.to_thread(recognizer.catalog.list_recordings, page=page, page_size=page_size, outcome=outcome, source=source, speaker_id=speaker_id, query=q, since=since)
    return {"items": [_analysis_payload(item) for item in items], "total": total, "page": page, "page_size": page_size, "offset": (page-1)*page_size, "limit": page_size}


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
        detailed = await asyncio.to_thread(
            recognizer.recognize_detailed,
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
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Recording not found")
    return _analysis_payload(updated, detailed)


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
        if request.speaker_id:
            profile = next((item for item in recognizer.list_speakers() if item.id == request.speaker_id), None)
            if not profile: raise HTTPException(status_code=404, detail="Speaker not found")
            speaker = await asyncio.to_thread(recognizer.enroll, profile.name, [audio], False, request.person_entity_id, request.person_entity_id is not None)
        else:
            speaker = await asyncio.to_thread(recognizer.enroll, request.new_speaker_name or "", [audio], False, request.person_entity_id, request.person_entity_id is not None)
        return {"speaker": speaker.model_dump(mode="json")}
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
    sample = await asyncio.to_thread(recognizer.catalog.get_sample, sample_id)
    if not sample or sample["speaker_id"] != speaker_id: raise HTTPException(status_code=404, detail="Sample not found")
    if sample.get("active") and not request.active:
        active = await asyncio.to_thread(recognizer.catalog.list_samples, speaker_id, True)
        if len(active) <= 1:
            raise HTTPException(status_code=409, detail="A profile needs at least one active sample")
    updated = await asyncio.to_thread(recognizer.catalog.set_sample_active, sample_id, request.active)
    try:
        speaker = await asyncio.to_thread(recognizer.retrain_from_samples, speaker_id)
    except (ValueError, OSError) as error:
        await asyncio.to_thread(recognizer.catalog.set_sample_active, sample_id, bool(sample.get("active")))
        raise HTTPException(status_code=409, detail=str(error)) from error
    public_sample = {
        key: value for key, value in (updated or {}).items() if key != "path"
    }
    return {"sample": public_sample, "speaker": speaker.model_dump(mode="json")}


@app.delete("/api/speakers/{speaker_id}/samples/{sample_id}", status_code=204, dependencies=[Depends(authorize_api)])
async def delete_sample(speaker_id: str, sample_id: str) -> Response:
    sample = await asyncio.to_thread(recognizer.catalog.get_sample, sample_id)
    if not sample or sample["speaker_id"] != speaker_id: raise HTTPException(status_code=404, detail="Sample not found")
    if sample.get("active"):
        active = await asyncio.to_thread(recognizer.catalog.list_samples, speaker_id, True)
        if len(active) <= 1:
            raise HTTPException(status_code=409, detail="Delete the profile instead of its last active sample")
    if not await asyncio.to_thread(recognizer.catalog.delete_sample, sample_id): raise HTTPException(status_code=404, detail="Sample not found")
    try: await asyncio.to_thread(recognizer.retrain_from_samples, speaker_id)
    except ValueError: pass  # A legacy embedding still makes the profile usable.
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
