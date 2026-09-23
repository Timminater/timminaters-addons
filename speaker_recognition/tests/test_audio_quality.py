from __future__ import annotations

import numpy as np
import pytest

from app.audio_quality import assess_pcm16_mono


def pcm_tone(seconds: float, amplitude: float = 0.2, sample_rate: int = 16_000) -> bytes:
    time = np.arange(round(seconds * sample_rate), dtype=np.float64) / sample_rate
    values = np.asarray(
        np.sin(2 * np.pi * 220 * time) * amplitude * 32767,
        dtype="<i2",
    )
    return values.tobytes()


def test_quiet_and_short_registration_is_rejected_but_analysis_is_advisory():
    silence = b"\x00\x00" * 16_000

    analysis = assess_pcm16_mono(silence, 16_000)
    enrollment = assess_pcm16_mono(silence, 16_000, purpose="registration")

    assert analysis["decision"] == "review"
    assert analysis["accepted"] is True
    assert analysis["flags"][0]["code"] == "too_little_active_speech"
    assert analysis["flags"][0]["severity"] == "advisory"
    assert enrollment["decision"] == "rejected"
    assert enrollment["accepted"] is False
    assert enrollment["flags"][0]["severity"] == "rejection"


def test_adequate_speech_like_energy_has_metrics_without_identity_claims():
    samples = b"\x00\x00" * 16_000 + pcm_tone(2.0) + b"\x00\x00" * 16_000
    report = assess_pcm16_mono(samples, 16_000, purpose="registration")

    assert report["decision"] == "accepted"
    assert report["metrics"]["active_speech_seconds"] >= 1.9
    assert report["metrics"]["active_speech_ratio"] > 0.45
    assert report["metrics"]["rms_dbfs"] < report["metrics"]["peak_dbfs"]
    assert not any("speaker" in flag["code"] for flag in report["flags"])


def test_background_level_is_advisory_even_for_enrollment():
    rng = np.random.default_rng(44)
    noise = np.asarray(rng.normal(0, 0.09, 16_000 * 2) * 32767, dtype="<i2")

    report = assess_pcm16_mono(noise.tobytes(), 16_000, purpose="registration")

    background = next(flag for flag in report["flags"] if flag["code"] == "high_background_level")
    assert background["severity"] == "advisory"
    assert report["accepted"] is True
    assert report["metrics"]["estimated_noise_floor_dbfs"] >= -35


def test_clipping_is_advisory_for_analysis_and_hard_for_registration():
    samples = np.frombuffer(pcm_tone(2.0, 0.4), dtype="<i2").copy()
    samples[:400] = 32767
    clipped_pcm = samples.astype("<i2").tobytes()

    analysis = assess_pcm16_mono(clipped_pcm, 16_000)
    registration = assess_pcm16_mono(clipped_pcm, 16_000, purpose="registration")

    assert analysis["accepted"] is True
    assert next(flag for flag in analysis["flags"] if flag["code"] == "clipping_detected")["severity"] == "advisory"
    assert registration["accepted"] is False
    assert next(flag for flag in registration["flags"] if flag["code"] == "clipping_detected")["severity"] == "rejection"


@pytest.mark.parametrize(
    ("pcm", "sample_rate"),
    [(b"", 16_000), (b"\x00", 16_000), (b"\x00\x00", 7_999), (b"\x00\x00", 96_001)],
)
def test_invalid_pcm_contract_is_rejected(pcm, sample_rate):
    with pytest.raises(ValueError):
        assess_pcm16_mono(pcm, sample_rate)


def test_unknown_purpose_is_rejected():
    with pytest.raises(ValueError, match="purpose"):
        assess_pcm16_mono(b"\x00\x00", 16_000, purpose="identity")
