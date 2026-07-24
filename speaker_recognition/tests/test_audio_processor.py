from __future__ import annotations

import numpy as np

from app.audio_processor import (
    CANONICAL_RATE,
    TargetAudioProcessor,
    _quality,
    _worker_main,
    resample_audio,
)


def test_resampling_round_trip_preserves_duration_within_one_sample():
    source = np.linspace(-0.5, 0.5, CANONICAL_RATE * 7 + 137, dtype=np.float32)

    at_48k = resample_audio(source, CANONICAL_RATE, 48_000)
    restored = resample_audio(at_48k, 48_000, CANONICAL_RATE)

    assert abs(restored.size - source.size) <= 1
    assert abs(restored.size / CANONICAL_RATE - source.size / CANONICAL_RATE) < 0.001


def test_invalid_denoised_quality_is_rejected():
    original = np.full(CANONICAL_RATE, 0.1, dtype=np.float32)
    silent = np.zeros(CANONICAL_RATE, dtype=np.float32)

    quality = _quality(original, silent)

    assert quality["denoised_passed"] is False
    assert quality["passed"] is False
    assert quality["denoised_silence_ratio"] == 1.0


class FakeConnection:
    def __init__(self):
        self.requests = iter(
            (
                {"audio": np.full(CANONICAL_RATE, 0.1, dtype=np.float32)},
                None,
            )
        )
        self.payloads = []

    def recv(self):
        return next(self.requests)

    def send(self, payload):
        self.payloads.append(payload)

    def close(self):
        return None


def test_worker_preloads_model_and_first_run_is_comparable(monkeypatch):
    monkeypatch.setattr(
        "app.audio_processor._Models.load",
        lambda _self: 125.0,
    )
    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: (audio.copy(), True, None),
    )
    connection = FakeConnection()

    _worker_main(connection, "unused")

    assert connection.payloads[0] == {
        "type": "ready",
        "model_load_ms": 125.0,
    }
    result = connection.payloads[1]
    assert result["stages"]["model"] == "warm"
    assert result["quality"]["model_was_loaded"] is True
    assert result["quality"]["timing_comparable"] is True
    assert "denoise_ms" in result["timings"]


def test_warm_worker_run_has_comparable_denoise_timing(monkeypatch):
    monkeypatch.setattr(
        "app.audio_processor._Models.load",
        lambda _self: 125.0,
    )
    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: (audio.copy(), True, None),
    )
    connection = FakeConnection()

    _worker_main(connection, "unused")

    result = connection.payloads[1]
    assert result["stages"]["model"] == "warm"
    assert result["quality"]["model_was_loaded"] is True
    assert result["quality"]["timing_comparable"] is True
    assert "denoise_ms" in result["timings"]
    assert "model_load_ms" not in result["timings"]


def test_worker_does_not_return_rejected_denoised_audio(monkeypatch):
    monkeypatch.setattr(
        "app.audio_processor._Models.load",
        lambda _self: 125.0,
    )
    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: (np.ones_like(audio), True, None),
    )
    connection = FakeConnection()

    _worker_main(connection, "unused")

    result = connection.payloads[1]
    assert result["denoised_pcm"] is None
    assert result["stages"]["denoise"] == "rejected_quality"
    assert result["quality"]["denoised_passed"] is False


def test_analysis_yields_before_starting_when_live_audio_is_waiting():
    processor = TargetAudioProcessor()
    processor._waiting_live = 1

    result = processor.process(
        np.zeros(1600, dtype=np.float32),
        priority="analysis",
    )

    assert result.fallback_reason == "analysis_preempted_for_live_stt"
    assert result.stages["denoise"] == "preempted"


def test_processing_timeout_closes_the_worker(monkeypatch):
    class NeverReady:
        def send(self, _payload):
            return None

        def poll(self, _timeout):
            return False

    processor = TargetAudioProcessor()
    closed: list[bool] = []
    monkeypatch.setattr(processor, "_ensure_worker", lambda: NeverReady())
    monkeypatch.setattr(processor, "close", lambda: closed.append(True))

    result = processor.process(
        np.zeros(1600, dtype=np.float32),
        timeout_seconds=0.01,
    )

    assert result.fallback_reason == "processing_timeout"
    assert closed == [True]


def test_start_skips_missing_model_directory(tmp_path):
    processor = TargetAudioProcessor(deepfilter_path=str(tmp_path / "missing"))

    assert processor.start() is False
