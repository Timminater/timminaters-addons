"""Bounded, offline DeepFilterNet2 speech enhancement."""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

import numpy as np
from numpy.typing import NDArray

_LOGGER = logging.getLogger(__name__)

CANONICAL_RATE = 16_000
DENOISE_RATE = 48_000
MAX_CLIP_SECONDS = 120
WORKER_IDLE_SECONDS = 300
DEFAULT_LIVE_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class ProcessedAudioResult:
    denoised_pcm: bytes | None
    # Retained as an always-empty compatibility field for partially upgraded
    # companion integrations. Speaker isolation is no longer implemented.
    isolated_pcm: bytes | None
    sample_rate: int
    stages: dict[str, str]
    timings: dict[str, float]
    quality: dict[str, float | bool | str]
    fallback_reason: str | None = None


def resample_audio(
    audio: NDArray[np.float32], source_rate: int, target_rate: int
) -> NDArray[np.float32]:
    """Deterministically resample mono audio while preserving its duration."""
    value = np.asarray(audio, dtype=np.float32)
    if source_rate == target_rate or not value.size:
        return value.copy()
    target_count = max(1, round(value.size * target_rate / source_rate))
    source_positions = np.arange(value.size, dtype=np.float64)
    target_positions = np.linspace(0, max(0, value.size - 1), target_count)
    return np.asarray(
        np.interp(target_positions, source_positions, value),
        dtype=np.float32,
    )


def _fixed_length(value: NDArray[np.float32], length: int) -> NDArray[np.float32]:
    if value.size == length:
        return value
    if value.size > length:
        return value[:length]
    return np.pad(value, (0, length - value.size))


def _pcm(value: NDArray[np.float32]) -> bytes:
    return np.asarray(
        np.clip(value, -1, 0.9999695) * 32768,
        dtype="<i2",
    ).tobytes()


def _quality(
    original: NDArray[np.float32],
    denoised: NDArray[np.float32],
) -> dict[str, float | bool | str]:
    def metrics(value: NDArray[np.float32]) -> dict[str, float | bool]:
        rms = float(np.sqrt(np.mean(np.square(value)))) if value.size else 0.0
        clipping = float(np.mean(np.abs(value) >= 0.999))
        silence = float(np.mean(np.abs(value) < 0.003))
        duration = value.size == original.size
        return {
            "rms": round(rms, 6),
            "clipping_ratio": round(clipping, 6),
            "silence_ratio": round(silence, 6),
            "duration_preserved": duration,
            "passed": bool(
                duration
                and rms >= 0.0001
                and clipping < 0.01
                and silence < 0.98
            ),
        }

    original_metrics = metrics(original)
    denoised_metrics = metrics(denoised)
    return {
        "result": "accepted" if denoised_metrics["passed"] else "rejected",
        "passed": denoised_metrics["passed"],
        "duration_preserved": denoised_metrics["duration_preserved"],
        "original_rms": original_metrics["rms"],
        "denoised_rms": denoised_metrics["rms"],
        "denoised_passed": denoised_metrics["passed"],
        "denoised_clipping_ratio": denoised_metrics["clipping_ratio"],
        "denoised_silence_ratio": denoised_metrics["silence_ratio"],
        "clipping_ratio": denoised_metrics["clipping_ratio"],
        "silence_ratio": denoised_metrics["silence_ratio"],
    }


class _Models:
    def __init__(self, deepfilter_path: str) -> None:
        self.deepfilter_path = deepfilter_path
        self._deepfilter: tuple[Any, Any, Any] | None = None

    def denoise(
        self,
        audio: NDArray[np.float32],
    ) -> tuple[NDArray[np.float32], bool, float | None]:
        import torch
        import df.logger
        from df.enhance import enhance, init_df

        model_was_loaded = self._deepfilter is not None
        model_load_ms: float | None = None
        if self._deepfilter is None:
            load_started = time.perf_counter()
            # DeepFilterNet's logger queries the source checkout's Git metadata.
            # Production images contain the pinned model, not Git or a checkout.
            df.logger.get_commit_hash = lambda: None
            loaded = init_df(
                model_base_dir=self.deepfilter_path,
                log_level="ERROR",
                log_file=None,
                default_model="DeepFilterNet2",
            )
            model, state = loaded[0], loaded[1]
            self._deepfilter = model, state, torch
            model_load_ms = round(
                (time.perf_counter() - load_started) * 1000,
                2,
            )
        model, state, torch_module = self._deepfilter
        enhanced = enhance(
            model,
            state,
            torch_module.from_numpy(audio).unsqueeze(0),
            pad=True,
        )
        return (
            np.asarray(enhanced.squeeze(0).cpu().numpy(), dtype=np.float32),
            model_was_loaded,
            model_load_ms,
        )


