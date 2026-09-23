from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from app.models import AudioInput
from app.recognizer import SpeakerRecognizer
from conftest import audio


def make_recognizer(tmp_path, fake_factory, identity_preprocess):
    recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, fake_factory, identity_preprocess)
    recognizer.initialize()
    return recognizer


def speech_tone(frequency: float, seconds: float = 10) -> AudioInput:
    timeline = np.arange(int(16_000 * seconds), dtype=np.float32) / 16_000
    pcm = np.asarray(10_000 * np.sin(2 * np.pi * frequency * timeline), dtype="<i2")
    return AudioInput(
        audio_data=base64.b64encode(pcm.tobytes()).decode(),
        sample_rate=16_000,
    )


def mixed_speakers_audio() -> AudioInput:
    pcm = np.concatenate(
        (
            np.full(16_000, 12_000, dtype="<i2"),
            np.zeros(8_000, dtype="<i2"),
            np.full(16_000, -12_000, dtype="<i2"),
        )
    )
    return AudioInput(
        audio_data=base64.b64encode(pcm.tobytes()).decode(),
        sample_rate=16_000,
    )


def test_decision_margin_uses_same_time_region(tmp_path, fake_factory, identity_preprocess, monkeypatch):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    recognizer.enroll("Alice", [audio(12000)])
    recognizer.enroll("Bob", [audio(-12000)])
    pcm = np.concatenate((
        np.full(16_000, 12_000, dtype="<i2"),
        np.full(16_000, -12_000, dtype="<i2"),
    ))
    payload = AudioInput(audio_data=base64.b64encode(pcm.tobytes()).decode(), sample_rate=16_000)
    monkeypatch.setattr(recognizer, "_candidate_regions", lambda _: [
        (0, 16_000, "speech"), (16_000, 32_000, "speech"),
    ])
    monkeypatch.setattr(recognizer, "_detect_multiple_speakers", lambda *_: [])
    result = recognizer.recognize_detailed(payload, threshold=0.8, min_margin=0.2)
    assert result.outcome == "matched"
    assert result.margin > 0.2


