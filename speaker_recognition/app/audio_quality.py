"""Objective, model-free feedback for bounded PCM16 mono recordings."""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np


MAX_ANALYZED_SECONDS = 120
MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 96_000
FRAME_SECONDS = 0.02
MIN_REGISTRATION_SPEECH_SECONDS = 0.6
MIN_REGISTRATION_SPEECH_RATIO = 0.05
REGISTRATION_CLIPPING_RATIO = 0.01
ADVISORY_CLIPPING_RATIO = 0.001
HIGH_BACKGROUND_DBFS = -35.0


def assess_pcm16_mono(
    pcm: bytes,
    sample_rate: int,
    *,
    purpose: Literal["analysis", "registration"] = "analysis",
) -> dict[str, Any]:
    """Return energy-based recording metrics, flags, and an acceptance decision.

    Speech activity is an energy estimate, not speech recognition or speaker
    identity evidence. High background energy is advisory; short enrollment
    audio and substantial clipping are hard registration rejections.
    """
    if purpose not in {"analysis", "registration"}:
        raise ValueError("purpose must be 'analysis' or 'registration'")
    if not isinstance(pcm, bytes):
        raise ValueError("PCM audio must be bytes")
    if not pcm:
        raise ValueError("PCM audio is empty")
    if len(pcm) % 2:
        raise ValueError("PCM16 audio must contain complete samples")
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
    ):
        raise ValueError("sample rate is outside the supported range")

    samples_i16 = np.frombuffer(pcm, dtype="<i2")
    duration = samples_i16.size / sample_rate
    if duration > MAX_ANALYZED_SECONDS:
        raise ValueError("PCM audio exceeds the quality analysis duration limit")

    samples = samples_i16.astype(np.float32) / 32768.0
    frame_samples = max(1, round(sample_rate * FRAME_SECONDS))
    frame_count = math.ceil(samples.size / frame_samples)
    padded = np.zeros(frame_count * frame_samples, dtype=np.float32)
    padded[: samples.size] = samples
    frames = padded.reshape(frame_count, frame_samples)
    frame_rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-6))

    # Cap the relative threshold so continuous speech is not mistaken for its
    # own noise floor. This is a conservative energy heuristic, not a VAD model.
    estimated_noise_floor = float(np.percentile(frame_dbfs, 20))
    activity_threshold = max(-42.0, min(estimated_noise_floor + 10.0, -30.0))
    active_frames = frame_dbfs >= activity_threshold
    active_seconds = float(np.count_nonzero(active_frames) * FRAME_SECONDS)
    active_seconds = min(active_seconds, duration)
    active_ratio = active_seconds / duration

    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    peak = float(np.max(np.abs(samples)))
    clipping_ratio = float(np.count_nonzero(np.abs(samples_i16) >= 32760) / samples_i16.size)
    rms_dbfs = 20.0 * math.log10(max(rms, 1e-6))
    peak_dbfs = 20.0 * math.log10(max(peak, 1.0 / 32768.0))

    flags: list[dict[str, Any]] = []
    too_little_speech = (
        active_seconds < MIN_REGISTRATION_SPEECH_SECONDS
        or active_ratio < MIN_REGISTRATION_SPEECH_RATIO
    )
    if too_little_speech:
        flags.append(
            {
                "code": "too_little_active_speech",
                "severity": "rejection" if purpose == "registration" else "advisory",
                "metric": "active_speech_seconds",
                "value": round(active_seconds, 3),
                "minimum_seconds": MIN_REGISTRATION_SPEECH_SECONDS,
                "active_ratio": round(active_ratio, 4),
                "minimum_ratio": MIN_REGISTRATION_SPEECH_RATIO,
            }
        )

    if clipping_ratio >= ADVISORY_CLIPPING_RATIO:
        hard_clipping = (
            purpose == "registration"
            and clipping_ratio >= REGISTRATION_CLIPPING_RATIO
        )
        flags.append(
            {
                "code": "clipping_detected",
                "severity": "rejection" if hard_clipping else "advisory",
                "metric": "clipping_ratio",
                "value": round(clipping_ratio, 6),
                "warning_threshold": ADVISORY_CLIPPING_RATIO,
                "rejection_threshold": REGISTRATION_CLIPPING_RATIO,
            }
        )

    # A flat test tone or sustained voiced sound has no measurable quiet
    # background region; avoid claiming its level is environmental noise.
    if estimated_noise_floor >= HIGH_BACKGROUND_DBFS and float(np.std(frame_dbfs)) > 0.15:
        flags.append(
            {
                "code": "high_background_level",
                "severity": "advisory",
                "metric": "estimated_noise_floor_dbfs",
                "value": round(estimated_noise_floor, 2),
                "threshold_dbfs": HIGH_BACKGROUND_DBFS,
            }
        )

    rejected = any(flag["severity"] == "rejection" for flag in flags)
    decision = "rejected" if rejected else "review" if flags else "accepted"
    return {
        "schema_version": 1,
        "purpose": purpose,
        "decision": decision,
        "accepted": not rejected,
        "metrics": {
            "duration_seconds": round(duration, 3),
            "active_speech_seconds": round(active_seconds, 3),
            "active_speech_ratio": round(active_ratio, 4),
            "rms_dbfs": round(rms_dbfs, 2),
            "peak_dbfs": round(peak_dbfs, 2),
            "clipping_ratio": round(clipping_ratio, 6),
            "estimated_noise_floor_dbfs": round(estimated_noise_floor, 2),
            "activity_threshold_dbfs": round(activity_threshold, 2),
        },
        "flags": flags,
    }
