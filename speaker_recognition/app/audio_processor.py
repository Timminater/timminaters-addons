"""Bounded, offline speech enhancement and target-speaker isolation."""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

_LOGGER = logging.getLogger(__name__)

CANONICAL_RATE = 16_000
DENOISE_RATE = 48_000
SPEX_RATE = 8_000
MAX_WHOLE_CLIP_SECONDS = 30
MAX_CLIP_SECONDS = 120
CHUNK_OVERLAP_SECONDS = 1
WORKER_IDLE_SECONDS = 300
DEFAULT_LIVE_TIMEOUT_SECONDS = 12.0
MIN_AVAILABLE_MEMORY_BYTES = 700 * 1024 * 1024


@dataclass(frozen=True)
class ProcessedAudioResult:
    denoised_pcm: bytes | None
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
    return np.asarray(np.interp(target_positions, source_positions, value), dtype=np.float32)


def _fixed_length(value: NDArray[np.float32], length: int) -> NDArray[np.float32]:
    if value.size == length:
        return value
    if value.size > length:
        return value[:length]
    return np.pad(value, (0, length - value.size))


def _pcm(value: NDArray[np.float32]) -> bytes:
    return np.asarray(np.clip(value, -1, 0.9999695) * 32768, dtype="<i2").tobytes()


def restore_output_scale(
    mixture: NDArray[np.float32],
    extracted: NDArray[np.float32],
) -> NDArray[np.float32]:
    """Restore scale-invariant separation output to the mixture amplitude."""
    reference = np.asarray(mixture, dtype=np.float64)
    estimate = _fixed_length(
        np.asarray(extracted, dtype=np.float32), reference.size
    ).astype(np.float64)
    denominator = float(np.dot(estimate, estimate))
    if denominator <= 1e-12:
        return np.zeros(reference.size, dtype=np.float32)
    gain = float(np.dot(reference, estimate) / denominator)
    restored = np.asarray(estimate * gain, dtype=np.float32)
    peak = float(np.max(np.abs(restored))) if restored.size else 0.0
    if peak > 0.98:
        restored *= 0.98 / peak
    return restored


def _chunked_inference(
    mixture: NDArray[np.float32],
    infer: Any,
) -> NDArray[np.float32]:
    """Run long clips in overlapping 30-second blocks with a linear crossfade."""
    whole_samples = MAX_WHOLE_CLIP_SECONDS * SPEX_RATE
    if mixture.size <= whole_samples:
        return _fixed_length(infer(mixture), mixture.size)

    overlap = CHUNK_OVERLAP_SECONDS * SPEX_RATE
    step = whole_samples - overlap
    accumulated = np.zeros(mixture.size, dtype=np.float32)
    weights = np.zeros(mixture.size, dtype=np.float32)
    for start in range(0, mixture.size, step):
        end = min(mixture.size, start + whole_samples)
        chunk = _fixed_length(infer(mixture[start:end]), end - start)
        window = np.ones(chunk.size, dtype=np.float32)
        fade = min(overlap, chunk.size)
        if start and fade:
            window[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        if end < mixture.size and fade:
            window[-fade:] = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        accumulated[start:end] += chunk * window
        weights[start:end] += window
        if end == mixture.size:
            break
    return accumulated / np.maximum(weights, 1e-6)


def _available_memory() -> int | None:
    candidates: list[int] = []
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    # Home Assistant Apps run in a cgroup. Host MemAvailable can look healthy
    # even when the App is close to its own memory ceiling.
    for limit_path, current_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        try:
            raw_limit = limit_path.read_text(encoding="ascii").strip()
            if raw_limit == "max":
                continue
            limit = int(raw_limit)
            current = int(current_path.read_text(encoding="ascii").strip())
            if 0 < limit < 1 << 60:
                candidates.append(max(0, limit - current))
                break
        except (OSError, ValueError):
            continue
    return min(candidates) if candidates else None


def _quality(
    original: NDArray[np.float32],
    denoised: NDArray[np.float32],
    isolated: NDArray[np.float32] | None,
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
                duration and rms >= 0.0001 and clipping < 0.01 and silence < 0.98
            ),
        }

    denoised_metrics = metrics(denoised)
    isolated_metrics = metrics(isolated) if isolated is not None else None
    selected = isolated_metrics or denoised_metrics
    return {
        "result": "accepted" if selected["passed"] else "rejected",
        "passed": selected["passed"],
        "duration_preserved": selected["duration_preserved"],
        "original_rms": metrics(original)["rms"],
        "denoised_rms": denoised_metrics["rms"],
        "denoised_passed": denoised_metrics["passed"],
        "denoised_clipping_ratio": denoised_metrics["clipping_ratio"],
        "denoised_silence_ratio": denoised_metrics["silence_ratio"],
        "isolated_rms": isolated_metrics["rms"] if isolated_metrics else 0.0,
        "isolated_passed": isolated_metrics["passed"] if isolated_metrics else False,
        "isolated_clipping_ratio": (
            isolated_metrics["clipping_ratio"] if isolated_metrics else 0.0
        ),
        "isolated_silence_ratio": (
            isolated_metrics["silence_ratio"] if isolated_metrics else 0.0
        ),
        # Compatibility metrics describe the selected processed candidate.
        "clipping_ratio": selected["clipping_ratio"],
        "silence_ratio": selected["silence_ratio"],
    }


