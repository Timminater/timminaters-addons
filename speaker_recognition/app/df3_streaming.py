"""True stateful Pipecat DeepFilterNet3 streaming with an explicit drain.

The inference engine is provided by ``pipecat-deepfilternet-stream`` pinned in
the container build.  This module deliberately owns utterance lifecycle and
SOXR finalisation because upstream ``BaseAudioFilter.stop()`` discards both
buffers without returning their delayed audio.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

DF3_RATE = 48_000
DF3_HOP = 480
# Upstream's benchmark measures three 10 ms hops between input and output.
# Feeding this many zero hops makes the final real hop observable.  We skip
# the corresponding leading delayed hops before the 48 -> transport resampler.
DF3_DELAY_HOPS = 3


class HopEngine(Protocol):
    def process_hop(self, hop_audio: NDArray[np.float32]) -> NDArray[np.float32]: ...


class SoxrStream(Protocol):
    def resample_chunk(
        self, audio: NDArray[np.int16], *, last: bool = False
    ) -> NDArray[np.int16]: ...


@dataclass(frozen=True)
class Df3StreamResult:
    pcm: bytes
    timings: dict[str, float]
    metrics: dict[str, float | int | bool | str]


class Df3StreamingSession:
    """One state-isolated 16/48/16 kHz DF3 utterance."""

    def __init__(
        self,
        sample_rate: int,
        *,
        engine_factory: Callable[[], HopEngine] | None = None,
        resampler_factory: Callable[[int, int], SoxrStream] | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = sample_rate
        self._engine = (engine_factory or self._default_engine_factory)()
        factory = resampler_factory or self._default_resampler_factory
        self._input_resampler = (
            factory(sample_rate, DF3_RATE) if sample_rate != DF3_RATE else None
        )
        self._output_resampler = (
            factory(DF3_RATE, sample_rate) if sample_rate != DF3_RATE else None
        )
        self._input_remainder = np.zeros(0, dtype=np.float32)
        self._output_parts: list[bytes] = []
        self._source_parts: list[bytes] = []
        self._source_samples = 0
        self._emitted_hops = 0
        self._model_call_ms: list[float] = []
        self._started = time.perf_counter()
        self._cpu_started = time.process_time()
        self._finished = False

    @property
    def source_pcm(self) -> bytes:
        """Return the exact PCM received so quality checks use equal duration."""
        return b"".join(self._source_parts)

    @staticmethod
    def _default_engine_factory() -> HopEngine:
        from pipecat_deepfilternet_stream import PerFrameDfn

        return PerFrameDfn()

    @staticmethod
    def _default_resampler_factory(input_rate: int, output_rate: int) -> SoxrStream:
        import soxr

        return soxr.ResampleStream(
            in_rate=input_rate,
            out_rate=output_rate,
            num_channels=1,
            quality="QQ",
            dtype="int16",
        )

    def process_chunk(self, pcm: bytes) -> None:
        """Process one incoming mono PCM16 chunk without batching the utterance."""
        if self._finished:
            raise RuntimeError("DF3 streaming session is already finished")
        if len(pcm) % 2:
            raise ValueError("PCM16 chunks must contain complete samples")
        if not pcm:
            return
        self._source_parts.append(pcm)
        source = np.frombuffer(pcm, dtype="<i2")
        self._source_samples += source.size
        at_48k = self._resample_input(source, last=False)
        self._process_48k(at_48k)

    def finish(self) -> Df3StreamResult:
        """Drain resamplers, partial hop and model lookahead exactly once."""
        if self._finished:
            raise RuntimeError("DF3 streaming session is already finished")
        self._finished = True
        post_started = time.perf_counter()

        input_tail = self._resample_input(np.zeros(0, dtype=np.int16), last=True)
        self._process_48k(input_tail)

        padded_48k_samples = 0
        if self._input_remainder.size:
            padded_48k_samples = DF3_HOP - self._input_remainder.size
            final_hop = np.pad(
                self._input_remainder,
                (0, padded_48k_samples),
            )
            self._input_remainder = np.zeros(0, dtype=np.float32)
            self._process_hop(final_hop)

        for _ in range(DF3_DELAY_HOPS):
            self._process_hop(np.zeros(DF3_HOP, dtype=np.float32))

        if self._output_resampler is not None:
            tail = self._output_resampler.resample_chunk(
                np.zeros(0, dtype=np.int16),
                last=True,
            )
            if tail.size:
                self._output_parts.append(
                    np.asarray(tail, dtype="<i2").tobytes()
                )

        output = b"".join(self._output_parts)
        available_samples = len(output) // 2
        # A correctly drained SOXR chain must contain the complete utterance.
        # Never manufacture a deceptively fast silent tail when it does not.
        if available_samples < self._source_samples:
            missing_ms = (
                (self._source_samples - available_samples)
                * 1000
                / self.sample_rate
            )
            raise RuntimeError(
                "DF3 drain returned "
                f"{missing_ms:.3f} ms less audio than received"
            )
        output = output[: self._source_samples * 2]
        post_ms = (time.perf_counter() - post_started) * 1000
        elapsed_ms = (time.perf_counter() - self._started) * 1000
        cpu_ms = (time.process_time() - self._cpu_started) * 1000
        calls = np.asarray(self._model_call_ms, dtype=np.float64)
        source = np.frombuffer(b"".join(self._source_parts), dtype="<i2")
        denoised = np.frombuffer(output, dtype="<i2")
        peak_rss_mib = self._peak_rss_mib()
        metrics: dict[str, float | int | bool | str] = {
            "backend": "df3_streaming",
            "stateful": True,
            "source_samples": self._source_samples,
            "output_samples": denoised.size,
            "duration_delta_ms": round(
                (denoised.size - source.size) * 1000 / self.sample_rate, 3
            ),
            "padded_48k_samples": int(padded_48k_samples),
            "drain_hops": DF3_DELAY_HOPS,
            "model_calls": int(calls.size),
            "model_call_p50_ms": round(float(np.percentile(calls, 50)), 3)
            if calls.size
            else 0.0,
            "model_call_p95_ms": round(float(np.percentile(calls, 95)), 3)
            if calls.size
            else 0.0,
            "model_call_p99_ms": round(float(np.percentile(calls, 99)), 3)
            if calls.size
            else 0.0,
            "model_call_max_ms": round(float(np.max(calls)), 3)
            if calls.size
            else 0.0,
            "model_deadline_misses": int(np.sum(calls > 10.0)),
        }
        if peak_rss_mib is not None:
            metrics["peak_rss_mib"] = round(peak_rss_mib, 2)
        return Df3StreamResult(
            pcm=output,
            timings={
                # Only post-EOF work extends user-visible latency. CPU work
                # performed during speech is reported separately, not added
                # to the pipeline total as though this were batch.
                "audio_processing_ms": round(post_ms, 2),
                "post_utterance_ms": round(post_ms, 2),
                "stream_compute_ms": round(cpu_ms, 2),
                "stream_wall_ms": round(elapsed_ms, 2),
            },
            metrics=metrics,
        )

    def _resample_input(
        self, samples: NDArray[np.int16], *, last: bool
    ) -> NDArray[np.int16]:
        if self._input_resampler is None:
            return np.asarray(samples, dtype=np.int16)
        return np.asarray(
            self._input_resampler.resample_chunk(samples, last=last),
            dtype=np.int16,
        )

    def _process_48k(self, samples: NDArray[np.int16]) -> None:
        if samples.size:
            as_float = samples.astype(np.float32) / 32768.0
            self._input_remainder = np.concatenate(
                (self._input_remainder, as_float)
            )
        while self._input_remainder.size >= DF3_HOP:
            hop = self._input_remainder[:DF3_HOP]
            self._input_remainder = self._input_remainder[DF3_HOP:]
            self._process_hop(hop)

    def _process_hop(self, hop: NDArray[np.float32]) -> None:
        call_started = time.perf_counter()
        enhanced = np.asarray(self._engine.process_hop(hop), dtype=np.float32)
        self._model_call_ms.append(
            (time.perf_counter() - call_started) * 1000
        )
        if enhanced.shape != (DF3_HOP,):
            raise RuntimeError(
                f"DF3 returned invalid hop shape {enhanced.shape}"
            )
        self._emitted_hops += 1
        if self._emitted_hops <= DF3_DELAY_HOPS:
            return
        pcm = np.asarray(
            np.clip(enhanced, -1.0, 0.9999695) * 32768,
            dtype=np.int16,
        )
        if self._output_resampler is not None:
            pcm = np.asarray(
                self._output_resampler.resample_chunk(pcm, last=False),
                dtype=np.int16,
            )
        if pcm.size:
            self._output_parts.append(np.asarray(pcm, dtype="<i2").tobytes())

    @staticmethod
    def _peak_rss_mib() -> float | None:
        if os.name == "nt":
            return None
        try:
            import resource

            # Linux reports KiB, macOS bytes. Production is Linux.
            value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            return value / 1024.0
        except (ImportError, OSError):
            return None
