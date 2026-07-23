from __future__ import annotations

import numpy as np

from app.audio_processor import (
    CANONICAL_RATE,
    SPEX_RATE,
    WORKER_IDLE_SECONDS,
    TargetAudioProcessor,
    _available_memory,
    _chunked_inference,
    _quality,
    _worker_main,
    resample_audio,
    restore_output_scale,
)


def test_resampling_round_trip_preserves_duration_within_one_sample():
    source = np.linspace(-0.5, 0.5, CANONICAL_RATE * 7 + 137, dtype=np.float32)

    at_48k = resample_audio(source, CANONICAL_RATE, 48_000)
    restored = resample_audio(at_48k, 48_000, CANONICAL_RATE)

    assert abs(restored.size - source.size) <= 1
    assert abs(restored.size / CANONICAL_RATE - source.size / CANONICAL_RATE) < 0.001


def test_short_isolation_is_processed_as_one_clip():
    audio = np.ones(29 * SPEX_RATE, dtype=np.float32)
    calls: list[int] = []

    result = _chunked_inference(
        audio,
        lambda chunk: calls.append(chunk.size) or chunk.copy(),
    )

    assert calls == [audio.size]
    np.testing.assert_array_equal(result, audio)


def test_long_isolation_uses_overlapping_crossfaded_blocks():
    audio = np.linspace(-0.5, 0.5, 65 * SPEX_RATE, dtype=np.float32)
    calls: list[int] = []

    result = _chunked_inference(
        audio,
        lambda chunk: calls.append(chunk.size) or chunk.copy(),
    )

    assert calls == [30 * SPEX_RATE, 30 * SPEX_RATE, 7 * SPEX_RATE]
    assert result.size == audio.size
    np.testing.assert_allclose(result, audio, atol=1e-6)


def test_memory_admission_uses_the_smallest_host_or_cgroup_value(monkeypatch):
    values = {
        "/proc/meminfo": "MemAvailable:       4194304 kB\n",
        "/sys/fs/cgroup/memory.max": str(2 * 1024**3),
        "/sys/fs/cgroup/memory.current": str(1536 * 1024**2),
    }

    def read_text(path, **_kwargs):
        value = values.get(str(path).replace("\\", "/"))
        if value is None:
            raise OSError
        return value

    monkeypatch.setattr("pathlib.Path.read_text", read_text)

    assert _available_memory() == 512 * 1024**2


def test_scale_invariant_output_is_restored_without_clipping():
    timeline = np.arange(16_000, dtype=np.float32) / 16_000
    mixture = 0.2 * np.sin(2 * np.pi * 180 * timeline)
    unscaled = mixture * -12_000

    restored = restore_output_scale(mixture, unscaled)

    np.testing.assert_allclose(restored, mixture, atol=1e-5)
    assert float(np.max(np.abs(restored))) < 0.21


def test_invalid_isolation_quality_is_rejected():
    original = np.full(CANONICAL_RATE, 0.1, dtype=np.float32)
    denoised = original.copy()
    clipped = np.ones(CANONICAL_RATE, dtype=np.float32)

    quality = _quality(original, denoised, clipped)

    assert quality["passed"] is False
    assert quality["isolated_passed"] is False
    assert quality["result"] == "rejected"
    assert quality["clipping_ratio"] == 1.0


def test_invalid_denoised_quality_is_reported_separately():
    original = np.full(CANONICAL_RATE, 0.1, dtype=np.float32)
    silent = np.zeros(CANONICAL_RATE, dtype=np.float32)

    quality = _quality(original, silent, None)

    assert quality["denoised_passed"] is False
    assert quality["passed"] is False
    assert quality["denoised_silence_ratio"] == 1.0


def test_worker_does_not_return_rejected_denoised_audio(monkeypatch):
    class Connection:
        def __init__(self):
            self.polls = iter((True, False))
            self.payload = None

        def poll(self, _timeout):
            return next(self.polls)

        def recv(self):
            return {
                "audio": np.full(CANONICAL_RATE, 0.1, dtype=np.float32),
                "reference": np.full(CANONICAL_RATE, 0.1, dtype=np.float32),
            }

        def send(self, payload):
            self.payload = payload

        def close(self):
            return None

    monkeypatch.setattr(
        "app.audio_processor._Models.denoise",
        lambda _self, audio: np.ones_like(audio),
    )
    monkeypatch.setattr(
        "app.audio_processor._Models.isolate",
        lambda _self, mixture, _reference: mixture,
    )
    monkeypatch.setattr("app.audio_processor._available_memory", lambda: 2**30)
    connection = Connection()

    _worker_main(connection, "unused", "unused")

    assert connection.payload["denoised_pcm"] is None
    assert connection.payload["isolated_pcm"] is not None
    assert connection.payload["stages"]["denoise"] == "rejected_quality"
    assert connection.payload["quality"]["denoised_passed"] is False


def test_analysis_yields_before_starting_when_live_audio_is_waiting():
    processor = TargetAudioProcessor()
    processor._waiting_live = 1

    result = processor.process(
        np.zeros(1600, dtype=np.float32),
        np.zeros(1600, dtype=np.float32),
        priority="analysis",
    )

    assert result.fallback_reason == "analysis_preempted_for_live_stt"
    assert result.stages["isolation"] == "preempted"


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
        np.zeros(1600, dtype=np.float32),
        timeout_seconds=0.01,
    )

    assert result.fallback_reason == "processing_timeout"
    assert closed == [True]


def test_worker_idle_unload_is_five_minutes():
    assert WORKER_IDLE_SECONDS == 300
