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
                      audio_retained INTEGER NOT NULL DEFAULT 1,
                      processing_status TEXT NOT NULL DEFAULT 'idle', processing_speaker_id TEXT,
                      processing_backend TEXT,
                      processing_stages_json TEXT NOT NULL DEFAULT '{}', processing_quality_json TEXT NOT NULL DEFAULT '{}',
                      processing_fallback_reason TEXT,
                      processing_timings_json TEXT NOT NULL DEFAULT '{}',
                      profile_revision_json TEXT NOT NULL DEFAULT '{}'
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
                    CREATE TABLE IF NOT EXISTS recognition_runs (
                      id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, created_at TEXT NOT NULL,
                      profile_revision_json TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS recognition_runs_recording ON recognition_runs(recording_id, created_at DESC);
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
                    "audio_retained": "INTEGER NOT NULL DEFAULT 1",
                    "processing_status": "TEXT NOT NULL DEFAULT 'idle'",
                    "processing_speaker_id": "TEXT",
                    "processing_backend": "TEXT",
                    "processing_stages_json": "TEXT NOT NULL DEFAULT '{}'",
                    "processing_quality_json": "TEXT NOT NULL DEFAULT '{}'",
                    "processing_fallback_reason": "TEXT",
                    "processing_timings_json": "TEXT NOT NULL DEFAULT '{}'",
                    "profile_revision_json": "TEXT NOT NULL DEFAULT '{}'",
                }
                for column, definition in migrations.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE recordings ADD COLUMN {column} {definition}")
            self._initialized = True

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

    def create_recording(self, pcm: bytes, sample_rate: int, *, source: str = "pipeline", retain_audio: bool = True, **metadata: Any) -> dict[str, Any]:
        self.initialize()
        recording_id = uuid.uuid4().hex
        target = self.analysis_dir / recording_id / "original.wav"
        if retain_audio:
            self._write_wav(target, pcm, sample_rate)
        duration = len(pcm) / (2 * sample_rate)
        now = _iso()
        try:
            with self._lock, self._connect() as db:
                db.execute(
                """INSERT INTO recordings (id,created_at,updated_at,source,satellite_id,stt_entity_id,
                   transcript,outcome,speaker_id,speaker_name,confidence,threshold,margin,scores_json,
                   segments_json,timings_json,extraction_mode,extraction_status,conversation_forwarded,
                   original_path,duration_seconds,bytes,labels_json,audio_retained,profile_revision_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (recording_id, now, now, source, metadata.get("satellite_id"), metadata.get("stt_entity_id"),
                 metadata.get("transcript"), metadata.get("outcome", "pending"), metadata.get("speaker_id"),
                 metadata.get("speaker_name"), metadata.get("confidence"), metadata.get("threshold"),
                 metadata.get("margin"), json.dumps(metadata.get("scores", {})), json.dumps(metadata.get("segments", [])),
                 json.dumps(metadata.get("timings", {})), metadata.get("extraction_mode", "off"),
                 metadata.get("extraction_status"), metadata.get("conversation_forwarded"), str(target) if retain_audio else "", duration,
                 target.stat().st_size if retain_audio else 0, json.dumps(metadata.get("labels", {})), int(retain_audio),
                 json.dumps(metadata.get("profile_revision", {}))),
                )
        except Exception:
            if retain_audio:
                target.unlink(missing_ok=True)
                try: target.parent.rmdir()
                except OSError: pass
            raise
        return self.get_recording(recording_id) or {"id": recording_id}

    def save_original_audio(self, recording_id: str, pcm: bytes, sample_rate: int) -> dict[str, Any] | None:
        """Retain the original after a decision (for the `errors` policy)."""
        self._safe_id(recording_id)
        with self._lock:
            row = self.get_recording(recording_id)
            if not row:
                return None
            if row.get("original_path") and self.audio_path(recording_id, "original"):
                return row
            target = self.analysis_dir / recording_id / "original.wav"
            self._write_wav(target, pcm, sample_rate)
            try:
                with self._connect() as db:
                    cursor = db.execute(
                        "UPDATE recordings SET original_path=?, bytes=?, audio_retained=1, updated_at=? WHERE id=?",
                        (str(target), target.stat().st_size, _iso(), recording_id),
                    )
                    if cursor.rowcount == 0:
                        target.unlink(missing_ok=True)
                        return None
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return self.get_recording(recording_id)

    def remove_original_audio(self, recording_id: str) -> dict[str, Any] | None:
        """Remove only a recording's indexed original WAV and clear its reference."""
        self._safe_id(recording_id)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT original_path FROM recordings WHERE id=?", (recording_id,)).fetchone()
            if not row:
                return None
            raw = row["original_path"]
            path = self._safe_catalog_path(raw, self.analysis_dir) if raw else None
            if path:
                path.unlink(missing_ok=True)
            db.execute(
                "UPDATE recordings SET original_path='', bytes=0, audio_retained=0, updated_at=? WHERE id=?",
                (_iso(), recording_id),
            )
        return self.get_recording(recording_id)

    def remove_analysis_audio(self, recording_id: str) -> dict[str, Any] | None:
        """Remove all indexed analysis WAVs while preserving recording metadata."""
        self._safe_id(recording_id)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT original_path,denoised_path,isolated_path,extracted_path FROM recordings WHERE id=?",
                (recording_id,),
            ).fetchone()
            if not row:
                return None
            for raw in (row["original_path"], row["denoised_path"], row["isolated_path"], row["extracted_path"]):
                path = self._safe_catalog_path(raw, self.analysis_dir) if raw else None
                if path:
                    path.unlink(missing_ok=True)
            db.execute(
                """UPDATE recordings SET original_path='', denoised_path=NULL, isolated_path=NULL,
                   extracted_path=NULL, bytes=0, audio_retained=0, updated_at=? WHERE id=?""",
                (_iso(), recording_id),
            )
        try:
            (self.analysis_dir / recording_id).rmdir()
        except OSError:
            pass
        return self.get_recording(recording_id)

    def reconcile_audio_retention(self, policy: str) -> int:
        """Finish privacy cleanup after an interrupted asynchronous job."""
        if policy not in {"none", "errors", "all"}:
            raise ValueError("Invalid analysis audio retention policy")
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT id,outcome,processing_status,labels_json FROM recordings WHERE audio_retained=1"
            ).fetchall()
        removed = 0
        for row in rows:
            labels = json.loads(row["labels_json"] or "{}")
            if not labels.get("retention_pending"):
                continue
            stored_policy = labels.get("retention_policy", policy)
            processing_status = row["processing_status"]
            if processing_status in {"queued", "running"}:
                self.update_recording(
                    row["id"], processing_status="failed",
                    processing_fallback_reason="interrupted_by_restart",
                )
                processing_status = "failed"
            if stored_policy == "none" or (
                stored_policy == "errors"
                and
                row["outcome"] == "matched" and processing_status == "complete"
            ):
                if self.remove_analysis_audio(row["id"]):
                    removed += 1
        return removed

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
            "profile_revision": "profile_revision_json",
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
        with self._lock:
            row = self.get_recording(recording_id)
            if not row or not row.get("audio_retained"):
                return row
            target = self.analysis_dir / recording_id / "extracted.wav"
            self._write_wav(target, pcm, sample_rate)
            with self._connect() as db:
                db.execute("UPDATE recordings SET extracted_path=?, extraction_status=?, updated_at=? WHERE id=?", (str(target), status, _iso(), recording_id))
            return self.get_recording(recording_id)

    def save_audio_variant(self, recording_id: str, variant: str, pcm: bytes, sample_rate: int) -> dict[str, Any] | None:
        """Save a generated 2.1 variant without relabelling legacy extraction."""
        self._safe_id(recording_id)
        if variant not in {"denoised", "isolated"}:
            raise ValueError("Unsupported generated audio variant")
        with self._lock:
            row = self.get_recording(recording_id)
            if not row or not row.get("audio_retained"):
                return row
            target = self.analysis_dir / recording_id / f"{variant}.wav"
            self._write_wav(target, pcm, sample_rate)
            with self._connect() as db:
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
            ("profile_revision_json", "profile_revision"),
        ):
            result[destination] = json.loads(result.pop(source) or "{}")
        result["conversation_forwarded"] = bool(result["conversation_forwarded"]) if result["conversation_forwarded"] is not None else None
        return result

    def get_recording(self, recording_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
        return self._row(row) if row else None

    def recording_profile_revision(self, recording_id: str) -> dict[str, Any] | None:
        """Return the immutable profile snapshot recorded for this analysis run."""
        recording = self.get_recording(recording_id)
        snapshot = recording.get("profile_revision") if recording else None
        return dict(snapshot) if isinstance(snapshot, dict) and snapshot else None

    def record_recognition_run(
        self,
        recording_id: str,
        profile_revision: dict[str, Any],
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an immutable recognition-run snapshot to the recording."""
        self._safe_id(recording_id)
        if not isinstance(profile_revision, dict) or not profile_revision.get("revision_id"):
            raise ValueError("A profile revision snapshot with revision_id is required")
        run_id = uuid.uuid4().hex
        created_at = _iso()
        with self._lock, self._connect() as db:
            if not db.execute("SELECT 1 FROM recordings WHERE id=?", (recording_id,)).fetchone():
                raise KeyError(recording_id)
            db.execute(
                "INSERT INTO recognition_runs (id,recording_id,created_at,profile_revision_json,details_json) VALUES (?,?,?,?,?)",
                (run_id, recording_id, created_at, json.dumps(profile_revision), json.dumps(details or {})),
            )
        return self.get_recognition_run(run_id) or {"id": run_id}

    def get_recognition_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM recognition_runs WHERE id=?", (run_id,)).fetchone()
        return self._recognition_run_row(row) if row else None

    def recognition_run_profile_revision(self, run_id: str) -> dict[str, Any] | None:
        """Read the exact immutable profile revision used by one run."""
        run = self.get_recognition_run(run_id)
        snapshot = run.get("profile_revision") if run else None
        return dict(snapshot) if isinstance(snapshot, dict) and snapshot else None

    def list_recognition_runs(self, recording_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM recognition_runs WHERE recording_id=? ORDER BY created_at DESC, id DESC",
                (recording_id,),
            ).fetchall()
        return [self._recognition_run_row(row) for row in rows]

    @staticmethod
    def _recognition_run_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["profile_revision"] = json.loads(result.pop("profile_revision_json") or "{}")
        result["details"] = json.loads(result.pop("details_json") or "{}")
        return result

    @staticmethod
    def _recording_predicate(*, outcome: str | None = None, source: str | None = None, speaker_id: str | None = None, query: str | None = None, since: str | None = None) -> tuple[str, list[Any]]:
        where: list[str] = []; values: list[Any] = []
        for column, value in (("outcome", outcome), ("source", source)):
            if value:
                where.append(f"{column}=?"); values.append(value)
        if speaker_id:
            where.append(
                "(speaker_id=? OR EXISTS ("
                "SELECT 1 FROM json_each(recordings.labels_json, '$.detected_speakers') "
                "WHERE json_extract(json_each.value, '$.speaker_id')=?"
                "))"
            )
            values.extend([speaker_id, speaker_id])
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
        self._safe_id(recording_id)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT original_path,denoised_path,isolated_path,extracted_path FROM recordings WHERE id=?", (recording_id,)).fetchone()
            if not row: return False
            db.execute("DELETE FROM recognition_runs WHERE recording_id=?", (recording_id,))
            db.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
            paths = [self._safe_catalog_path(raw, self.analysis_dir) for raw in (row["original_path"], row["denoised_path"], row["isolated_path"], row["extracted_path"]) if raw]
        for path in paths:
            if path: path.unlink(missing_ok=True)
        try: (self.analysis_dir / recording_id).rmdir()
        except OSError: pass
        return True

    def storage_usage(self) -> int:
        total = 0
        for path in self.analysis_dir.rglob("*.wav"):
            try: total += path.stat().st_size
            except OSError: pass
        return total

    def scan_orphans(self) -> dict[str, list[str]]:
        """Inventory missing references and unindexed WAVs; never deletes them."""
        self.initialize()
        referenced: set[Path] = set()
        missing: list[str] = []
        with self._lock, self._connect() as db:
            recording_rows = db.execute("SELECT original_path,denoised_path,isolated_path,extracted_path FROM recordings").fetchall()
            sample_rows = db.execute("SELECT path FROM enrollment_samples").fetchall()
        for row in recording_rows:
            for raw in (row["original_path"], row["denoised_path"], row["isolated_path"], row["extracted_path"]):
                if not raw:
                    continue
                path = self._safe_catalog_path(raw, self.analysis_dir)
                if path is None:
                    missing.append(str(raw))
                else:
                    referenced.add(path)
        for row in sample_rows:
            path = self._safe_catalog_path(row["path"], self.enrollment_dir)
            if path is None:
                missing.append(str(row["path"]))
            else:
                referenced.add(path)
        unindexed: list[str] = []
        for root in (self.analysis_dir, self.enrollment_dir):
            for path in root.rglob("*.wav"):
                try:
                    resolved = path.resolve()
                    if resolved not in referenced:
                        unindexed.append(str(resolved))
                except OSError:
                    continue
        return {"missing_references": sorted(set(missing)), "unindexed_wav_files": sorted(unindexed)}

    def delete_unindexed_wav_files(self, selected_paths: list[str]) -> int:
        """Remove only user-selected WAVs still unindexed inside owned roots."""
        if not selected_paths or len(selected_paths) > 100:
            raise ValueError("Select between 1 and 100 unindexed WAV files")
        deleted = 0
        with self._lock:
            allowed = set(self.scan_orphans()["unindexed_wav_files"])
            roots = (self.analysis_dir.resolve(), self.enrollment_dir.resolve())
            paths = [Path(raw).resolve() for raw in dict.fromkeys(selected_paths)]
            for path in paths:
                if (
                    str(path) not in allowed
                    or path.suffix.lower() != ".wav"
                    or not any(root in path.parents for root in roots)
                ):
                    raise ValueError("Path is not a current unindexed WAV inside app storage")
            for path in paths:
                if path.is_file():
                    path.unlink()
                    deleted += 1
        return deleted

    def storage_breakdown(self) -> dict[str, int]:
        """Report actual on-disk WAV bytes grouped by retention category."""
        self.initialize()
        categories: dict[str, set[Path]] = {
            "analysis_original_bytes": set(),
            "analysis_derived_bytes": set(),
            "active_enrollment_bytes": set(),
            "archived_enrollment_bytes": set(),
        }
        referenced: set[Path] = set()
        with self._lock, self._connect() as db:
            recordings = db.execute(
                "SELECT original_path,denoised_path,isolated_path,extracted_path FROM recordings"
            ).fetchall()
            samples = db.execute("SELECT path,speaker_id,active FROM enrollment_samples").fetchall()
            recording_count = int(db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0])
            active_speaker_count = int(db.execute(
                "SELECT COUNT(DISTINCT speaker_id) FROM enrollment_samples WHERE speaker_id IS NOT NULL AND active=1"
            ).fetchone()[0])
            archived_sample_count = int(db.execute(
                "SELECT COUNT(*) FROM enrollment_samples WHERE speaker_id IS NULL OR active=0"
            ).fetchone()[0])
        for row in recordings:
            original = row["original_path"]
            if original:
                path = self._safe_catalog_path(original, self.analysis_dir)
                if path:
                    categories["analysis_original_bytes"].add(path)
                    referenced.add(path)
            for raw in (row["denoised_path"], row["isolated_path"], row["extracted_path"]):
                if raw:
                    path = self._safe_catalog_path(raw, self.analysis_dir)
                    if path:
                        categories["analysis_derived_bytes"].add(path)
                        referenced.add(path)
        for row in samples:
            path = self._safe_catalog_path(row["path"], self.enrollment_dir)
            if not path:
                continue
            category = "active_enrollment_bytes" if row["speaker_id"] is not None and row["active"] else "archived_enrollment_bytes"
            categories[category].add(path)
            referenced.add(path)
        unindexed: set[Path] = set()
        for root in (self.analysis_dir, self.enrollment_dir):
            for path in root.rglob("*.wav"):
                try:
                    resolved = path.resolve()
                    if resolved not in referenced:
                        unindexed.add(resolved)
                except OSError:
                    continue
        result = {
            key: sum(self._file_bytes(path) for path in paths)
            for key, paths in categories.items()
        }
        result["unindexed_wav_bytes"] = sum(self._file_bytes(path) for path in unindexed)
        result["total_bytes"] = sum(result.values())
        result["recordings_count"] = recording_count
        result["active_speakers_count"] = active_speaker_count
        result["archived_samples_count"] = archived_sample_count
        return result

    @staticmethod
    def _file_bytes(path: Path) -> int:
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return 0

    def list_archived_samples(self) -> list[dict[str, Any]]:
        """List inactive or detached enrollment samples, newest first."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM enrollment_samples WHERE speaker_id IS NULL OR active=0 ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._sample_row(row) for row in rows]

    def delete_archived_sample(self, sample_id: str) -> bool:
        """Delete one archived sample and its audio only inside enrollment storage."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM enrollment_samples WHERE id=?", (sample_id,)).fetchone()
            if not row:
                return False
            if row["speaker_id"] is not None and row["active"]:
                raise ValueError("Only archived enrollment samples can be deleted here")
            candidate = Path(row["path"]).resolve()
            root = self.enrollment_dir.resolve()
            if root not in candidate.parents:
                raise ValueError("Archived sample path is outside enrollment storage")
            path = candidate if candidate.is_file() else None
            db.execute("DELETE FROM enrollment_samples WHERE id=?", (sample_id,))
        if path:
            path.unlink(missing_ok=True)
        return True

    @staticmethod
    def _safe_catalog_path(raw: str, root: Path) -> Path | None:
        try:
            path = Path(raw).resolve()
            base = root.resolve()
            if base not in path.parents or not path.is_file():
                return None
            return path
        except (OSError, RuntimeError):
            return None

    def add_sample(self, speaker_id: str, pcm: bytes, sample_rate: int, *, source_recording_id: str | None = None, metadata: dict[str, Any] | None = None, active: bool = True) -> dict[str, Any]:
        sample_id = uuid.uuid4().hex
        target = self.enrollment_dir / speaker_id / f"{sample_id}.wav"
        self._write_wav(target, pcm, sample_rate)
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO enrollment_samples (id,speaker_id,created_at,active,path,duration_seconds,bytes,source_recording_id,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)", (sample_id, speaker_id, _iso(), int(active), str(target), len(pcm)/(2*sample_rate), target.stat().st_size, source_recording_id, json.dumps(metadata or {})))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.get_sample(sample_id) or {"id": sample_id}

    def get_sample(self, sample_id: str, *, include_internal: bool = False) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM enrollment_samples WHERE id=?", (sample_id,)).fetchone()
        return self._sample_row(row, include_internal=include_internal) if row else None

    def store_sample_embedding(self, sample_id: str, embedding: list[float], model: str, preprocess: str) -> None:
        """Persist a rebuilt per-sample vector without exposing it publicly."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT metadata_json FROM enrollment_samples WHERE id=?", (sample_id,)).fetchone()
            if not row:
                raise KeyError(sample_id)
            metadata = json.loads(row[0] or "{}")
            metadata.update({
                "embedding": embedding,
                "embedding_model": model,
                "preprocess_version": preprocess,
            })
            db.execute("UPDATE enrollment_samples SET metadata_json=? WHERE id=?", (json.dumps(metadata), sample_id))
            db.execute("DELETE FROM calibration WHERE id=1")

    @staticmethod
    def _sample_row(row: sqlite3.Row, *, include_internal: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["active"] = bool(result["active"])
        metadata = json.loads(result.pop("metadata_json") or "{}")
        if not include_internal:
            metadata = {key: value for key, value in metadata.items() if key not in {"embedding", "legacy_embedding", "legacy_sample_count", "embedding_model", "preprocess_version", "legacy_embedding_model", "legacy_preprocess_version"}}
        result["metadata"] = metadata
        return result

    def list_samples(self, speaker_id: str, active_only: bool = False, *, include_internal: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM enrollment_samples WHERE speaker_id=?" + (" AND active=1" if active_only else "") + " ORDER BY created_at DESC"
        with self._lock, self._connect() as db: rows = db.execute(sql, (speaker_id,)).fetchall()
        return [self._sample_row(row, include_internal=include_internal) for row in rows]

    def set_sample_active(self, sample_id: str, active: bool) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT speaker_id,active FROM enrollment_samples WHERE id=?", (sample_id,)).fetchone()
            if row and row["speaker_id"] and bool(row["active"]) and not active:
                count = db.execute("SELECT COUNT(*) FROM enrollment_samples WHERE speaker_id=? AND active=1", (row["speaker_id"],)).fetchone()[0]
                if count <= 1:
                    raise ValueError("A profile needs at least one active sample")
            db.execute("UPDATE enrollment_samples SET active=? WHERE id=?", (int(active), sample_id))
            if row and active != bool(row["active"]):
                db.execute("DELETE FROM calibration WHERE id=1")
        return self.get_sample(sample_id)

    def set_samples_active(self, speaker_id: str, sample_ids: list[str], active: bool) -> None:
        """Atomically update a speaker's sample set and invalidate calibration."""
        with self._lock, self._connect() as db:
            if active:
                db.executemany("UPDATE enrollment_samples SET active=1 WHERE speaker_id=? AND id=?", [(speaker_id, item) for item in sample_ids])
            else:
                count = db.execute("SELECT COUNT(*) FROM enrollment_samples WHERE speaker_id=? AND active=1 AND id NOT IN (%s)" % (",".join("?" for _ in sample_ids) or "NULL"), [speaker_id, *sample_ids]).fetchone()[0]
                if count < 1:
                    raise ValueError("A profile needs at least one active sample")
                db.executemany("UPDATE enrollment_samples SET active=0 WHERE speaker_id=? AND id=?", [(speaker_id, item) for item in sample_ids])
            db.execute("DELETE FROM calibration WHERE id=1")

    def replace_active_samples(self, speaker_id: str, sample_ids: list[str]) -> list[str]:
        """Set an exact active sample revision; return the previous revision."""
        with self._lock, self._connect() as db:
            available = {row[0] for row in db.execute("SELECT id FROM enrollment_samples WHERE speaker_id=?", (speaker_id,))}
            if not set(sample_ids).issubset(available):
                raise ValueError("An active revision must contain existing samples")
            previous = [row[0] for row in db.execute("SELECT id FROM enrollment_samples WHERE speaker_id=? AND active=1", (speaker_id,))]
            db.execute("UPDATE enrollment_samples SET active=0 WHERE speaker_id=?", (speaker_id,))
            db.executemany("UPDATE enrollment_samples SET active=1 WHERE speaker_id=? AND id=?", [(speaker_id, item) for item in sample_ids])
            db.execute("DELETE FROM calibration WHERE id=1")
            return previous

    def sample_path(self, sample_id: str) -> Path | None:
        sample = self.get_sample(sample_id)
        if not sample: return None
        path = Path(sample["path"]).resolve()
        return path if self.enrollment_dir.resolve() in path.parents and path.is_file() else None

    def delete_sample(self, sample_id: str, *, remove_audio: bool = True) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM enrollment_samples WHERE id=?", (sample_id,)).fetchone()
            if not row: return False
            if row["speaker_id"] and row["active"]:
                count = db.execute("SELECT COUNT(*) FROM enrollment_samples WHERE speaker_id=? AND active=1", (row["speaker_id"],)).fetchone()[0]
                if count <= 1:
                    raise ValueError("A profile needs at least one active sample")
            sample = self._sample_row(row)
            db.execute("DELETE FROM enrollment_samples WHERE id=?", (sample_id,))
            if sample["active"]: db.execute("DELETE FROM calibration WHERE id=1")
        if remove_audio:
            Path(sample["path"]).unlink(missing_ok=True)
        return True

    def archive_or_delete_speaker_samples(self, speaker_id: str, delete_audio: bool) -> None:
        samples = self.list_samples(speaker_id)
        if delete_audio:
            # Remove files before dropping their index rows. If an unlink fails
            # or the process stops midway, a retry still has the remaining paths.
            for sample in samples:
                path = self._safe_catalog_path(sample["path"], self.enrollment_dir)
                if path is not None:
                    path.unlink(missing_ok=True)
            with self._lock, self._connect() as db:
                db.execute("DELETE FROM enrollment_samples WHERE speaker_id=?", (speaker_id,))
                db.execute("DELETE FROM calibration WHERE id=1")
        else:
            with self._lock, self._connect() as db:
                db.execute("UPDATE enrollment_samples SET speaker_id=NULL, active=0 WHERE speaker_id=?", (speaker_id,))
                db.execute("DELETE FROM calibration WHERE id=1")

    def move_samples(self, source_id: str, target_id: str, sample_ids: list[str] | None = None) -> None:
        """Move every enrollment row in one SQLite transaction, preserving WAV paths."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM enrollment_samples WHERE speaker_id=?", (source_id,)).fetchall()
            if not rows:
                raise ValueError("Source profile has no enrollment samples")
            available = {row["id"] for row in rows}
            selected = available if sample_ids is None else set(sample_ids)
            if not selected or not selected.issubset(available):
                raise ValueError("Sample ownership changed during profile merge")
            placeholders = ",".join("?" for _ in selected)
            db.execute(
                f"UPDATE enrollment_samples SET speaker_id=? WHERE speaker_id=? AND id IN ({placeholders})",
                (target_id, source_id, *selected),
            )
            db.execute("DELETE FROM calibration WHERE id=1")

    def scrub_guest_history(self, guest_id: str, guest_name: str) -> None:
        """Remove guest identity from historical analysis metadata without touching audio."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,segments_json,scores_json,labels_json,profile_revision_json FROM recordings WHERE speaker_id=? OR speaker_name=? OR segments_json LIKE ? OR segments_json LIKE ? OR scores_json LIKE ? OR scores_json LIKE ? OR labels_json LIKE ? OR labels_json LIKE ? OR profile_revision_json LIKE ? OR profile_revision_json LIKE ?", (guest_id, guest_name, f"%{guest_id}%", f"%{guest_name}%", f"%{guest_id}%", f"%{guest_name}%", f"%{guest_id}%", f"%{guest_name}%", f"%{guest_id}%", f"%{guest_name}%")).fetchall()
            for row in rows:
                try:
                    segments = json.loads(row["segments_json"] or "[]")
                except (TypeError, ValueError):
                    segments = []
                def scrub(value: Any) -> Any:
                    if isinstance(value, list):
                        return [scrub(item) for item in value]
                    if isinstance(value, dict):
                        result = {}
                        for key, item in value.items():
                            if key == guest_name:
                                continue
                            if key in {"speaker_id", "speaker_name"} and isinstance(item, str) and item in {guest_id, guest_name}:
                                result[key] = None
                            elif key == "name" and item == guest_name:
                                result[key] = "unknown"
                            elif isinstance(item, (dict, list)):
                                result[key] = scrub(item)
                            elif isinstance(item, str) and item in {guest_id, guest_name}:
                                result[key] = None
                            else:
                                result[key] = item
                        return result
                    return value
                try:
                    scores = json.loads(row["scores_json"] or "{}")
                except (TypeError, ValueError):
                    scores = {}
                try:
                    labels = json.loads(row["labels_json"] or "{}")
                except (TypeError, ValueError):
                    labels = {}
                try:
                    revision = json.loads(row["profile_revision_json"] or "{}")
                except (TypeError, ValueError):
                    revision = {}
                db.execute("UPDATE recordings SET speaker_id=CASE WHEN speaker_id=? THEN NULL ELSE speaker_id END, speaker_name=CASE WHEN speaker_id=? OR speaker_name=? THEN NULL ELSE speaker_name END, segments_json=?, scores_json=?, labels_json=?, profile_revision_json=? WHERE id=?", (guest_id, guest_id, guest_name, json.dumps(scrub(segments)), json.dumps(scrub(scores)), json.dumps(scrub(labels)), json.dumps(scrub(revision)), row["id"]))
            historical_runs = db.execute(
                "SELECT id,profile_revision_json,details_json FROM recognition_runs "
                "WHERE profile_revision_json LIKE ? OR details_json LIKE ? OR details_json LIKE ?",
                (f"%{guest_id}%", f"%{guest_id}%", f"%{guest_name}%"),
            ).fetchall()
            for run in historical_runs:
                revision = json.loads(run["profile_revision_json"] or "{}")
                details = json.loads(run["details_json"] or "{}")
                db.execute(
                    "UPDATE recognition_runs SET profile_revision_json=?, details_json=? WHERE id=?",
                    (json.dumps(scrub(revision)), json.dumps(scrub(details)), run["id"]),
                )
            # Experimental diarization is analysis metadata too. Keep each run
            # but anonymize references to an expired guest; its WAV is untouched.
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diarization_runs'").fetchone()
            if exists:
                runs = db.execute("SELECT recording_id,result_json FROM diarization_runs WHERE result_json LIKE ?", (f"%{guest_id}%",)).fetchall()
                def scrub_run(value: Any) -> Any:
                    if isinstance(value, list):
                        return [scrub_run(item) for item in value]
                    if isinstance(value, dict):
                        return {key: (None if key == "speaker_id" and item == guest_id else scrub_run(item) if isinstance(item, (dict, list)) else item) for key, item in value.items()}
                    return value
                for run in runs:
                    try:
                        result = json.loads(run["result_json"] or "{}")
                    except (TypeError, ValueError):
                        result = {}
                    db.execute("UPDATE diarization_runs SET result_json=? WHERE recording_id=?", (json.dumps(scrub_run(result)), run["recording_id"]))
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
            expired = [row for row in rows if row["id"] not in protected_ids and datetime.fromisoformat(row["created_at"]) < cutoff]
            retained = [row for row in rows if row not in expired]
            def row_bytes(row: sqlite3.Row) -> int:
                return sum(path.stat().st_size for value in (row["original_path"], row["denoised_path"], row["isolated_path"], row["extracted_path"]) if value and (path := self._safe_catalog_path(value, self.analysis_dir)))
            total = sum(row_bytes(row) for row in retained)
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
                total -= row_bytes(row)
            for row in expired:
                db.execute("DELETE FROM recordings WHERE id=?", (row['id'],))
                directory = self.analysis_dir / row['id']
                for value in (row['original_path'], row['denoised_path'], row['isolated_path'], row['extracted_path']):
                    path = self._safe_catalog_path(value, self.analysis_dir) if value else None
                    if path: path.unlink(missing_ok=True)
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

    def delete_setting(self, key: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM catalogue_settings WHERE key=?", (key,))

    def restore_sample_ownership(
        self, sample_ids: list[str], owner_id: str, other_allowed_owner_id: str
    ) -> None:
        """Idempotently restore journaled samples to their pre-merge owner."""
        if not sample_ids:
            raise ValueError("Merge journal has no source samples")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            by_id: dict[str, str | None] = {}
            for offset in range(0, len(sample_ids), 500):
                batch = sample_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT id,speaker_id FROM enrollment_samples WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
                by_id.update({row["id"]: row["speaker_id"] for row in rows})
            if set(by_id) != set(sample_ids) or any(
                current not in {owner_id, other_allowed_owner_id}
                for current in by_id.values()
            ):
                raise ValueError("Merge journal samples have unexpected ownership")
            moved = [sample_id for sample_id in sample_ids if by_id[sample_id] != owner_id]
            for offset in range(0, len(moved), 500):
                batch = moved[offset : offset + 500]
                moved_placeholders = ",".join("?" for _ in batch)
                db.execute(
                    f"UPDATE enrollment_samples SET speaker_id=? WHERE id IN ({moved_placeholders})",
                    (owner_id, *batch),
                )
            if moved:
                db.execute("DELETE FROM calibration WHERE id=1")
