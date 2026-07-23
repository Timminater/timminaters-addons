"""Offline container smoke test for the bundled enhancement models."""

from __future__ import annotations

import os
import resource
import time

import numpy as np

from app.audio_processor import CANONICAL_RATE, TargetAudioProcessor


def synthetic_voice(
    timeline: np.ndarray,
    *,
    fundamental: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Create deterministic speech-like voiced syllables without a test asset."""
    pitch = fundamental + 8 * np.sin(2 * np.pi * 0.7 * timeline)
    phase = 2 * np.pi * np.cumsum(pitch) / CANONICAL_RATE
    voiced = np.zeros_like(timeline)
    # A changing harmonic envelope approximates several broad vowel formants.
    vowel_mix = (np.sin(2 * np.pi * 0.45 * timeline) + 1) / 2
    for harmonic in range(1, 24):
        frequency = harmonic * fundamental
        first_vowel = np.exp(-((frequency - 650) / 380) ** 2)
        second_vowel = np.exp(-((frequency - 1100) / 520) ** 2)
        weight = (0.15 + (1 - vowel_mix) * first_vowel + vowel_mix * second_vowel)
        voiced += (weight / harmonic) * np.sin(harmonic * phase)
    syllables = np.maximum(0, np.sin(2 * np.pi * 2.2 * timeline)) ** 0.35
    fricative_gate = np.maximum(0, np.sin(2 * np.pi * 1.1 * timeline + 1.4)) ** 8
    fricative = rng.normal(size=timeline.size).astype(np.float32)
    return np.asarray(0.30 * syllables * voiced + 0.03 * fricative_gate * fricative)


def main() -> None:
    seconds = 5
    sample_count = seconds * CANONICAL_RATE
    timeline = np.arange(sample_count, dtype=np.float32) / CANONICAL_RATE
    rng = np.random.default_rng(210)
    target = synthetic_voice(timeline, fundamental=118, rng=rng)
    competing = 0.45 * synthetic_voice(timeline, fundamental=205, rng=rng)
    mixture = np.asarray(
        target + competing + 0.01 * rng.normal(size=sample_count),
        dtype=np.float32,
    )
    reference = np.asarray(target, dtype=np.float32)

    processor = TargetAudioProcessor()
    started = time.perf_counter()
    result = processor.process(mixture, reference, timeout_seconds=30)
    elapsed = time.perf_counter() - started
    hold_seconds = float(os.environ.get("MODEL_SMOKE_HOLD_SECONDS", "0"))
    if hold_seconds:
        time.sleep(hold_seconds)
    processor.close()

    if result.denoised_pcm is None or result.isolated_pcm is None:
        raise SystemExit(
            "Model pipeline failed: "
            f"{result.stages} ({result.fallback_reason}); quality={result.quality}"
        )
    expected_bytes = sample_count * 2
    if abs(len(result.denoised_pcm) - expected_bytes) > CANONICAL_RATE * 2 * 0.05:
        raise SystemExit("Denoised audio differs by more than 50 ms")
    if abs(len(result.isolated_pcm) - expected_bytes) > CANONICAL_RATE * 2 * 0.05:
        raise SystemExit("Isolated audio differs by more than 50 ms")

    peak_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    print(
        "MODEL_PIPELINE_OK",
        f"seconds={elapsed:.3f}",
        f"peak_child_mib={peak_kib / 1024:.1f}",
        f"stages={result.stages}",
        f"timings={result.timings}",
    )


if __name__ == "__main__":
    main()
