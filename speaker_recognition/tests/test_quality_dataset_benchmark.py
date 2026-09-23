from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import quality_dataset_benchmark as benchmark


def test_manifest_rejects_duplicate_ids() -> None:
    manifest = Path(__file__).parent / "fixtures" / "quality_duplicate_manifest.json"
    with pytest.raises(ValueError, match="Duplicate case id"):
        benchmark.load_manifest(manifest)


def test_unknown_false_accept_and_wrong_identity_fail_without_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    responses = iter([
        ({"matched": True, "outcome": "matched", "speaker": {"name": "speaker_b"}}, 8.0),
        ({"matched": True, "outcome": "matched", "speaker": {"name": "speaker_b"}}, 9.0),
    ])
    cases = [
        {"id": "unknown-case", "path": "unknown.wav", "expected": "unknown",
         "day": "day_1", "device": "mic_a", "condition": "quiet"},
        {"id": "wrong-person-case", "path": "wrong.wav", "expected": "speaker_a",
         "day": "day_1", "device": "mic_a", "condition": "quiet"},
    ]

    monkeypatch.setattr(benchmark, "load_manifest", lambda _path: cases)
    monkeypatch.setattr(benchmark, "read_wav", lambda _path: (b"\x00\x00", 16_000))
    monkeypatch.setattr(benchmark, "call_recognizer", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setenv("SPEAKER_RECOGNITION_TOKEN", "test-only")
    monkeypatch.setattr(sys, "argv", ["quality_dataset_benchmark.py", "--manifest", "manifest.json"])

    assert benchmark.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["identity_counts"] == {"false_accept": 1, "wrong_identity": 1}
    assert len(report["hard_failures"]) == 2
    assert report["latency_ms"]["overall"]["n"] == 2
