"""Local manifest-driven identity benchmark using the transient recognize API."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import statistics
import sys
import time
import urllib.request
import wave
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


GROUP_FIELDS = ("expected", "day", "device", "condition")
MAX_AUDIO_SECONDS = 120
MAX_AUDIO_BYTES = 16 * 1024 * 1024


def load_manifest(path: Path) -> list[dict[str, str]]:
    """Read a JSON array/object or CSV without logging fixture contents."""
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document.get("cases") if isinstance(document, dict) else document
    if not isinstance(rows, list) or not rows:
        raise ValueError("Manifest must contain a non-empty case list")

    cases: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Manifest row {index} must be an object")
        case = {key: str(row.get(key, "")).strip() for key in ("id", "path", *GROUP_FIELDS)}
        if not case["id"] or not case["path"] or not case["expected"]:
            raise ValueError(f"Manifest row {index} needs id, path, and expected")
        if case["id"] in seen_ids:
            raise ValueError(f"Duplicate case id: {case['id']}")
        seen_ids.add(case["id"])
        for field in ("day", "device", "condition"):
            case[field] = case[field] or "unspecified"
        cases.append(case)
    return cases


def read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected mono PCM16 WAV")
        sample_rate = handle.getframerate()
        if not 8000 <= sample_rate <= 48000:
            raise ValueError(f"{path.name}: sample rate must be 8-48 kHz")
        frames = handle.getnframes()
        if frames <= 0 or frames > sample_rate * MAX_AUDIO_SECONDS:
            raise ValueError(f"{path.name}: audio duration must be 0-{MAX_AUDIO_SECONDS}s")
        pcm = handle.readframes(frames)
    if len(pcm) > MAX_AUDIO_BYTES:
        raise ValueError(f"{path.name}: audio exceeds {MAX_AUDIO_BYTES} bytes")
    return pcm, sample_rate


def call_recognizer(base_url: str, token: str, pcm: bytes, sample_rate: int,
                    timeout: float) -> tuple[dict[str, Any], float]:
    payload = json.dumps({
        "audio": {
            "audio_data": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": sample_rate,
        }
    }).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/recognize",
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    return result, (time.perf_counter() - started) * 1000


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * quantile / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 3)


def summarize_latency(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "mean_ms": round(statistics.mean(values), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="JSON or CSV manifest; relative audio paths use its directory")
    parser.add_argument("--url", default="http://127.0.0.1:18099")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path,
                        help="Optional JSON report path; report contains case IDs and labels")
    args = parser.parse_args()
    token = os.environ.get("SPEAKER_RECOGNITION_TOKEN", "")
    if not token:
        parser.error("Set SPEAKER_RECOGNITION_TOKEN in the environment")
    try:
        cases = load_manifest(args.manifest)
        rows: list[dict[str, Any]] = []
        failures: list[str] = []
        for case in cases:
            audio_path = Path(case["path"])
            if not audio_path.is_absolute():
                audio_path = args.manifest.resolve().parent / audio_path
            pcm, sample_rate = read_wav(audio_path)
            response, elapsed_ms = call_recognizer(
                args.url, token, pcm, sample_rate, args.timeout
            )
            speaker = response.get("speaker")
            actual = speaker.get("name") if isinstance(speaker, dict) else None
            expected = case["expected"]
            outcome = str(response.get("outcome", "error"))
            unknown = expected.casefold() == "unknown"
            if unknown and (response.get("matched") or actual):
                status = "false_accept"
                failures.append(f"{case['id']}: unknown fixture accepted as {actual!r}")
            elif not unknown and actual and actual.casefold() != expected.casefold():
                status = "wrong_identity"
                failures.append(f"{case['id']}: expected {expected!r}, got {actual!r}")
            elif not unknown and actual and actual.casefold() == expected.casefold():
                status = "correct"
            elif not unknown:
                status = "missed_known"
            else:
                status = "correct_unknown"
            rows.append({
                "id": case["id"], "expected": expected, "actual": actual,
                "outcome": outcome, "status": status,
                "latency_ms": round(elapsed_ms, 3),
                **{field: case[field] for field in ("day", "device", "condition")},
            })

        grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for row in rows:
            for field in GROUP_FIELDS:
                grouped[field][row[field]].append(row["latency_ms"])
        report = {
            "api": "/api/recognize (transient; no analysis-history recording)",
            "case_count": len(rows),
            "identity_counts": dict(Counter(row["status"] for row in rows)),
            "latency_ms": {
                "overall": summarize_latency([row["latency_ms"] for row in rows]),
                "by_group": {
                    field: {key: summarize_latency(values)
                            for key, values in sorted(groups.items())}
                    for field, groups in sorted(grouped.items())
                },
            },
            "cases": rows,
            "hard_failures": failures,
        }
        serialized = json.dumps(report, indent=2, sort_keys=True)
        if args.output:
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 1 if failures else 0
    except Exception as error:
        print(f"benchmark error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
