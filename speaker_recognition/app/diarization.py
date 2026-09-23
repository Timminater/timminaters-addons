"""Opt-in, offline speaker timeline for retained analysis audio.

This is deliberately experimental. It never changes live recognition or STT.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np

from app.models import AudioInput


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TimelineStore:
    def __init__(self, catalogue):
        self.catalogue = catalogue
        self._initialized = False

    def initialize(self) -> None:
        with self.catalogue._lock:
            if self._initialized:
                return
            with self.catalogue._connect() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS diarization_runs (
                        recording_id TEXT PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
                        status TEXT NOT NULL, updated_at TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}'
                    )"""
                )
                db.execute(
                    "UPDATE diarization_runs SET status='failed', updated_at=?, "
                    "result_json=? WHERE status IN ('queued','running')",
                    (_now(), json.dumps({"reason": "interrupted_by_restart"})),
                )
            self._initialized = True

    def set(self, recording_id: str, status: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        self.catalogue._safe_id(recording_id)
        self.initialize()
        payload = result or {}
        with self.catalogue._lock, self.catalogue._connect() as db:
            if not db.execute("SELECT 1 FROM recordings WHERE id=?", (recording_id,)).fetchone():
                raise KeyError(recording_id)
            db.execute(
                "INSERT INTO diarization_runs(recording_id,status,updated_at,result_json) "
                "VALUES (?,?,?,?) ON CONFLICT(recording_id) DO UPDATE SET "
                "status=excluded.status,updated_at=excluded.updated_at,result_json=excluded.result_json",
                (recording_id, status, _now(), json.dumps(payload, allow_nan=False)),
            )
        return self.get(recording_id) or {}

    def get(self, recording_id: str) -> dict[str, Any] | None:
        self.catalogue._safe_id(recording_id)
        self.initialize()
        with self.catalogue._lock, self.catalogue._connect() as db:
            row = db.execute(
                "SELECT status,updated_at,result_json FROM diarization_runs WHERE recording_id=?",
                (recording_id,),
            ).fetchone()
        if not row:
            return None
        return {"recording_id": recording_id, "status": row["status"],
                "updated_at": row["updated_at"], **json.loads(row["result_json"])}


def analyze_timeline(recognizer, audio: AudioInput, threshold: float, margin: float) -> dict[str, Any]:
    """Score non-overlapping one-second speech windows with a separate encoder.

    Each window independently clears both decision gates. No label is inferred
    from a neighbouring window. Gaps and uncertain windows stay anonymous.
    """
    started = time.perf_counter()
    raw = recognizer._decode_audio(audio)
    wav = recognizer._canonicalize(raw, audio.sample_rate)
    with recognizer._lock:
        references = {key: value.copy() for key, value in recognizer._embeddings.items()}
        revisions = [recognizer.profile_revision_snapshot(key)["revision_id"] for key in sorted(references)]
    encoder = recognizer._encoder_factory() if references else None
    window = 16_000
    cells: list[dict[str, Any]] = []
    for start in range(0, len(wav), window):
        end = min(start + window, len(wav))
        clip = wav[start:end]
        if len(clip) < 8_000:
            continue
        rms = float(np.sqrt(np.mean(np.square(clip, dtype=np.float64))))
        if rms < 0.008:
            continue
        speaker_id: str | None = None
        confidence = None
        score_margin = None
        if encoder is not None:
            try:
                embedding = recognizer._embed_wav(encoder, clip, 16_000)
                ranked = sorted(
                    ((key, float(np.dot(ref, embedding))) for key, ref in references.items()),
                    key=lambda pair: pair[1], reverse=True,
                )
                if ranked:
                    confidence = ranked[0][1]
                    score_margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else -1.0)
                    if confidence >= threshold and score_margin >= margin:
                        speaker_id = ranked[0][0]
                        # Different voices inside one window must never be
                        # flattened into a single asserted identity.
                        if len(clip) >= window:
                            for half in (clip[: window // 2], clip[window // 2 :]):
                                half_embedding = recognizer._embed_wav(encoder, half, 16_000)
                                half_ranked = sorted(
                                    ((key, float(np.dot(ref, half_embedding))) for key, ref in references.items()),
                                    key=lambda pair: pair[1], reverse=True,
                                )
                                if not half_ranked or half_ranked[0][0] != speaker_id or half_ranked[0][1] < threshold:
                                    speaker_id = None
                                    break
            except ValueError:
                pass
        cells.append({"start_seconds": round(start / 16_000, 3),
                      "end_seconds": round(end / 16_000, 3),
                      "speaker_id": speaker_id,
                      "status": "known" if speaker_id else "unknown",
                      "confidence": round(confidence, 6) if confidence is not None else None,
                      "margin": round(score_margin, 6) if score_margin is not None else None})
    segments: list[dict[str, Any]] = []
    for cell in cells:
        if (segments and segments[-1]["speaker_id"] == cell["speaker_id"]
                and math.isclose(segments[-1]["end_seconds"], cell["start_seconds"], abs_tol=0.001)):
            segments[-1]["end_seconds"] = cell["end_seconds"]
            segments[-1]["confidence"] = min(
                segments[-1]["confidence"], cell["confidence"]
            ) if segments[-1]["confidence"] is not None and cell["confidence"] is not None else None
        else:
            segments.append(dict(cell))
    return {"experimental": True, "segments": segments,
            "threshold": threshold, "margin": margin,
            "profile_revisions": revisions,
            "duration_seconds": round(len(wav) / 16_000, 3),
            "processing_ms": round((time.perf_counter() - started) * 1000, 2)}
