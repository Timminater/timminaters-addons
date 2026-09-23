from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.review_features import device_quality, list_review_inbox, preview_experiment, set_review
from app.storage import AudioCatalog


def pcm(value: int, seconds: float = 1.0, rate: int = 16000) -> bytes:
    import numpy as np
    return np.full(int(seconds * rate), value, dtype="<i2").tobytes()


def test_review_label_does_not_train_and_audio_less_recording_remains_reviewable(tmp_path):
    catalog = AudioCatalog(tmp_path)
    catalog.initialize()
    item = catalog.create_recording(pcm(8000), 16000, outcome="matched", speaker_id="original", satellite_id="assist_satellite.kitchen")
    catalog.remove_analysis_audio(item["id"])
    # The review feature receives no recognizer at all: labeling cannot trigger learning.

    updated = set_review(catalog, item["id"], "resolved", truth_unknown=True)

    assert updated["labels"]["truth_unknown"] is True
    inbox = list_review_inbox(catalog, status=None)
    assert inbox["total"] == 1
    assert inbox["items"][0]["has_audio"] is False
    assert "original_path" not in inbox["items"][0]


def test_inbox_includes_unknown_ambiguous_and_manual_error_flag(tmp_path):
    catalog = AudioCatalog(tmp_path); catalog.initialize()
    unknown = catalog.create_recording(pcm(5000), 16000, outcome="unmatched")
    ambiguous = catalog.create_recording(pcm(5000), 16000, outcome="ambiguous")
    matched = catalog.create_recording(pcm(5000), 16000, outcome="matched")
    set_review(catalog, matched["id"], "pending")
    assert {item["id"] for item in list_review_inbox(catalog)["items"]} == {unknown["id"], ambiguous["id"], matched["id"]}
    set_review(catalog, unknown["id"], "ignored")
    assert list_review_inbox(catalog)["total"] == 2
    with pytest.raises(ValueError):
        set_review(catalog, ambiguous["id"], "resolved", truth_speaker_id="abc", truth_unknown=True)


def test_device_quality_aggregates_recognition_unknown_quality_and_latency(tmp_path):
    catalog = AudioCatalog(tmp_path); catalog.initialize()
    catalog.create_recording(pcm(3000), 16000, outcome="matched", satellite_id="assist_satellite.office", timings={"total_ms": 120}, labels={"audio_quality": {"flags": ["clipping"]}})
    catalog.create_recording(pcm(3000), 16000, outcome="unmatched", satellite_id="assist_satellite.office", timings={"recognition_ms": 40})
    device = device_quality(catalog)["devices"][0]
    assert device["recordings"] == 2
    assert device["recognitions"] == 1 and device["unknown"] == 1
    assert device["quality_problems"] == 1
    assert device["average_wait_ms"] == 80
    assert device["wait_samples"] == 2


def test_preview_uses_manual_labels_and_does_not_mutate_calibration_or_production(tmp_path):
    catalog = AudioCatalog(tmp_path); catalog.initialize()
    profile_id = "a" * 32
    catalog.set_calibration(.7, .2, {"kept": True})
    sample = catalog.create_recording(pcm(9000), 16000, outcome="unmatched")
    set_review(catalog, sample["id"], "resolved", truth_speaker_id=profile_id)
    source = catalog.create_recording(pcm(-8000), 16000, outcome="unmatched")
    set_review(catalog, source["id"], "resolved", truth_speaker_id=profile_id)
    catalog.add_sample(profile_id, pcm(-8000), 16000, source_recording_id=source["id"])

    class StubRecognizer:
        _threshold = .8
        _min_margin = .15
        def recognize_detailed(self, audio, *, threshold, min_margin):
            return SimpleNamespace(speaker=SimpleNamespace(id=profile_id), outcome="matched", confidence=.9, margin=.3)

    recognizer = StubRecognizer()

    result = preview_experiment(catalog, recognizer, .5, 0)

    assert result["sample_count"] == 1
    assert result["excluded"]["enrollment_source"] == 1
    assert result["production_settings_changed"] is False
    assert result["alternative_model_routes"] == []
    assert catalog.calibration()["threshold"] == .7
    assert recognizer._threshold == .8 and recognizer._min_margin == .15


def test_preview_excludes_recordings_without_retained_audio(tmp_path):
    catalog = AudioCatalog(tmp_path); catalog.initialize()
    item = catalog.create_recording(pcm(2000), 16000, outcome="unmatched")
    set_review(catalog, item["id"], "resolved", truth_unknown=True)
    catalog.remove_analysis_audio(item["id"])

    class NeverCalled:
        def recognize_detailed(self, *args, **kwargs):
            raise AssertionError("missing audio must be excluded before recognition")

    result = preview_experiment(catalog, NeverCalled(), .8, 0)
    assert result["sample_count"] == 0
    assert result["excluded"]["no_audio"] == 1
