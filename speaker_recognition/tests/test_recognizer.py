from __future__ import annotations

import base64

import numpy as np
import pytest

from app.recognizer import SpeakerRecognizer
from conftest import audio


def make_recognizer(tmp_path, fake_factory, identity_preprocess):
    recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, fake_factory, identity_preprocess)
    recognizer.initialize()
    return recognizer


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
    profile = recognizer.enroll("../../Tim <script>", [audio(1000)])
    files = list((tmp_path / "speakers").glob("*.npy"))
    assert len(files) == 1 and files[0].stem == profile.id
