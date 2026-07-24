from __future__ import annotations

import numpy as np

from app.audio_processor import (
    CANONICAL_RATE,
    WORKER_IDLE_SECONDS,
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
        self.polls = iter((True, False))
        self.payload = None

    def poll(self, _timeout):
        return next(self.polls)

    def recv(self):
        return {"audio": np.full(CANONICAL_RATE, 0.1, dtype=np.float32)}

    def send(self, payload):
        self.payload = payload

    def close(self):
        return None


def test_cold_worker_run_is_marked_and_excluded_from_comparable_timing(monkeypatch):
    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: (audio.copy(), False, 125.0),
    )
    connection = FakeConnection()

    _worker_main(connection, "unused")

    assert connection.payload["stages"]["model"] == "loaded_cold"
    assert connection.payload["quality"]["model_was_loaded"] is False
    assert connection.payload["quality"]["timing_comparable"] is False
    assert connection.payload["timings"]["model_load_ms"] == 125.0
    assert "cold_start_ms" in connection.payload["timings"]
    assert "denoise_ms" not in connection.payload["timings"]


def test_warm_worker_run_has_comparable_denoise_timing(monkeypatch):
    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: (audio.copy(), True, None),
    )
    connection = FakeConnection()

    _worker_main(connection, "unused")

    assert connection.payload["stages"]["model"] == "warm"
    assert connection.payload["quality"]["model_was_loaded"] is True
    assert connection.payload["quality"]["timing_comparable"] is True
    assert "denoise_ms" in connection.payload["timings"]
    assert "model_load_ms" not in connection.payload["timings"]


def test_worker_does_not_return_rejected_denoised_audio(monkeypatch):
    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: (np.ones_like(audio), True, None),
    )
    connection = FakeConnection()

    _worker_main(connection, "unused")

    assert connection.payload["denoised_pcm"] is None
    assert connection.payload["stages"]["denoise"] == "rejected_quality"
    assert connection.payload["quality"]["denoised_passed"] is False


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


def test_worker_idle_unload_is_five_minutes():
    assert WORKER_IDLE_SECONDS == 300
