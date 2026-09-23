from __future__ import annotations

import sqlite3
import wave
from datetime import datetime, timedelta, timezone

import pytest

from app.storage import AudioCatalog


def test_v20_catalogue_migrates_without_losing_recordings(tmp_path):
    analysis_dir = tmp_path / "analysis" / ("a" * 32)
    analysis_dir.mkdir(parents=True)
    original = analysis_dir / "original.wav"
    with wave.open(str(original), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x01\x00" * 16_000)

    now = datetime.now(timezone.utc).isoformat()
    database = tmp_path / "audio_catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE recordings (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              source TEXT NOT NULL, satellite_id TEXT, stt_entity_id TEXT,
              transcript TEXT, outcome TEXT NOT NULL DEFAULT 'pending', speaker_id TEXT,
              speaker_name TEXT, confidence REAL, threshold REAL, margin REAL,
              scores_json TEXT NOT NULL DEFAULT '{}', segments_json TEXT NOT NULL DEFAULT '[]',
              timings_json TEXT NOT NULL DEFAULT '{}', extraction_mode TEXT NOT NULL DEFAULT 'off',
              extraction_status TEXT, conversation_forwarded INTEGER,
              original_path TEXT NOT NULL, extracted_path TEXT,
              duration_seconds REAL NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
              labels_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recordings (
              id, created_at, updated_at, source, original_path, duration_seconds, bytes
            ) VALUES (?, ?, ?, 'pipeline', ?, 1, ?)
            """,
            ("a" * 32, now, now, str(original), original.stat().st_size),
        )

    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    migrated = catalogue.get_recording("a" * 32)

    assert migrated is not None
    assert migrated["original_path"] == str(original)
    assert migrated["denoised_path"] is None
    assert migrated["isolated_path"] is None
    assert migrated["processing_status"] == "idle"
    assert migrated["processing_backend"] is None
    assert migrated["processing_stages"] == {}
    assert migrated["processing_quality"] == {}
    assert migrated["processing_timings"] == {}
    assert migrated["audio_retained"] == 1
    assert migrated["profile_revision"] == {}


def test_retention_removes_analysis_variants_but_keeps_enrollment_audio(tmp_path):
    catalogue = AudioCatalog(tmp_path, retention_days=7)
    catalogue.initialize()
    recording = catalogue.create_recording(
        b"\x01\x00" * 16_000, 16_000, source="test"
    )
    catalogue.save_audio_variant(
        recording["id"], "denoised", b"\x02\x00" * 16_000, 16_000
    )
    sample = catalogue.add_sample(
        "speaker-id", b"\x03\x00" * 16_000, 16_000
    )
    sample_path = catalogue.sample_path(sample["id"])

    removed = catalogue.cleanup(
        now=datetime.now(timezone.utc) + timedelta(days=8)
    )

    assert removed == 1
    assert catalogue.get_recording(recording["id"]) is None
    assert sample_path is not None and sample_path.is_file()


def test_reset_processing_preserves_source_and_recognition_metadata(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    recording = catalogue.create_recording(
        b"\x01\x00" * 16_000,
        16_000,
        source="test",
        transcript="bewaar mij",
        outcome="matched",
        speaker_id="speaker-id",
        confidence=0.91,
        timings={"stt_ms": 80, "total_ms": 100},
        labels={"person_entity_id": "person.test_user"},
    )
    catalogue.save_audio_variant(
        recording["id"], "denoised", b"\x02\x00" * 16_000, 16_000
    )
    catalogue.update_recording(
        recording["id"],
        processing_status="complete",
        processing_backend="df3_streaming",
        processing_stages={"streaming": "drained"},
        processing_quality={"stateful": True},
        processing_timings={
            "audio_processing_ms": 25,
            "post_utterance_ms": 8,
        },
        labels={
            "person_entity_id": "person.test_user",
            "audio_variant": "denoised",
            "fallback": False,
            "quality": {"stateful": True},
        },
    )
    denoised = catalogue.audio_path(recording["id"], "denoised")

    reset = catalogue.reset_processing(recording["id"])
    repeated = catalogue.reset_processing(recording["id"])

    assert denoised is not None and not denoised.exists()
    assert reset is not None and repeated is not None
    assert reset["transcript"] == "bewaar mij"
    assert reset["speaker_id"] == "speaker-id"
    assert reset["confidence"] == 0.91
    assert reset["timings"] == {"stt_ms": 80, "total_ms": 100}
    assert reset["processing_timings"] == {}
    assert reset["processing_backend"] is None
    assert reset["processing_status"] == "idle"
    assert reset["labels"] == {
        "person_entity_id": "person.test_user",
        "audio_variant": "original",
    }


def test_sample_revision_is_atomic_and_calibration_is_invalidated(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    one = catalogue.add_sample("speaker", b"\x01\x00" * 1600, 16000)
    two = catalogue.add_sample("speaker", b"\x02\x00" * 1600, 16000, active=False)
    catalogue.set_calibration(0.8, 0.1, {"source": "test"})

    previous = catalogue.replace_active_samples("speaker", [two["id"]])

    assert previous == [one["id"]]
    assert [sample["id"] for sample in catalogue.list_samples("speaker", True)] == [two["id"]]
    assert catalogue.calibration() is None


def test_last_active_sample_cannot_be_deactivated_or_deleted(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    sample = catalogue.add_sample("speaker", b"\x01\x00" * 1600, 16000)

    try:
        catalogue.set_sample_active(sample["id"], False)
        assert False, "expected last active sample guard"
    except ValueError as error:
        assert "at least one" in str(error)
    try:
        catalogue.delete_sample(sample["id"])
        assert False, "expected last active sample guard"
    except ValueError as error:
        assert "at least one" in str(error)

    assert catalogue.get_sample(sample["id"])["active"] is True
    assert catalogue.sample_path(sample["id"]).is_file()


def test_recording_can_keep_metadata_without_audio_and_restore_after_decision(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    pcm = b"\x01\x00" * 16000
    recording = catalogue.create_recording(
        pcm, 16000, retain_audio=False, transcript="hou lampen aan", outcome="matched"
    )

    assert recording["duration_seconds"] == 1
    assert recording["transcript"] == "hou lampen aan"
    assert recording["audio_retained"] == 0
    assert recording["original_path"] == ""
    assert catalogue.audio_path(recording["id"], "original") is None
    assert catalogue.save_audio_variant(recording["id"], "denoised", pcm, 16000)["denoised_path"] is None
    assert catalogue.storage_usage() == 0

    retained = catalogue.save_original_audio(recording["id"], pcm, 16000)
    assert retained["audio_retained"] == 1
    assert catalogue.audio_path(recording["id"], "original").is_file()

    catalogue.save_audio_variant(recording["id"], "denoised", pcm, 16000)
    removed = catalogue.remove_analysis_audio(recording["id"])
    assert removed["transcript"] == "hou lampen aan"
    assert removed["audio_retained"] == 0
    assert catalogue.audio_path(recording["id"], "original") is None
    assert catalogue.audio_path(recording["id"], "denoised") is None


def test_orphan_scan_reports_unknown_wav_without_deleting_it(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    unknown = catalogue.analysis_dir / "unindexed" / "mystery.wav"
    catalogue._write_wav(unknown, b"\x01\x00" * 1600, 16000)

    inventory = catalogue.scan_orphans()
    assert str(unknown.resolve()) in inventory["unindexed_wav_files"]
    assert unknown.is_file()


def test_audio_removal_refuses_to_unlink_database_path_outside_analysis_root(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    pcm = b"\x01\x00" * 1600
    recording = catalogue.create_recording(pcm, 16000)
    outside = tmp_path / "keep.wav"
    catalogue._write_wav(outside, pcm, 16000)
    with sqlite3.connect(catalogue.db_path) as connection:
        connection.execute(
            "UPDATE recordings SET original_path=? WHERE id=?",
            (str(outside), recording["id"]),
        )

    catalogue.remove_analysis_audio(recording["id"])

    assert outside.is_file()
    assert catalogue.get_recording(recording["id"])["original_path"] == ""


def test_recognition_run_keeps_an_immutable_profile_revision_snapshot(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    recording = catalogue.create_recording(b"\x01\x00" * 1600, 16000, retain_audio=False)
    snapshot = {
        "revision_id": "rev-123",
        "speaker_id": "speaker-1",
        "sample_ids": ["sample-a", "sample-b"],
        "threshold": 0.82,
        "margin": 0.15,
    }

    run = catalogue.record_recognition_run(
        recording["id"], snapshot, {"outcome": "matched", "confidence": 0.91}
    )

    assert catalogue.recognition_run_profile_revision(run["id"]) == snapshot
    assert catalogue.list_recognition_runs(recording["id"])[0]["details"]["outcome"] == "matched"
    assert catalogue.recording_profile_revision(recording["id"]) is None
    next_snapshot = {**snapshot, "revision_id": "rev-456", "threshold": 0.84}
    second_run = catalogue.record_recognition_run(recording["id"], next_snapshot, {"outcome": "unmatched"})
    assert {item["profile_revision"]["revision_id"] for item in catalogue.list_recognition_runs(recording["id"])} == {"rev-123", "rev-456"}
    assert catalogue.recognition_run_profile_revision(run["id"]) == snapshot
    assert catalogue.delete_recording(recording["id"])
    assert catalogue.get_recognition_run(run["id"]) is None
    assert catalogue.get_recognition_run(second_run["id"]) is None


def test_storage_breakdown_reports_real_bytes_and_private_counts(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    pcm = b"\x01\x00" * 1600
    recording = catalogue.create_recording(pcm, 16000)
    catalogue.save_audio_variant(recording["id"], "denoised", pcm, 16000)
    catalogue.add_sample("speaker-a", pcm, 16000)
    archived = catalogue.add_sample("speaker-a", pcm, 16000)
    catalogue.set_sample_active(archived["id"], False)
    orphan = catalogue.analysis_dir / "lost" / "orphan.wav"
    catalogue._write_wav(orphan, pcm, 16000)

    report = catalogue.storage_breakdown()

    assert report["analysis_original_bytes"] == catalogue.audio_path(recording["id"], "original").stat().st_size
    assert report["analysis_derived_bytes"] == catalogue.audio_path(recording["id"], "denoised").stat().st_size
    assert report["active_enrollment_bytes"] == catalogue.sample_path(catalogue.list_samples("speaker-a", True)[0]["id"]).stat().st_size
    assert report["archived_enrollment_bytes"] == catalogue.sample_path(archived["id"]).stat().st_size
    assert report["unindexed_wav_bytes"] == orphan.stat().st_size
    assert report["recordings_count"] == 1
    assert report["active_speakers_count"] == 1
    assert report["archived_samples_count"] == 1
    assert not any("name" in key or "path" in key for key in report)


def test_archived_sample_management_deletes_only_indexed_enrollment_audio(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    active = catalogue.add_sample("speaker-a", b"\x01\x00" * 1600, 16000)
    archived = catalogue.add_sample("speaker-a", b"\x02\x00" * 1600, 16000)
    catalogue.set_sample_active(archived["id"], False)
    archived_path = catalogue.sample_path(archived["id"])

    assert [item["id"] for item in catalogue.list_archived_samples()] == [archived["id"]]
    assert catalogue.delete_archived_sample(archived["id"])
    assert catalogue.get_sample(archived["id"]) is None
    assert archived_path is not None and not archived_path.exists()
    with pytest.raises(ValueError, match="archived"):
        catalogue.delete_archived_sample(active["id"])


def test_archived_sample_delete_refuses_paths_outside_enrollment_root(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    catalogue.add_sample("speaker-a", b"\x01\x00" * 1600, 16000)
    archived = catalogue.add_sample("speaker-a", b"\x02\x00" * 1600, 16000)
    catalogue.set_sample_active(archived["id"], False)
    outside = tmp_path / "protected.wav"
    catalogue._write_wav(outside, b"\x03\x00" * 1600, 16000)
    with sqlite3.connect(catalogue.db_path) as connection:
        connection.execute(
            "UPDATE enrollment_samples SET path=? WHERE id=?",
            (str(outside), archived["id"]),
        )

    with pytest.raises(ValueError, match="outside enrollment"):
        catalogue.delete_archived_sample(archived["id"])
    assert outside.is_file()
    assert catalogue.get_sample(archived["id"]) is not None


def test_orphan_cleanup_only_deletes_selected_unindexed_wav(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    indexed = catalogue.create_recording(b"\x01\x00" * 1600, 16000)
    orphan = catalogue.analysis_dir / "unindexed.wav"
    catalogue._write_wav(orphan, b"\x02\x00" * 1600, 16000)
    outside = tmp_path / "outside.wav"
    catalogue._write_wav(outside, b"\x03\x00" * 1600, 16000)
    with pytest.raises(ValueError):
        catalogue.delete_unindexed_wav_files([str(orphan), str(outside)])
    assert orphan.is_file()
    assert catalogue.delete_unindexed_wav_files([str(orphan)]) == 1
    assert not orphan.exists()
    assert catalogue.audio_path(indexed["id"], "original") is not None


def test_retention_reconciliation_removes_interrupted_temporary_audio(tmp_path):
    catalogue = AudioCatalog(tmp_path)
    catalogue.initialize()
    matched = catalogue.create_recording(
        b"\x01\x00" * 1600, 16000, outcome="matched",
        labels={"retention_pending": True, "retention_policy": "errors"},
    )
    catalogue.update_recording(matched["id"], processing_status="complete")
    failed = catalogue.create_recording(
        b"\x02\x00" * 1600, 16000, outcome="error",
        labels={"retention_pending": True, "retention_policy": "errors"},
    )
    historical = catalogue.create_recording(b"\x03\x00" * 1600, 16000, outcome="matched")
    interrupted = catalogue.create_recording(
        b"\x04\x00" * 1600, 16000, outcome="matched",
        labels={"retention_pending": True, "retention_policy": "errors"},
    )
    catalogue.update_recording(interrupted["id"], processing_status="running")
    assert catalogue.reconcile_audio_retention("errors") == 1
    assert catalogue.audio_path(matched["id"], "original") is None
    assert catalogue.audio_path(failed["id"], "original") is not None
    assert catalogue.audio_path(interrupted["id"], "original") is not None
    assert catalogue.get_recording(interrupted["id"])["processing_status"] == "failed"
    catalogue.update_recording(
        failed["id"], labels={"retention_pending": True, "retention_policy": "none"},
    )
    assert catalogue.reconcile_audio_retention("all") == 1
    assert catalogue.audio_path(failed["id"], "original") is None
    assert catalogue.audio_path(historical["id"], "original") is not None