def test_detects_multiple_known_speakers_in_separate_regions(
    tmp_path, fake_factory, identity_preprocess
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    speaker_a = recognizer.enroll(
        "Testspreker A",
        [audio(12000)],
        person_entity_id="person.test_speaker_a",
    )
    speaker_b = recognizer.enroll(
        "Testspreker B",
        [audio(-12000)],
        person_entity_id="person.test_speaker_b",
    )

    detailed = recognizer.recognize_detailed(
        mixed_speakers_audio(), threshold=0.8, min_margin=0.1
    )

    assert detailed.outcome == "multiple_speakers"
    assert detailed.speaker is None
    assert detailed.best_segment is None
    assert [item["speaker_id"] for item in detailed.detected_speakers] == [
        speaker_a.id,
        speaker_b.id,
    ]
    assert [item["speaker_name"] for item in detailed.detected_speakers] == [
        "Testspreker A",
        "Testspreker B",
    ]
    assert detailed.detected_speakers[0]["best_segment"] == {
        "start_seconds": 0.0,
        "end_seconds": 1.0,
    }
    assert detailed.detected_speakers[1]["best_segment"] == {
        "start_seconds": 1.5,
        "end_seconds": 2.5,
    }


def test_enroll_append_recognize_delete_and_reload(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    alice = recognizer.enroll("Alice", [audio(12000), audio(8000)])
    bob = recognizer.enroll("Bob", [audio(-12000)])
    assert alice.sample_count == 2
    assert bob.sample_count == 1
    assert recognizer.enroll("alice", [audio(10000)]).sample_count == 3

    matched, confidence, scores = recognizer.recognize(audio(9000))
    assert matched is not None and matched.id == alice.id
    assert confidence > 0.99
    assert set(scores) == {"alice", "Bob"}

    restarted = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    assert [item.sample_count for item in restarted.list_speakers()] == [3, 1]
    assert restarted.delete(bob.id)
    assert not restarted.delete("missing")
    assert [item.name for item in restarted.list_speakers()] == ["alice"]


def test_replace_resets_sample_count(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    recognizer.enroll("Alice", [audio(12000), audio(12000)])
    replaced = recognizer.enroll("Alice", [audio(-12000)], replace=True)
    assert replaced.sample_count == 1
    matched, _, _ = recognizer.recognize(audio(-12000))
    assert matched is not None and matched.name == "Alice"


def test_person_mapping_is_optional_and_persists(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    alice = recognizer.enroll(
        "Alice", [audio(12000)], person_entity_id="person.alice"
    )
    assert alice.person_entity_id == "person.alice"

    appended = recognizer.enroll("Alice", [audio(10000)])
    assert appended.person_entity_id == "person.alice"

    restarted = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    assert restarted.list_speakers()[0].person_entity_id == "person.alice"


def test_person_mapping_can_be_cleared_explicitly(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    recognizer.enroll("Alice", [audio(12000)], person_entity_id="person.alice")

    cleared = recognizer.enroll(
        "Alice",
        [audio(10000)],
        person_entity_id=None,
        update_person_mapping=True,
    )

    assert cleared.person_entity_id is None


@pytest.mark.parametrize("payload", ["not base64!", base64.b64encode(b"x").decode(), ""])
def test_rejects_invalid_pcm(tmp_path, fake_factory, identity_preprocess, payload):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    from app.models import AudioInput
    with pytest.raises((ValueError, Exception)):
        recognizer.enroll("Alice", [AudioInput(audio_data=payload, sample_rate=16000)])


def test_rejects_silence_and_oversized_audio(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    with pytest.raises(ValueError, match="silent"):
        recognizer.enroll("Alice", [audio(0)])
    with pytest.raises(ValueError, match="exceeds"):
        recognizer.enroll("Alice", [audio(1000, seconds=11)])


def test_name_does_not_become_filename(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    profile = recognizer.enroll("../../Testspreker <script>", [audio(1000)])
    files = list((tmp_path / "speakers").glob("*.npy"))
    assert len(files) == 1 and files[0].stem == profile.id


def test_registry_failure_rolls_back_enrollment(tmp_path, fake_factory, identity_preprocess, monkeypatch):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    alice = recognizer.enroll("Alice", [audio(12000)])
    original_embedding = recognizer._embeddings[alice.id].copy()

    monkeypatch.setattr(
        recognizer, "_write_registry", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        recognizer.enroll("Alice", [audio(-12000)], replace=True)

    assert recognizer.list_speakers()[0].sample_count == 1
    np.testing.assert_array_equal(recognizer._embeddings[alice.id], original_embedding)


def test_corrupt_profile_does_not_hide_other_profiles(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    alice = recognizer.enroll("Alice", [audio(12000)])
    bob = recognizer.enroll("Bob", [audio(-12000)])
    (tmp_path / "speakers" / f"{bob.id}.npy").write_bytes(b"corrupt")

    restarted = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    assert [profile.id for profile in restarted.list_speakers()] == [alice.id]


def test_profile_mean_is_independent_of_enrollment_order(tmp_path, fake_factory, identity_preprocess):
    first = make_recognizer(tmp_path / "first", fake_factory, identity_preprocess)
    first_profile = first.enroll("Alice", [audio(12000), audio(-12000), audio(6000)])
    second = make_recognizer(tmp_path / "second", fake_factory, identity_preprocess)
    second_profile = second.enroll("Alice", [audio(6000)])
    second.enroll("Alice", [audio(-12000)])
    second_profile = second.enroll("Alice", [audio(12000)])

    np.testing.assert_allclose(
        first._embeddings[first_profile.id], second._embeddings[second_profile.id],
        rtol=0, atol=1e-7,
    )
    assert len(first.catalog.list_samples(first_profile.id, active_only=True)) == 3
    assert len(second.catalog.list_samples(second_profile.id, active_only=True)) == 3
    assert all(
        item["metadata"]["embedding_model"] == "resemblyzer-v1"
        and item["metadata"]["preprocess_version"] == "resemblyzer-preprocess-v1"
        for item in first.catalog.list_samples(first_profile.id, active_only=True, include_internal=True)
    )


def test_failed_first_profile_commit_removes_staged_sample_audio(
    tmp_path, fake_factory, identity_preprocess, monkeypatch
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    monkeypatch.setattr(
        recognizer, "_write_registry", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        recognizer.enroll("Alice", [audio(12000)])

    assert recognizer.list_speakers() == []
    assert list((tmp_path / "enrollment").rglob("*.wav")) == []


def test_sample_vectors_are_private_and_unactivated_revision_recovers(
    tmp_path, fake_factory, identity_preprocess
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    profile = recognizer.enroll("Alice", [audio(12000)])
    sample = recognizer.catalog.list_samples(profile.id)[0]
    assert "embedding" not in sample["metadata"]
    assert "legacy_embedding" not in sample["metadata"]
    internal = recognizer.catalog.get_sample(sample["id"], include_internal=True)
    assert internal["metadata"]["embedding"]

    recognizer.catalog.replace_active_samples(profile.id, [])
    restarted = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    assert len(restarted.catalog.list_samples(profile.id, active_only=True)) == 1
    assert restarted.list_speakers()[0].sample_count == 1


def test_profile_revision_identity_is_stable_and_tracks_active_sample_set(
    tmp_path, fake_factory, identity_preprocess
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    profile = recognizer.enroll("Alice", [audio(12000), audio(-12000)])
    first = recognizer.profile_revision_snapshot(profile.id, threshold=0.81, margin=0.12)
    restarted = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    after_restart = restarted.profile_revision_snapshot(profile.id, threshold=0.81, margin=0.12)
    assert first["revision_id"] == after_restart["revision_id"]
    assert first["sample_ids"] == after_restart["sample_ids"]

    sample = restarted.catalog.list_samples(profile.id, active_only=True)[0]
    restarted.catalog.set_sample_active(sample["id"], False)
    changed = restarted.profile_revision_snapshot(profile.id, threshold=0.81, margin=0.12)
    assert changed["revision_id"] != first["revision_id"]


def test_parallel_sample_deactivation_keeps_one_active_profile_sample(
    tmp_path, fake_factory, identity_preprocess,
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    profile = recognizer.enroll("Alice", [audio(12000), audio(-12000)])
    sample_ids = [item["id"] for item in recognizer.catalog.list_samples(profile.id)]

    def deactivate(sample_id):
        try:
            recognizer.set_sample_active_and_retrain(profile.id, sample_id, False)
            return "ok"
        except ValueError:
            return "last_active"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deactivate, sample_ids))
    assert sorted(results) == ["last_active", "ok"]
    assert len(recognizer.catalog.list_samples(profile.id, active_only=True)) == 1
    assert recognizer.list_speakers()[0].sample_count == 1


def test_recognition_with_snapshot_captures_all_profiles_for_unmatched_result(
    tmp_path, fake_factory, identity_preprocess
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    alice = recognizer.enroll("Alice", [audio(12000)])
    bob = recognizer.enroll("Bob", [audio(-12000)])

    detailed, snapshot = recognizer.recognize_detailed_with_snapshot(
        audio(12000), threshold=1.1, min_margin=0.25
    )

    assert detailed.outcome == "unmatched"
    assert snapshot["threshold"] == 1.1
    assert snapshot["margin"] == 0.25
    assert {item["speaker_id"] for item in snapshot["profile_revisions"]} == {
        alice.id, bob.id
    }
    changed_threshold = recognizer.recognize_detailed_with_snapshot(
        audio(12000), threshold=1.05, min_margin=0.25
    )[1]
    assert changed_threshold["revision_id"] != snapshot["revision_id"]