class _Models:
    def __init__(self, deepfilter_path: str, spex_path: str) -> None:
        self.deepfilter_path = deepfilter_path
        self.spex_path = spex_path
        self._deepfilter: tuple[Any, Any, Any] | None = None
        self._spex: Any | None = None

    def denoise(self, audio: NDArray[np.float32]) -> NDArray[np.float32]:
        import torch
        import df.logger
        from df.enhance import enhance, init_df

        if self._deepfilter is None:
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
        model, state, torch_module = self._deepfilter
        enhanced = enhance(
            model,
            state,
            torch_module.from_numpy(audio).unsqueeze(0),
            pad=True,
        )
        return np.asarray(enhanced.squeeze(0).cpu().numpy(), dtype=np.float32)

    def isolate(
        self, mixture: NDArray[np.float32], reference: NDArray[np.float32]
    ) -> NDArray[np.float32]:
        import torch

        if self._spex is None:
            from app.spex_model import load_spex_plus

            torch.set_num_threads(max(1, min(4, int(os.environ.get("AUDIO_MODEL_THREADS", "2")))))
            self._spex = load_spex_plus(self.spex_path)
        def infer(chunk: NDArray[np.float32]) -> NDArray[np.float32]:
            with torch.inference_mode():
                mixture_tensor = torch.from_numpy(chunk)
                reference_tensor = torch.from_numpy(reference)
                length = torch.tensor([reference.size], dtype=torch.long)
                output = self._spex(mixture_tensor, reference_tensor, length)
            return np.asarray(output.squeeze(0).cpu().numpy(), dtype=np.float32)

        return restore_output_scale(mixture, _chunked_inference(mixture, infer))


def _worker_main(
    connection: Connection, deepfilter_path: str, spex_path: str
) -> None:
    models = _Models(deepfilter_path, spex_path)
    while connection.poll(WORKER_IDLE_SECONDS):
        try:
            request = connection.recv()
        except EOFError:
            break
        if request is None:
            break
        original = np.asarray(request["audio"], dtype=np.float32)
        original = original[: MAX_CLIP_SECONDS * CANONICAL_RATE]
        reference = np.asarray(request["reference"], dtype=np.float32)
        stages: dict[str, str] = {}
        timings: dict[str, float] = {}
        fallback_reason: str | None = None

        denoise_started = time.perf_counter()
        try:
            at_48k = resample_audio(original, CANONICAL_RATE, DENOISE_RATE)
            enhanced = models.denoise(at_48k)
            denoised = _fixed_length(
                resample_audio(enhanced, DENOISE_RATE, CANONICAL_RATE), original.size
            )
            if not np.all(np.isfinite(denoised)) or not np.any(denoised):
                raise ValueError("Denoiser returned invalid audio")
            stages["denoise"] = "ready"
        except Exception as error:  # A model failure must be fail-open for Assist.
            _LOGGER.warning("DeepFilterNet2 failed: %s", error)
            denoised = original.copy()
            stages["denoise"] = "failed"
            fallback_reason = "denoise_failed"
        timings["denoise_ms"] = round((time.perf_counter() - denoise_started) * 1000, 2)
        denoised_quality = _quality(original, denoised, None)
        denoised_output: NDArray[np.float32] | None = denoised
        isolation_input = denoised
        if denoised_quality["denoised_passed"] is not True:
            denoised_output = None
            isolation_input = original
            stages["denoise"] = "rejected_quality"
            fallback_reason = "denoised_quality_failed"

        isolation_started = time.perf_counter()
        isolated: NDArray[np.float32] | None = None
        memory = _available_memory()
        if memory is not None and memory < MIN_AVAILABLE_MEMORY_BYTES:
            stages["isolation"] = "skipped_low_memory"
            fallback_reason = "insufficient_memory"
        elif reference.size < SPEX_RATE:
            stages["isolation"] = "missing_reference"
            fallback_reason = "reference_too_short"
        else:
            try:
                mixture_8k = resample_audio(isolation_input, CANONICAL_RATE, SPEX_RATE)
                reference_8k = resample_audio(reference, CANONICAL_RATE, SPEX_RATE)
                extracted_8k = models.isolate(mixture_8k, reference_8k)
                isolated = _fixed_length(
                    resample_audio(extracted_8k, SPEX_RATE, CANONICAL_RATE),
                    original.size,
                )
                if (
                    not np.all(np.isfinite(isolated))
                    or float(np.sqrt(np.mean(np.square(isolated)))) < 0.0001
                ):
                    raise ValueError("SpEx+ returned silent or invalid audio")
                stages["isolation"] = "ready"
            except Exception as error:  # A model failure must be fail-open for Assist.
                _LOGGER.warning("SpEx+ failed: %s", error)
                isolated = None
                stages["isolation"] = "failed"
                fallback_reason = "isolation_failed"
        timings["isolation_ms"] = round(
            (time.perf_counter() - isolation_started) * 1000, 2
        )
        quality = _quality(original, denoised, isolated)
        if isolated is not None and quality["isolated_passed"] is not True:
            isolated = None
            stages["isolation"] = "rejected_quality"
            fallback_reason = "isolated_quality_failed"
        connection.send(
            {
                "denoised_pcm": (
                    _pcm(denoised_output) if denoised_output is not None else None
                ),
                "isolated_pcm": _pcm(isolated) if isolated is not None else None,
                "sample_rate": CANONICAL_RATE,
                "stages": stages,
                "timings": timings,
                # Preserve the rejected candidate's measurements for diagnosis.
                "quality": quality,
                "fallback_reason": fallback_reason,
            }
        )
    connection.close()


