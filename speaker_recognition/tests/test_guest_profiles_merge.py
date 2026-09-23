from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import EnrollmentRequest, VoiceSample, AudioInput
from app.diarization import TimelineStore
from app.recognizer import SpeakerRecognizer
from conftest import audio


def make_recognizer(tmp_path, fake_factory, identity_preprocess):
    result = SpeakerRecognizer(tmp_path, 0.8, 10, fake_factory, identity_preprocess)
    result.initialize()
    return result


def test_guest_default_and_never_expiry_are_distinct(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    guest = recognizer.enroll("Temporary", [audio(10000)], profile_kind="guest")
    never = recognizer.enroll(
        "Visitor", [audio(-10000)], profile_kind="guest", expiry_was_explicit=True
    )
    assert guest.expires_at is not None
    assert timedelta(days=29) < guest.expires_at - datetime.now(timezone.utc) < timedelta(days=31)
    assert never.expires_at is None
    assert guest.person_entity_id is None


def test_default_enrollment_does_not_convert_or_extend_existing_guest(
    tmp_path, fake_factory, identity_preprocess
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    deadline = datetime.now(timezone.utc) + timedelta(days=4)
    guest = recognizer.enroll(
        "Guest", [audio(10000)], profile_kind="guest", expires_at=deadline,
        profile_kind_explicit=True, expiry_was_explicit=True,
    )
    updated = recognizer.enroll("Guest", [audio(9000)])
    assert updated.profile_kind == "guest"
    assert updated.expires_at == deadline


def test_merge_uses_active_wavs_and_keeps_target_identity(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    target = recognizer.enroll("Kept name", [audio(10000)], person_entity_id="person.kept")
    source = recognizer.enroll("Old name", [audio(-10000)])
    historical = recognizer.catalog.create_recording(
        b"\0\0" * 16000, 16000, speaker_id=source.id, speaker_name=source.name
    )

    merged = recognizer.merge_profiles(source.id, target.id)

    assert merged.id == target.id
    assert merged.name == target.name
    assert merged.person_entity_id == target.person_entity_id
    assert merged.sample_count == 2
    assert [sample["speaker_id"] for sample in recognizer.catalog.list_samples(target.id)] == [target.id, target.id]
    assert recognizer.catalog.get_recording(historical["id"])["speaker_id"] == source.id
    assert source.id not in {profile.id for profile in recognizer.list_speakers()}


def test_merge_refuses_missing_active_wav_without_mutating_profiles(tmp_path, fake_factory, identity_preprocess):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    target = recognizer.enroll("Target", [audio(10000)])
    source = recognizer.enroll("Source", [audio(-10000)])
    source_sample = recognizer.catalog.list_samples(source.id, active_only=True)[0]
    recognizer.catalog.sample_path(source_sample["id"]).unlink()

    with pytest.raises(ValueError, match="WAV is missing"):
        recognizer.merge_profiles(source.id, target.id)

    assert {profile.id for profile in recognizer.list_speakers()} == {source.id, target.id}
    assert recognizer.catalog.list_samples(source.id, active_only=True)


def test_merge_rolls_back_sample_ownership_when_registry_commit_fails(
    tmp_path, fake_factory, identity_preprocess, monkeypatch
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    target = recognizer.enroll("Target", [audio(10000)])
    source = recognizer.enroll("Source", [audio(-10000)])
    write_registry = recognizer._write_registry
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated registry failure")
        write_registry()

    monkeypatch.setattr(recognizer, "_write_registry", fail_once)
    with pytest.raises(OSError, match="simulated registry failure"):
        recognizer.merge_profiles(source.id, target.id)

    assert {profile.id for profile in recognizer.list_speakers()} == {source.id, target.id}
    assert len(recognizer.catalog.list_samples(source.id, active_only=True)) == 1
    assert len(recognizer.catalog.list_samples(target.id, active_only=True)) == 1


def test_startup_recovers_merge_interrupted_after_sample_move(
    tmp_path, fake_factory, identity_preprocess, monkeypatch
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    target = recognizer.enroll("Target", [audio(10000)])
    source = recognizer.enroll("Source", [audio(-10000)])
    original_move = recognizer.catalog.move_samples

    def move_then_crash(source_id, target_id, sample_ids=None):
        original_move(source_id, target_id, sample_ids)
        raise SystemExit("simulated process termination")

    monkeypatch.setattr(recognizer.catalog, "move_samples", move_then_crash)
    with pytest.raises(SystemExit, match="simulated process termination"):
        recognizer.merge_profiles(source.id, target.id)

    restarted = make_recognizer(tmp_path, fake_factory, identity_preprocess)

    assert {profile.id for profile in restarted.list_speakers()} == {source.id, target.id}
    assert len(restarted.catalog.list_samples(source.id, active_only=True)) == 1
    assert len(restarted.catalog.list_samples(target.id, active_only=True)) == 1
    assert restarted.catalog.get_setting("speaker_profile_merge_journal") is None


def test_expiry_removes_guest_audio_and_scrubs_history_but_keeps_analysis_audio(
    tmp_path, fake_factory, identity_preprocess
):
    recognizer = make_recognizer(tmp_path, fake_factory, identity_preprocess)
    expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    guest = recognizer.enroll("Guest name", [audio(10000)], profile_kind="guest", expires_at=expires)
    sample_path = recognizer.catalog.sample_path(
        recognizer.catalog.list_samples(guest.id)[0]["id"]
    )
    recording = recognizer.catalog.create_recording(
        b"\0\0" * 16000, 16000, speaker_id=guest.id, speaker_name=guest.name
    )
    analysis_path = recognizer.catalog.audio_path(recording["id"], "original")
    timeline = TimelineStore(recognizer.catalog)
    timeline.set(recording["id"], "complete", {"segments": [{"speaker_id": guest.id}]})

    assert recognizer.expire_guest_profiles() == [guest.id]
    assert sample_path is not None and not sample_path.exists()
    assert analysis_path is not None and analysis_path.exists()
    history = recognizer.catalog.get_recording(recording["id"])
    assert history["speaker_id"] is None
    assert history["speaker_name"] is None
    assert timeline.get(recording["id"])["segments"][0]["speaker_id"] is None


def test_guest_cannot_have_home_assistant_person():
    with pytest.raises(ValueError, match="Guest profiles"):
        EnrollmentRequest(
            speaker_name="Guest",
            samples=[VoiceSample(audio=AudioInput(audio_data="AA=="))],
            profile_kind="guest",
            person_entity_id="person.someone",
        )
