"""Target-environment benchmark for the real stateful DF3 drain path.

Run against the built container with this repository's tests bind-mounted:

    docker run --rm -v "${PWD}/tests:/app/tests:ro" --entrypoint python \
      speaker-recognition:df3-local tests/df3_stream_benchmark.py --repeats 5
"""

from __future__ import annotations

import argparse
import json
from statistics import median

import numpy as np

from app.df3_streaming import Df3StreamingSession


def _fixture(seconds: float, sample_rate: int = 16_000) -> bytes:
    samples = int(seconds * sample_rate)
    position = np.arange(samples, dtype=np.float32) / sample_rate
    rng = np.random.default_rng(20260724 + samples)
    signal = (
        0.16 * np.sin(2 * np.pi * 180 * position)
        + 0.06 * np.sin(2 * np.pi * 420 * position)
        + 0.025 * rng.standard_normal(samples)
    )
    return np.asarray(
        np.clip(signal, -1, 0.9999695) * 32768,
        dtype="<i2",
    ).tobytes()


def _run(pcm: bytes, sample_rate: int = 16_000) -> dict:
    session = Df3StreamingSession(sample_rate)
    chunk_bytes = sample_rate // 50 * 2  # 20 ms transport chunks
    for offset in range(0, len(pcm), chunk_bytes):
        session.process_chunk(pcm[offset : offset + chunk_bytes])
    result = session.finish()
    return {**result.timings, **result.metrics}


def _percentile(values: list[float], percentile: float) -> float:
    return round(float(np.percentile(values, percentile)), 3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--durations", type=float, nargs="+", default=[5, 10, 30]
    )
    args = parser.parse_args()

    # Warm process-wide tract runnables and native libraries separately.
    _run(_fixture(1))
    report: dict[str, object] = {
        "repeats": args.repeats,
        "durations": {},
    }
    for seconds in args.durations:
        runs = [_run(_fixture(seconds)) for _ in range(args.repeats)]
        post = [float(item["post_utterance_ms"]) for item in runs]
        compute = [float(item["stream_compute_ms"]) for item in runs]
        report["durations"][str(seconds)] = {
            "post_utterance_p50_ms": _percentile(post, 50),
            "post_utterance_p95_ms": _percentile(post, 95),
            "post_utterance_p99_ms": _percentile(post, 99),
            "stream_compute_p50_ms": round(median(compute), 3),
            "model_call_p99_worst_ms": max(
                float(item["model_call_p99_ms"]) for item in runs
            ),
            "model_call_max_worst_ms": max(
                float(item["model_call_max_ms"]) for item in runs
            ),
            "deadline_misses_total": sum(
                int(item["model_deadline_misses"]) for item in runs
            ),
            "peak_rss_mib": max(
                float(item.get("peak_rss_mib", 0)) for item in runs
            ),
            "duration_delta_ms": sorted(
                {float(item["duration_delta_ms"]) for item in runs}
            ),
            "runs": runs,
        }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
