from __future__ import annotations

import base64

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