def _worker_main(connection: Connection, deepfilter_path: str) -> None:
    models = _Models(deepfilter_path)
    while connection.poll(WORKER_IDLE_SECONDS):
        try:
            request = connection.recv()
        except EOFError:
            break
        if request is None:
            break
        original = np.asarray(request["audio"], dtype=np.float32)
        original = original[: MAX_CLIP_SECONDS * CANONICAL_RATE]
        stages: dict[str, str] = {}
        timings: dict[str, float] = {}
        fallback_reason: str | None = None
        denoise_started = time.perf_counter()
        model_was_loaded = False
        model_load_ms: float | None = None
        try:
            at_48k = resample_audio(original, CANONICAL_RATE, DENOISE_RATE)
            enhanced, model_was_loaded, model_load_ms = models.denoise(at_48k)
            denoised = _fixed_length(
                resample_audio(enhanced, DENOISE_RATE, CANONICAL_RATE),
                original.size,
            )
            if not np.all(np.isfinite(denoised)) or not np.any(denoised):
                raise ValueError("Denoiser returned invalid audio")
            stages["denoise"] = "ready"
            stages["model"] = "warm" if model_was_loaded else "loaded_cold"
        except Exception as error:  # A model failure must be fail-open for Assist.
            _LOGGER.warning("DeepFilterNet2 failed: %s", error)
            denoised = original.copy()
            stages["denoise"] = "failed"
            stages["model"] = "warm" if model_was_loaded else "load_failed"
            fallback_reason = "denoise_failed"

        stage_ms = round((time.perf_counter() - denoise_started) * 1000, 2)
        if model_was_loaded:
            # Only warm runs are comparable and therefore receive denoise_ms.
            timings["denoise_ms"] = stage_ms
        else:
            timings["cold_start_ms"] = stage_ms
            if model_load_ms is not None:
                timings["model_load_ms"] = model_load_ms

        quality = _quality(original, denoised)
        quality["model_was_loaded"] = model_was_loaded
        quality["timing_comparable"] = model_was_loaded
        denoised_output: NDArray[np.float32] | None = denoised
        if quality["denoised_passed"] is not True:
            denoised_output = None
            stages["denoise"] = "rejected_quality"
            fallback_reason = "denoised_quality_failed"

        connection.send(
            {
                "denoised_pcm": (
                    _pcm(denoised_output)
                    if denoised_output is not None
                    else None
                ),
                "sample_rate": CANONICAL_RATE,
                "stages": stages,
                "timings": timings,
                "quality": quality,
                "fallback_reason": fallback_reason,
            }
        )
    connection.close()


class TargetAudioProcessor:
    """Own one lazy denoise worker and terminate it after inactivity."""

    def __init__(self, *, deepfilter_path: str | None = None) -> None:
        self.deepfilter_path = deepfilter_path or os.environ.get(
            "DEEPFILTER_MODEL_DIR",
            "/opt/models/DeepFilterNet2",
        )
        self._process: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._lock = threading.Lock()
        self._priority_lock = threading.Lock()
        self._waiting_live = 0

    def _live_waiting(self) -> bool:
        with self._priority_lock:
            return self._waiting_live > 0

    def _ensure_worker(self) -> Connection:
        if (
            self._process is not None
            and self._process.is_alive()
            and self._connection is not None
        ):
            return self._connection
        self.close()
        parent, child = multiprocessing.Pipe()
        process = multiprocessing.Process(
            target=_worker_main,
            args=(child, self.deepfilter_path),
            daemon=True,
            name="speaker-recognition-denoise",
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent
        return parent

    def process(
        self,
        audio: NDArray[np.float32],
        _reference: NDArray[np.float32] | None = None,
        *,
        timeout_seconds: float = DEFAULT_LIVE_TIMEOUT_SECONDS,
        priority: str = "live",
    ) -> ProcessedAudioResult:
        started = time.perf_counter()
        if priority not in {"live", "analysis"}:
            raise ValueError("Unsupported processing priority")
        if priority == "live":
            with self._priority_lock:
                self._waiting_live += 1
        if not self._lock.acquire(timeout=max(0.1, timeout_seconds)):
            if priority == "live":
                with self._priority_lock:
                    self._waiting_live -= 1
            return self._failure(
                "processor_busy",
                "busy",
                started,
            )
        if priority == "live":
            with self._priority_lock:
                self._waiting_live -= 1
        try:
            if priority == "analysis" and self._live_waiting():
                return self._failure(
                    "analysis_preempted_for_live_stt",
                    "preempted",
                    started,
                )
            connection = self._ensure_worker()
            connection.send({"audio": np.asarray(audio, dtype=np.float32)})
            deadline = started + timeout_seconds
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    self.close()
                    return self._failure(
                        "processing_timeout",
                        "timeout",
                        started,
                    )
                if connection.poll(min(0.1, remaining)):
                    break
                if priority == "analysis" and self._live_waiting():
                    self.close()
                    return self._failure(
                        "analysis_preempted_for_live_stt",
                        "preempted",
                        started,
                    )
            payload = connection.recv()
            timings = dict(payload["timings"])
            request_ms = round((time.perf_counter() - started) * 1000, 2)
            if payload["quality"].get("model_was_loaded"):
                timings["audio_processing_ms"] = request_ms
            else:
                timings["cold_request_ms"] = request_ms
            return ProcessedAudioResult(
                denoised_pcm=payload["denoised_pcm"],
                isolated_pcm=None,
                sample_rate=int(payload["sample_rate"]),
                stages=dict(payload["stages"]),
                timings=timings,
                quality=dict(payload["quality"]),
                fallback_reason=payload.get("fallback_reason"),
            )
        except (EOFError, BrokenPipeError, OSError) as error:
            _LOGGER.warning("Audio model worker failed: %s", error)
            self.close()
            return self._failure("worker_failed", "failed", started)
        finally:
            self._lock.release()

    @staticmethod
    def _failure(
        reason: str,
        stage: str,
        started: float,
    ) -> ProcessedAudioResult:
        return ProcessedAudioResult(
            denoised_pcm=None,
            isolated_pcm=None,
            sample_rate=CANONICAL_RATE,
            stages={"denoise": stage, "model": stage},
            timings={
                "cold_request_ms": round(
                    (time.perf_counter() - started) * 1000,
                    2,
                )
            },
            quality={
                "model_was_loaded": False,
                "timing_comparable": False,
            },
            fallback_reason=reason,
        )

    def close(self) -> None:
        connection, process = self._connection, self._process
        self._connection = None
        self._process = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
