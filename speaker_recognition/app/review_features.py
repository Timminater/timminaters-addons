"""Review labels and read-only recognition experiments for stored recordings.

This module deliberately never updates profiles.  A human truth label is kept
in the recording's labels JSON and only enrollment APIs may add it as audio.
"""
from __future__ import annotations

import base64
import json
import wave
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models import AudioInput


REVIEW_STATUSES = {"pending", "resolved", "ignored"}


def _rows(catalog: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    catalog.initialize()
    with catalog._lock, catalog._connect() as db:
        return [catalog._row(row) for row in db.execute(sql, params).fetchall()]


def list_review_inbox(
    catalog: Any, *, page: int = 1, page_size: int = 50, status: str | None = "pending"
) -> dict[str, Any]:
    """List unknown/ambiguous/error recordings and recordings manually flagged.

    Rows include ``has_audio`` derived from the catalog's safe audio resolver.
    ``status`` may be pending, resolved, ignored, or None for all statuses.
    """
    if status is not None and status not in REVIEW_STATUSES:
        raise ValueError("status must be pending, resolved, ignored, or None")
    page = max(1, int(page)); page_size = min(100, max(1, int(page_size)))
    rows = _rows(
        catalog,
        "SELECT * FROM recordings WHERE outcome IN ('unmatched','ambiguous','error','blocked') "
        "OR json_extract(labels_json,'$.review_status') IS NOT NULL "
        "ORDER BY created_at DESC, id DESC",
    )
    eligible = []
    for row in rows:
        review = row.get("labels", {}).get("review_status")
        effective = review or "pending"
        if status is not None and effective != status:
            continue
        row["review_status"] = effective
        row["truth_speaker_id"] = row.get("labels", {}).get("truth_speaker_id")
        row["truth_unknown"] = bool(row.get("labels", {}).get("truth_unknown", False))
        row["has_audio"] = bool(catalog.audio_path(row["id"], "original"))
        # Absolute paths are an internal storage detail, not part of this API.
        for key in ("original_path", "denoised_path", "isolated_path", "extracted_path"):
            row.pop(key, None)
        eligible.append(row)
    start = (page - 1) * page_size
    return {"items": eligible[start:start + page_size], "total": len(eligible), "page": page, "page_size": page_size}


def set_review(
    catalog: Any,
    recording_id: str,
    status: str,
    truth_speaker_id: str | None = None,
    truth_unknown: bool = False,
) -> dict[str, Any]:
    """Persist a review state and optional ground truth without profile changes."""
    catalog._safe_id(recording_id)
    if status not in REVIEW_STATUSES:
        raise ValueError("status must be pending, resolved, or ignored")
    if truth_unknown and truth_speaker_id is not None:
        raise ValueError("Choose a speaker or unknown, not both")
    if truth_speaker_id is not None and (not isinstance(truth_speaker_id, str) or not truth_speaker_id.strip()):
        raise ValueError("truth_speaker_id must be a non-empty speaker identifier")
    catalog.initialize()
    with catalog._lock, catalog._connect() as db:
        row = db.execute("SELECT labels_json FROM recordings WHERE id=?", (recording_id,)).fetchone()
        if not row:
            raise KeyError(recording_id)
        labels = json.loads(row["labels_json"] or "{}")
        labels["review_status"] = status
        if truth_speaker_id is not None or truth_unknown:
            labels["truth_speaker_id"] = truth_speaker_id
            labels["truth_unknown"] = bool(truth_unknown)
        else:
            # An omitted truth clears a previous manual label.
            labels.pop("truth_speaker_id", None)
            labels.pop("truth_unknown", None)
        db.execute("UPDATE recordings SET labels_json=?, updated_at=? WHERE id=?", (json.dumps(labels), datetime.now(timezone.utc).isoformat(), recording_id))
    return catalog.get_recording(recording_id) or {"id": recording_id}


def device_quality(catalog: Any, days: int = 30) -> dict[str, Any]:
    """Aggregate recognition counts and quality/latency signals by satellite."""
    if days < 1 or days > 3650:
        raise ValueError("days must be between 1 and 3650")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _rows(catalog, "SELECT * FROM recordings WHERE created_at>=? ORDER BY created_at", (since,))
    devices: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        devices[row.get("satellite_id") or "unknown"].append(row)
    result = []
    for device, items in sorted(devices.items()):
        waits: list[float] = []
        for item in items:
            timing = item.get("timings") or {}
            value = timing.get("total_ms", timing.get("recognition_ms"))
            if isinstance(value, (int, float)) and value >= 0:
                waits.append(float(value))
        waits.sort()
        quality_problems = sum(bool(
            ((item.get("labels") or {}).get("audio_quality") or {}).get("flags")
            or (item.get("processing_quality") or {}).get("flags")
            or item.get("processing_fallback_reason")
        ) for item in items)
        result.append({
            "satellite_id": device,
            "recordings": len(items),
            "recognitions": sum(item.get("outcome") == "matched" for item in items),
            "unknown": sum(item.get("outcome") in {"unmatched", "ambiguous"} for item in items),
            "quality_problems": quality_problems,
            "average_wait_ms": round(sum(waits) / len(waits), 2) if waits else None,
            "p95_wait_ms": round(waits[min(len(waits) - 1, int(len(waits) * .95))], 2) if waits else None,
            "wait_samples": len(waits),
        })
    return {"days": days, "devices": result}


def _read_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("stored audio is not mono PCM16")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def preview_experiment(
    catalog: Any, recognizer: Any, threshold: float, margin: float
) -> dict[str, Any]:
    """Run candidate settings against manually labeled retained recordings.

    This is intentionally ephemeral: recognition receives explicit overrides;
    no calibration, production configuration, recording, or profile is written.
    Enrollment source recordings are excluded from the independent test set.
    """
    if not 0 <= float(threshold) <= 1 or not 0 <= float(margin) <= 2:
        raise ValueError("threshold must be 0..1 and margin must be 0..2")
    rows = _rows(catalog, "SELECT * FROM recordings ORDER BY created_at DESC")
    active_speakers = {item.id for item in recognizer.list_speakers()} if hasattr(recognizer, "list_speakers") else None
    with catalog._lock, catalog._connect() as db:
        source_ids = {str(row[0]) for row in db.execute(
            "SELECT DISTINCT source_recording_id FROM enrollment_samples "
            "WHERE source_recording_id IS NOT NULL"
        )}
    evaluated = []; excluded = {"no_audio": 0, "enrollment_source": 0, "unlabeled": 0, "stale_label": 0, "failed": 0}
    for row in rows:
        labels = row.get("labels") or {}
        truth_id = labels.get("truth_speaker_id")
        truth_unknown = labels.get("truth_unknown") is True
        if truth_id is None and not truth_unknown:
            excluded["unlabeled"] += 1; continue
        if truth_id is not None and active_speakers is not None and truth_id not in active_speakers:
            excluded["stale_label"] += 1; continue
        if row["id"] in source_ids:
            excluded["enrollment_source"] += 1; continue
        path = catalog.audio_path(row["id"], "original")
        if not path:
            excluded["no_audio"] += 1; continue
        try:
            pcm, sample_rate = _read_pcm(path)
            audio = AudioInput(audio_data=base64.b64encode(pcm).decode("ascii"), sample_rate=sample_rate)
            analysis = recognizer.recognize_detailed(audio, threshold=float(threshold), min_margin=float(margin))
        except Exception as error:
            excluded["failed"] += 1
            evaluated.append({"recording_id": row["id"], "error": str(error)[:240]})
            continue
        predicted_id = analysis.speaker.id if analysis.speaker else None
        expected_id = truth_id if not truth_unknown else None
        evaluated.append({
            "recording_id": row["id"], "truth_speaker_id": expected_id,
            "predicted_speaker_id": predicted_id, "outcome": analysis.outcome,
            "correct": predicted_id == expected_id,
            "confidence": round(float(analysis.confidence), 5),
            "margin": round(float(analysis.margin), 5),
        })
    valid = [item for item in evaluated if "error" not in item]
    correct = sum(bool(item["correct"]) for item in valid)
    wrong_identity = sum(
        item["truth_speaker_id"] is not None
        and item["predicted_speaker_id"] is not None
        and item["truth_speaker_id"] != item["predicted_speaker_id"]
        for item in valid
    )
    false_accept_unknown = sum(
        item["truth_speaker_id"] is None and item["predicted_speaker_id"] is not None
        for item in valid
    )
    missed_known = sum(
        item["truth_speaker_id"] is not None and item["predicted_speaker_id"] is None
        for item in valid
    )
    return {
        "mode": "experiment", "model_route": "current", "alternative_model_routes": [],
        "unavailable_model_routes": [{"id": "alternative", "available": False, "reason": "No separately validated experimental model route is configured."}],
        "threshold": float(threshold), "margin": float(margin), "production_settings_changed": False,
        "sample_count": len(valid), "correct": correct,
        "wrong_identity": wrong_identity,
        "false_accept_unknown": false_accept_unknown,
        "missed_known": missed_known,
        "accuracy": round(correct / len(valid), 4) if valid else None,
        "excluded": excluded, "results": evaluated,
    }