class TargetAudioProcessor:
    """Own one lazy model process and terminate it after inactivity or timeout."""

    def __init__(
        self,
        *,
        deepfilter_path: str | None = None,
        spex_path: str | None = None,
    ) -> None:
        self.deepfilter_path = deepfilter_path or os.environ.get(
            "DEEPFILTER_MODEL_DIR", "/opt/models/DeepFilterNet2"
        )
        self.spex_path = spex_path or os.environ.get(
            "SPEX_CHECKPOINT", "/opt/models/spex/last_best_checkpoint.pt"
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
        if self._process is not None and self._process.is_alive() and self._connection:
            return self._connection
        self.close()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_worker_main,
            args=(child, self.deepfilter_path, self.spex_path),
            name="speaker-audio-models",
            daemon=True,
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent
        return parent

    def process(
        self,
        audio: NDArray[np.float32],
        reference: NDArray[np.float32],
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
            return ProcessedAudioResult(
                None,
                None,
                CANONICAL_RATE,
                {"denoise": "busy", "isolation": "busy"},
                {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
                {},
                "processor_busy",
            )
        if priority == "live":
            with self._priority_lock:
                self._waiting_live -= 1
        try:
            if priority == "analysis" and self._live_waiting():
                return ProcessedAudioResult(
                    None,
                    None,
                    CANONICAL_RATE,
                    {"denoise": "preempted", "isolation": "preempted"},
                    {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
                    {},
                    "analysis_preempted_for_live_stt",
                )
            connection = self._ensure_worker()
            connection.send(
                {
                    "audio": np.asarray(audio, dtype=np.float32),
                    "reference": np.asarray(reference, dtype=np.float32),
                }
            )
            deadline = started + timeout_seconds
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    self.close()
                    return ProcessedAudioResult(
                        None,
                        None,
                        CANONICAL_RATE,
                        {"denoise": "timeout", "isolation": "timeout"},
                        {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
                        {},
                        "processing_timeout",
                    )
                if connection.poll(min(0.1, remaining)):
                    break
                if priority == "analysis" and self._live_waiting():
                    self.close()
                    return ProcessedAudioResult(
                        None,
                        None,
                        CANONICAL_RATE,
                        {"denoise": "preempted", "isolation": "preempted"},
                        {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
                        {},
                        "analysis_preempted_for_live_stt",
                    )
            payload = connection.recv()
            timings = dict(payload["timings"])
            timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
            timings["audio_processing_ms"] = timings["total_ms"]
            return ProcessedAudioResult(
                denoised_pcm=payload["denoised_pcm"],
                isolated_pcm=payload["isolated_pcm"],
                sample_rate=int(payload["sample_rate"]),
                stages=dict(payload["stages"]),
                timings=timings,
                quality=dict(payload["quality"]),
                fallback_reason=payload.get("fallback_reason"),
            )
        except (EOFError, BrokenPipeError, OSError) as error:
            _LOGGER.warning("Audio model worker failed: %s", error)
            self.close()
            return ProcessedAudioResult(
                None,
                None,
                CANONICAL_RATE,
                {"denoise": "failed", "isolation": "failed"},
                {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
                {},
                "worker_failed",
            )
        finally:
            self._lock.release()

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
            try:
                process.close()
            except ValueError:
                pass
