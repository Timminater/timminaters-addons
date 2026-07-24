from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from app.df3_streaming import DF3_DELAY_HOPS, DF3_HOP, Df3StreamingSession


class DelayedIdentityEngine:
    """Model double with the measured three-hop DF3 timing displacement."""

    def __init__(self) -> None:
        self._delay = deque(
            np.zeros(DF3_HOP, dtype=np.float32)
            for _ in range(DF3_DELAY_HOPS)
        )
        self.calls = 0

    def process_hop(self, hop_audio: np.ndarray) -> np.ndarray:
        self.calls += 1
        self._delay.append(hop_audio.copy())
        return self._delay.popleft()


class RatioThreeResampler:
    def __init__(self, input_rate: int, output_rate: int) -> None:
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.final_calls = 0

    def resample_chunk(
        self, audio: np.ndarray, *, last: bool = False
    ) -> np.ndarray:
        if last:
            self.final_calls += 1
        if self.input_rate == 16_000 and self.output_rate == 48_000:
            return np.repeat(audio.astype(np.int16), 3)
        if self.input_rate == 48_000 and self.output_rate == 16_000:
            return audio.astype(np.int16)[::3]
        raise AssertionError("unexpected test resampling ratio")


def _session(rate: int = 16_000):
    engines: list[DelayedIdentityEngine] = []
    resamplers: list[RatioThreeResampler] = []

    def engine_factory():
        engine = DelayedIdentityEngine()
        engines.append(engine)
        return engine

    def resampler_factory(input_rate: int, output_rate: int):
        resampler = RatioThreeResampler(input_rate, output_rate)
        resamplers.append(resampler)
        return resampler

    return (
        Df3StreamingSession(
            rate,
            engine_factory=engine_factory,
            resampler_factory=resampler_factory,
        ),
        engines,
        resamplers,
    )


def _pcm(samples: int, value: int = 1200) -> bytes:
    return np.full(samples, value, dtype="<i2").tobytes()


@pytest.mark.parametrize(
    "samples",
    [
        160,  # very short
        16_000,  # hop aligned after 16 -> 48 kHz
        16_137,  # non-hop-aligned
        5 * 16_000,
        10 * 16_000,
        30 * 16_000,
    ],
)
def test_finish_drains_without_losing_or_adding_audio(samples):
    session, engines, resamplers = _session()
    source = _pcm(samples)
    split = max(2, min(len(source), 997 * 2))
    for offset in range(0, len(source), split):
        session.process_chunk(source[offset : offset + split])

    result = session.finish()

    assert result.pcm == source
    assert result.metrics["source_samples"] == samples
    assert result.metrics["output_samples"] == samples
    assert result.metrics["duration_delta_ms"] == 0.0
    assert result.metrics["stateful"] is True
    assert result.metrics["drain_hops"] == DF3_DELAY_HOPS
    assert engines[0].calls > DF3_DELAY_HOPS
    assert [item.final_calls for item in resamplers] == [1, 1]
    assert result.timings["post_utterance_ms"] >= 0


def test_silence_is_drained_and_duration_preserved():
    session, _, _ = _session()
    source = _pcm(16_137, value=0)

    session.process_chunk(source)
    result = session.finish()

    assert result.pcm == source
    assert result.metrics["duration_delta_ms"] == 0.0


def test_processing_happens_before_utterance_finish():
    session, engines, _ = _session()

    session.process_chunk(_pcm(2 * 16_000))

    assert engines[0].calls >= 200


def test_new_utterance_gets_fresh_model_and_resampler_state():
    first, first_engines, first_resamplers = _session()
    second, second_engines, second_resamplers = _session()
    source = _pcm(16_137)

    first.process_chunk(source)
    first_result = first.finish()
    second.process_chunk(source)
    second_result = second.finish()

    assert first_result.pcm == second_result.pcm == source
    assert first_engines[0] is not second_engines[0]
    assert first_resamplers[0] is not second_resamplers[0]


def test_finished_session_rejects_reuse():
    session, _, _ = _session()
    session.process_chunk(_pcm(160))
    session.finish()

    with pytest.raises(RuntimeError, match="already finished"):
        session.process_chunk(_pcm(160))
    with pytest.raises(RuntimeError, match="already finished"):
        session.finish()

