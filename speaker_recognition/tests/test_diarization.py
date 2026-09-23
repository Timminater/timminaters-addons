from __future__ import annotations

import base64

import numpy as np

from app.diarization import TimelineStore, analyze_timeline
from app.models import AudioInput
from app.recognizer import SpeakerRecognizer
from conftest import audio


def test_offline_timeline_keeps_uncertain_windows_unknown(tmp_path, fake_factory, identity_preprocess):
    recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, fake_factory, identity_preprocess)
    recognizer.initialize()
    recognizer.enroll("Alice", [audio(12000)])
    recognizer.enroll("Bob", [audio(-12000)])
    pcm = np.concatenate((
        np.full(16000, 12000, dtype="<i2"),
        np.zeros(16000, dtype="<i2"),
        np.full(16000, -12000, dtype="<i2"),
    ))
    sample = AudioInput(audio_data=base64.b64encode(pcm.tobytes()).decode(), sample_rate=16000)
    result = analyze_timeline(recognizer, sample, threshold=0.8, margin=0.1)
    assert result["experimental"] is True
    assert len(result["segments"]) == 2
    assert result["segments"][0]["speaker_id"] != result["segments"][1]["speaker_id"]


def test_timeline_store_recovers_interrupted_job_and_cascades_delete(tmp_path):
    from app.storage import AudioCatalog

    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    recording = catalogue.create_recording(b"\x01\x00" * 16000, 16000)
    store = TimelineStore(catalogue)
    store.set(recording["id"], "running")
    restarted = TimelineStore(catalogue)
    assert restarted.get(recording["id"])["status"] == "failed"
    catalogue.delete_recording(recording["id"])
    assert restarted.get(recording["id"]) is None


def test_timeline_does_not_name_a_window_with_two_different_voices(
    tmp_path, fake_factory, identity_preprocess,
):
    recognizer = SpeakerRecognizer(tmp_path, 0.8, 10, fake_factory, identity_preprocess)
    recognizer.initialize()
    recognizer.enroll("Alice", [audio(12000)])
    recognizer.enroll("Bob", [audio(-12000)])
    pcm = np.concatenate((
        np.full(8000, 12000, dtype="<i2"),
        np.full(8000, -12000, dtype="<i2"),
    ))
    sample = AudioInput(audio_data=base64.b64encode(pcm.tobytes()).decode(), sample_rate=16000)
    result = analyze_timeline(recognizer, sample, threshold=0.8, margin=0.1)
    assert result["segments"][0]["speaker_id"] is None
