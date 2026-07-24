"""Durable audio catalogue used by the analysis and enrollment UI.

The catalogue deliberately keeps the database as an index only.  WAV files are
written atomically next to it so a corrupt/incomplete database row can never
point at a partially written recording.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


GIB = 1024 * 1024 * 1024


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


class AudioCatalog:
    """SQLite metadata and safe paths for transient recordings and samples."""

    def __init__(self, data_dir: Path, retention_days: int = 7, max_storage_bytes: int = 2 * GIB) -> None:
        self.data_dir = Path(data_dir)
        self.analysis_dir = self.data_dir / "analysis"
        self.enrollment_dir = self.data_dir / "enrollment"
        self.db_path = self.data_dir / "audio_catalog.sqlite3"
        self.retention_days = retention_days
        self.max_storage_bytes = max_storage_bytes
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.analysis_dir.mkdir(parents=True, exist_ok=True)
            self.enrollment_dir.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS recordings (
                      id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                      source TEXT NOT NULL, satellite_id TEXT, stt_entity_id TEXT,
                      transcript TEXT, outcome TEXT NOT NULL DEFAULT 'pending', speaker_id TEXT,
                      speaker_name TEXT, confidence REAL, threshold REAL, margin REAL,
                      scores_json TEXT NOT NULL DEFAULT '{}', segments_json TEXT NOT NULL DEFAULT '[]',
                      timings_json TEXT NOT NULL DEFAULT '{}', extraction_mode TEXT NOT NULL DEFAULT 'off',
                      extraction_status TEXT, conversation_forwarded INTEGER,
                      original_path TEXT NOT NULL, extracted_path TEXT, duration_seconds REAL NOT NULL DEFAULT 0,
                      bytes INTEGER NOT NULL DEFAULT 0, labels_json TEXT NOT NULL DEFAULT '{}',
                      denoised_path TEXT, isolated_path TEXT,
                      processing_status TEXT NOT NULL DEFAULT 'idle', processing_speaker_id TEXT,
                      processing_backend TEXT,
                      processing_stages_json TEXT NOT NULL DEFAULT '{}', processing_quality_json TEXT NOT NULL DEFAULT '{}',
                      processing_fallback_reason TEXT,
                      processing_timings_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS recordings_created ON recordings(created_at DESC);
                    CREATE INDEX IF NOT EXISTS recordings_outcome ON recordings(outcome);
                    CREATE TABLE IF NOT EXISTS enrollment_samples (
                      id TEXT PRIMARY KEY, speaker_id TEXT, created_at TEXT NOT NULL,
                      active INTEGER NOT NULL DEFAULT 1, path TEXT NOT NULL UNIQUE,
                      duration_seconds REAL NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                      source_recording_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS samples_speaker ON enrollment_samples(speaker_id, active);
                    CREATE TABLE IF NOT EXISTS calibration (
                      id INTEGER PRIMARY KEY CHECK (id = 1), threshold REAL, margin REAL,
                      updated_at TEXT, details_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS catalogue_settings (
                      key TEXT PRIMARY KEY, value_json TEXT NOT NULL
                    );
                    """
                )
                # The old extracted_path is a VAD splice. Preserve it as
                # legacy data; migrations only append nullable columns.
                existing = {row["name"] for row in db.execute("PRAGMA table_info(recordings)")}
                migrations = {
                    "denoised_path": "TEXT", "isolated_path": "TEXT",
                    "processing_status": "TEXT NOT NULL DEFAULT 'idle'",
                    "processing_speaker_id": "TEXT",
                    "processing_backend": "TEXT",
                    "processing_stages_json": "TEXT NOT NULL DEFAULT '{}'",
                    "processing_quality_json": "TEXT NOT NULL DEFAULT '{}'",
                    "processing_fallback_reason": "TEXT",
                    "processing_timings_json": "TEXT NOT NULL DEFAULT '{}'",
                }
                for column, definition in migrations.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE recordings ADD COLUMN {column} {definition}")
            self._initialized = True
            self.cleanup()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _safe_id(value: str) -> str:
        if not value or any(char not in "0123456789abcdef" for char in value.lower()):
            raise ValueError("Invalid recording identifier")
        return value

    @staticmethod
    def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
        # wave needs a real seekable stream.  Write to a temporary sibling using
        # _write_wav instead in normal paths; this helper is intentionally absent.
        raise AssertionError("Use _write_wav")

    @staticmethod
    def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
        if sample_rate < 8000 or sample_rate > 48000 or not pcm or len(pcm) % 2:
            raise ValueError("Audio must contain signed 16-bit mono PCM")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def create_recording(self, pcm: bytes, sample_rate: int, *, source: str = "pipeline", **metadata: Any) -> dict[str, Any]:
        self.initialize()
        recording_id = uuid.uuid4().hex
        target = self.analysis_dir / recording_id / "original.wav"
        self._write_wav(target, pcm, sample_rate)
        duration = len(pcm) / (2 * sample_rate)
        now = _iso()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO recordings (id,created_at,updated_at,source,satellite_id,stt_entity_id,
                   transcript,outcome,speaker_id,speaker_name,confidence,threshold,margin,scores_json,
                   segments_json,timings_json,extraction_mode,extraction_status,conversation_forwarded,
                   original_path,duration_seconds,bytes,labels_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (recording_id, now, now, source, metadata.get("satellite_id"), metadata.get("stt_entity_id"),
                 metadata.get("transcript"), metadata.get("outcome", "pending"), metadata.get("speaker_id"),
                 metadata.get("speaker_name"), metadata.get("confidence"), metadata.get("threshold"),
                 metadata.get("margin"), json.dumps(metadata.get("scores", {})), json.dumps(metadata.get("segments", [])),
                 json.dumps(metadata.get("timings", {})), metadata.get("extraction_mode", "off"),
                 metadata.get("extraction_status"), metadata.get("conversation_forwarded"), str(target), duration,
                 target.stat().st_size, json.dumps(metadata.get("labels", {}))),
            )
        return self.get_recording(recording_id) or {"id": recording_id}

    def update_recording(self, recording_id: str, **changes: Any) -> dict[str, Any] | None:
        if not changes:
            return self.get_recording(recording_id)
        columns = {
            "source", "satellite_id", "stt_entity_id", "transcript", "outcome", "speaker_id", "speaker_name",
            "confidence", "threshold", "margin", "extraction_mode", "extraction_status", "conversation_forwarded",
            "processing_status", "processing_speaker_id", "processing_backend",
            "processing_fallback_reason", "denoised_path",
        }
        json_columns = {
            "scores": "scores_json", "segments": "segments_json",
            "timings": "timings_json", "labels": "labels_json",
            "processing_stages": "processing_stages_json",
            "processing_quality": "processing_quality_json",
            "processing_timings": "processing_timings_json",
        }
        values: list[Any] = []
        clauses: list[str] = []
        for key, value in changes.items():
            column = json_columns.get(key, key)
            if key in json_columns:
                value = json.dumps(value)
            if column in columns or column in json_columns.values():
                clauses.append(f"{column}=?")
                values.append(value)
        if not clauses:
            return self.get_recording(recording_id)
        values.extend([_iso(), recording_id])
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE recordings SET {', '.join(clauses)}, updated_at=? WHERE id=?", values)
        return self.get_recording(recording_id)

    def reset_processing(self, recording_id: str) -> dict[str, Any] | None:
        """Remove only reproducible denoise output and processing metadata."""
        self._safe_id(recording_id)
        with self._lock:
            recording = self.get_recording(recording_id)
            if recording is None:
                return None
            raw_path = recording.get("denoised_path")
            if raw_path:
                denoised = Path(raw_path).resolve()
                allowed = self.analysis_dir.resolve()
                if allowed not in denoised.parents:
                    raise ValueError("Denoised audio path is outside analysis storage")
                denoised.unlink(missing_ok=True)

            labels = dict(recording.get("labels") or {})
            for key in ("fallback", "fallback_reason", "quality"):
                labels.pop(key, None)
            labels["audio_variant"] = "original"

            # New records keep processor timings separate. For records written
            # by 2.1.0 before this migration, reconstruct the original baseline.
            timings = dict(recording.get("timings") or {})
            baseline_total = timings.pop("baseline_total_ms", None)
            for key in (
                "audio_processing_ms", "denoise_ms", "model_load_ms",
                "cold_request_ms", "cold_start_ms", "post_utterance_ms",
                "stream_compute_ms", "stream_wall_ms", "df3_load_ms",
            ):
                timings.pop(key, None)
            if baseline_total is not None:
                timings["total_ms"] = baseline_total
            extraction_status = (
                "disabled"
                if recording.get("extraction_mode") == "off"
                else "not_processed"
            )

            with self._connect() as db:
                db.execute(
                    """
                    UPDATE recordings
                    SET denoised_path=NULL, processing_status='idle',
                        processing_speaker_id=NULL, processing_backend=NULL,
                        processing_stages_json='{}',
                        processing_quality_json='{}',
                        processing_fallback_reason=NULL,
                        processing_timings_json='{}',
                        extraction_status=?, timings_json=?, labels_json=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        extraction_status, json.dumps(timings),
                        json.dumps(labels), _iso(), recording_id,
                    ),
                )
        return self.get_recording(recording_id)

    def save_extracted(self, recording_id: str, pcm: bytes, sample_rate: int, status: str = "ready") -> dict[str, Any] | None:
        self._safe_id(recording_id)
        target = self.analysis_dir / recording_id / "extracted.wav"
        self._write_wav(target, pcm, sample_rate)
        with self._lock, self._connect() as db:
            db.execute("UPDATE recordings SET extracted_path=?, extraction_status=?, updated_at=? WHERE id=?", (str(target), status, _iso(), recording_id))
        return self.get_recording(recording_id)

    def save_audio_variant(self, recording_id: str, variant: str, pcm: bytes, sample_rate: int) -> dict[str, Any] | None:
        """Save a generated 2.1 variant without relabelling legacy extraction."""
        self._safe_id(recording_id)
        if variant not in {"denoised", "isolated"}:
            raise ValueError("Unsupported generated audio variant")
        target = self.analysis_dir / recording_id / f"{variant}.wav"
        self._write_wav(target, pcm, sample_rate)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                f"UPDATE recordings SET {variant}_path=?, updated_at=? WHERE id=?",
                (str(target), _iso(), recording_id),
            )
            if cursor.rowcount == 0:
                target.unlink(missing_ok=True)
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
                return None
        return self.get_recording(recording_id)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, destination in (
            ("scores_json", "scores"), ("segments_json", "segments"),
            ("timings_json", "timings"), ("labels_json", "labels"),
            ("processing_stages_json", "processing_stages"),
            ("processing_quality_json", "processing_quality"),
            ("processing_timings_json", "processing_timings"),
        ):
            result[destination] = json.loads(result.pop(source) or "{}")
        result["conversation_forwarded"] = bool(result["conversation_forwarded"]) if result["conversation_forwarded"] is not None else None
        return result

    def get_recording(self, recording_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _recording_predicate(*, outcome: str | None = None, source: str | None = None, speaker_id: str | None = None, query: str | None = None, since: str | None = None) -> tuple[str, list[Any]]:
        where: list[str] = []; values: list[Any] = []
        for column, value in (("outcome", outcome), ("source", source), ("speaker_id", speaker_id)):
            if value:
                where.append(f"{column}=?"); values.append(value)
        if query:
            where.append("(speaker_name LIKE ? OR transcript LIKE ? OR satellite_id LIKE ? OR stt_entity_id LIKE ?)")
            search = f"%{query}%"
            values.extend([search, search, search, search])
        if since:
            where.append("created_at>=?"); values.append(since)
        return ((" WHERE " + " AND ".join(where)) if where else "", values)

    def list_recordings(self, *, page: int = 1, page_size: int = 50, outcome: str | None = None, source: str | None = None, speaker_id: str | None = None, query: str | None = None, since: str | None = None) -> tuple[list[dict[str, Any]], int]:
        page = max(1, page); page_size = min(100, max(1, page_size))
        predicate, values = self._recording_predicate(outcome=outcome, source=source, speaker_id=speaker_id, query=query, since=since)
        with self._lock, self._connect() as db:
            total = int(db.execute("SELECT COUNT(*) FROM recordings" + predicate, values).fetchone()[0])
            rows = db.execute("SELECT * FROM recordings" + predicate + " ORDER BY created_at DESC LIMIT ? OFFSET ?", values + [page_size, (page - 1) * page_size]).fetchall()
        return [self._row(item) for item in rows], total

    def recording_ids(self, *, outcome: str | None = None, source: str | None = None, speaker_id: str | None = None, query: str | None = None, since: str | None = None) -> list[str]:
        predicate, values = self._recording_predicate(outcome=outcome, source=source, speaker_id=speaker_id, query=query, since=since)
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id FROM recordings" + predicate, values).fetchall()
        return [str(row["id"]) for row in rows]

    def audio_path(self, recording_id: str, variant: str) -> Path | None:
        if variant not in {"original", "denoised", "isolated", "extracted"}:
            return None
        row = self.get_recording(recording_id)
        if not row:
            return None
        raw = (row.get("isolated_path") or row.get("extracted_path")) if variant == "extracted" else row.get(f"{variant}_path")
        if not raw:
            return None
        path = Path(raw).resolve()
        allowed = self.analysis_dir.resolve()
        if allowed not in path.parents or not path.is_file():
            return None
        return path

    def delete_recording(self, recording_id: str) -> bool:
        row = self.get_recording(recording_id)
        if not row: return False
        with self._lock, self._connect() as db: db.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
        for raw in (row.get("original_path"), row.get("denoised_path"), row.get("isolated_path"), row.get("extracted_path")):
            if raw:
                path = Path(raw)
                if path.is_file(): path.unlink(missing_ok=True)
        try: (self.analysis_dir / recording_id).rmdir()
        except OSError: pass
        return True

    def storage_usage(self) -> int:
        total = 0
        for path in self.analysis_dir.rglob("*.wav"):
            try: total += path.stat().st_size
            except OSError: pass
        return total

    def add_sample(self, speaker_id: str, pcm: bytes, sample_rate: int, *, source_recording_id: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        sample_id = uuid.uuid4().hex
        target = self.enrollment_dir / speaker_id / f"{sample_id}.wav"
        self._write_wav(target, pcm, sample_rate)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO enrollment_samples (id,speaker_id,created_at,active,path,duration_seconds,bytes,source_recording_id,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)", (sample_id, speaker_id, _iso(), 1, str(target), len(pcm)/(2*sample_rate), target.stat().st_size, source_recording_id, json.dumps(metadata or {})))
        return self.get_sample(sample_id) or {"id": sample_id}

    def get_sample(self, sample_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM enrollment_samples WHERE id=?", (sample_id,)).fetchone()
        if not row: return None
        result = dict(row); result["active"] = bool(result["active"]); result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def list_samples(self, speaker_id: str, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM enrollment_samples WHERE speaker_id=?" + (" AND active=1" if active_only else "") + " ORDER BY created_at DESC"
        with self._lock, self._connect() as db: rows = db.execute(sql, (speaker_id,)).fetchall()
        return [self.get_sample(row["id"]) for row in rows if self.get_sample(row["id"])]

    def set_sample_active(self, sample_id: str, active: bool) -> dict[str, Any] | None:
        with self._lock, self._connect() as db: db.execute("UPDATE enrollment_samples SET active=? WHERE id=?", (int(active), sample_id))
        return self.get_sample(sample_id)

    def sample_path(self, sample_id: str) -> Path | None:
        sample = self.get_sample(sample_id)
        if not sample: return None
        path = Path(sample["path"]).resolve()
        return path if self.enrollment_dir.resolve() in path.parents and path.is_file() else None

    def delete_sample(self, sample_id: str, *, remove_audio: bool = True) -> bool:
        sample = self.get_sample(sample_id)
        if not sample: return False
        with self._lock, self._connect() as db: db.execute("DELETE FROM enrollment_samples WHERE id=?", (sample_id,))
        if remove_audio:
            Path(sample["path"]).unlink(missing_ok=True)
        return True

    def archive_or_delete_speaker_samples(self, speaker_id: str, delete_audio: bool) -> None:
        samples = self.list_samples(speaker_id)
        if delete_audio:
            for sample in samples: self.delete_sample(sample["id"], remove_audio=True)
        else:
            with self._lock, self._connect() as db: db.execute("UPDATE enrollment_samples SET speaker_id=NULL, active=0 WHERE speaker_id=?", (speaker_id,))

    def cleanup(
        self,
        now: datetime | None = None,
        protected_ids: set[str] | None = None,
    ) -> int:
        now = now or utcnow(); cutoff = now - timedelta(days=self.retention_days); removed = 0
        protected_ids = protected_ids or set()
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id,created_at,original_path,denoised_path,isolated_path,extracted_path FROM recordings ORDER BY created_at ASC").fetchall()
            # Analysis WAVs are deliberately excluded from Home Assistant
            # backups.  After restoring a backup, discard orphan metadata too.
            expired = [row for row in rows if row["id"] not in protected_ids and (not Path(row["original_path"]).is_file() or datetime.fromisoformat(row["created_at"]) < cutoff)]
            retained = [row for row in rows if row not in expired]
            total = sum(sum(Path(value).stat().st_size for value in (row['original_path'], row['denoised_path'], row['isolated_path'], row['extracted_path']) if value and Path(value).is_file()) for row in retained)
            while total > self.max_storage_bytes:
                position = next(
                    (
                        index
                        for index, candidate in enumerate(retained)
                        if candidate["id"] not in protected_ids
                    ),
                    None,
                )
                if position is None:
                    break
                expired.append(retained.pop(position)); row = expired[-1]
                total -= sum(Path(value).stat().st_size for value in (row['original_path'], row['denoised_path'], row['isolated_path'], row['extracted_path']) if value and Path(value).is_file())
            for row in expired:
                db.execute("DELETE FROM recordings WHERE id=?", (row['id'],))
                directory = self.analysis_dir / row['id']
                for path in (Path(row['original_path']), *(Path(value) for value in (row['denoised_path'], row['isolated_path'], row['extracted_path']) if value)):
                    if path and path.is_file(): path.unlink(missing_ok=True)
                try: directory.rmdir()
                except OSError: pass
                removed += 1
        return removed

    def calibration(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as db: row = db.execute("SELECT * FROM calibration WHERE id=1").fetchone()
        if not row: return None
        result = dict(row); result["details"] = json.loads(result.pop("details_json") or "{}")
        return result

    def set_calibration(self, threshold: float | None, margin: float | None, details: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            if threshold is None and margin is None: db.execute("DELETE FROM calibration WHERE id=1")
            else: db.execute("INSERT INTO calibration (id,threshold,margin,updated_at,details_json) VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET threshold=excluded.threshold,margin=excluded.margin,updated_at=excluded.updated_at,details_json=excluded.details_json", (threshold, margin, _iso(), json.dumps(details)))
        return self.calibration()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as db: row = db.execute("SELECT value_json FROM catalogue_settings WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO catalogue_settings (key,value_json) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (key, json.dumps(value)))
