from __future__ import annotations

import sqlite3
import wave
from datetime import datetime, timedelta, timezone

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
        labels={"person_entity_id": "person.tim"},
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
            "person_entity_id": "person.tim",
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
        "person_entity_id": "person.tim",
        "audio_variant": "original",
    }
