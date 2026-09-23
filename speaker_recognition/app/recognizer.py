"""Persistent, thread-safe speaker profile storage and recognition."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import threading
import uuid
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Callable, Protocol

import numpy as np
from numpy.typing import NDArray

from app.audio_processor import (
    CANONICAL_RATE,
    ProcessedAudioResult,
    TargetAudioProcessor,
    resample_audio,
)
from app.models import AudioInput, SpeakerInfo
from app.storage import AudioCatalog

_LOGGER = logging.getLogger(__name__)
EMBEDDING_MODEL_VERSION = "resemblyzer-v1"
PREPROCESS_VERSION = "resemblyzer-preprocess-v1"
PROFILE_MERGE_JOURNAL_KEY = "speaker_profile_merge_journal"


class Encoder(Protocol):
    def embed_utterance(self, wav: NDArray[np.float32]) -> NDArray[np.float32]: ...


@dataclass(frozen=True)
class RecognitionAnalysis:
    """Rich recognition result. ``recognize`` keeps its historic tuple API."""
    speaker: SpeakerInfo | None
    confidence: float
    scores: dict[str, float]
    threshold: float
    margin: float
    outcome: str
    best_segment: dict[str, float] | None
    detected_speakers: list[dict[str, object]]
    candidates: list[dict[str, object]]
    timings: dict[str, float]
    canonical_pcm: bytes
    sample_rate: int = 16000
    extracted_pcm: bytes | None = None
    extraction_status: str | None = None


def _default_encoder() -> Encoder:
    from resemblyzer import VoiceEncoder  # type: ignore[import-untyped]

    return VoiceEncoder()


def _default_preprocess(wav: NDArray[np.float32], sample_rate: int) -> NDArray[np.float32]:
    from resemblyzer import preprocess_wav  # type: ignore[import-untyped]

    return np.asarray(preprocess_wav(wav, source_sr=sample_rate), dtype=np.float32)


class SpeakerRecognizer:
    def __init__(
        self,
        data_dir: Path,
        threshold: float,
        max_audio_seconds: int,
        encoder_factory: Callable[[], Encoder] = _default_encoder,
        preprocess: Callable[[NDArray[np.float32], int], NDArray[np.float32]] = _default_preprocess,
        min_margin: float = 0.0,
        audio_processing_backend: str = "df2_batch",
    ) -> None:
        self._profiles_dir = data_dir / "speakers"
        self._registry_path = self._profiles_dir / "registry.json"
        self._threshold = threshold
        self._min_margin = min_margin
        self._max_audio_seconds = max_audio_seconds
        self._encoder_factory = encoder_factory
        self._preprocess = preprocess
        self._encoder: Encoder | None = None
        self._profiles: dict[str, SpeakerInfo] = {}
        self._embeddings: dict[str, NDArray[np.float32]] = {}
        self._lock = threading.RLock()
        self.catalog = AudioCatalog(data_dir)
        self._audio_processor = TargetAudioProcessor(
            backend=audio_processing_backend
        )

    @property
    def ready(self) -> bool:
        return self._encoder is not None

    @property
    def speaker_count(self) -> int:
        """Cheap health snapshot; never waits for a model operation."""
        return len(self._profiles)

    def initialize(self) -> None:
        with self._lock:
            self._profiles_dir.mkdir(parents=True, exist_ok=True)
            self.catalog.initialize()
            self._load_profiles()
            self._recover_profile_merge_journal()
            self._encoder = self._encoder_factory()
            self._recover_profile_revisions()
            _LOGGER.info("Recognition engine ready with %d speaker(s)", len(self._profiles))

    def list_speakers(self) -> list[SpeakerInfo]:
        with self._lock:
            return sorted(self._profiles.values(), key=lambda item: item.name.casefold())

    def profile_revision_snapshot(
        self,
        speaker_id: str,
        *,
        threshold: float | None = None,
        margin: float | None = None,
        settings: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Return a durable identity and decision snapshot for one profile."""
        with self._lock:
            profile = self._profiles.get(speaker_id)
            if profile is None:
                raise KeyError(speaker_id)
            samples = self.catalog.list_samples(speaker_id, active_only=True, include_internal=True)
            sample_versions = sorted(
                (
                    {
                        "id": sample["id"],
                        "model": (sample.get("metadata") or {}).get("embedding_model")
                        or (sample.get("metadata") or {}).get("legacy_embedding_model")
                        or "legacy-unknown",
                        "preprocess": (sample.get("metadata") or {}).get("preprocess_version")
                        or (sample.get("metadata") or {}).get("legacy_preprocess_version")
                        or "legacy-unknown",
                        "legacy_model": (sample.get("metadata") or {}).get("legacy_embedding_model"),
                        "legacy_preprocess": (sample.get("metadata") or {}).get("legacy_preprocess_version"),
                    }
                    for sample in samples
                ),
                key=lambda item: item["id"],
            )
            legacy_fingerprint = None
            if not samples:
                embedding_path = self._profiles_dir / f"{speaker_id}.npy"
                try:
                    legacy_fingerprint = hashlib.sha256(embedding_path.read_bytes()).hexdigest()
                except OSError:
                    legacy_fingerprint = "unavailable"
            identity = {
                "speaker_id": speaker_id,
                "samples": sample_versions,
                "legacy_fingerprint": legacy_fingerprint,
            }
            canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            snapshot: dict[str, object] = {
                "revision_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "speaker_id": speaker_id,
                "sample_ids": [item["id"] for item in sample_versions],
                "sample_versions": sample_versions,
                "sample_count": profile.sample_count,
                "threshold": self._threshold if threshold is None else float(threshold),
                "margin": self._min_margin if margin is None else float(margin),
            }
            if legacy_fingerprint:
                snapshot["legacy_fingerprint"] = legacy_fingerprint
            if settings:
                # Require JSON-compatible primitive configuration in history.
                snapshot["settings"] = json.loads(json.dumps(settings, allow_nan=False))
            return snapshot

    def close(self) -> None:
        """Release the optional model worker without affecting profile storage."""
        self._audio_processor.close()

    def warm_audio_processor(self) -> bool:
        """Load the optional denoiser once and keep it resident."""
        return self._audio_processor.start()

    def configure_audio_processing_backend(self, backend: str) -> None:
        """Change the preferred runtime backend without restarting models."""
        self._audio_processor.configure_backend(backend)

    def enroll(
        self,
        speaker_name: str,
        audio_inputs: list[AudioInput],
        replace: bool = False,
        person_entity_id: str | None = None,
        update_person_mapping: bool = False,
        profile_kind: str = "resident",
        expires_at: datetime | None = None,
        expiry_was_explicit: bool = False,
        profile_kind_explicit: bool = False,
        source_recording_id: str | None = None,
    ) -> SpeakerInfo:
        with self._lock:
            if profile_kind not in {"resident", "guest"}:
                raise ValueError("Invalid profile kind")
            if profile_kind == "guest" and person_entity_id is not None:
                raise ValueError("Guest profiles cannot be linked to a Home Assistant person")
            if profile_kind == "resident" and expires_at is not None:
                raise ValueError("Only guest profiles can have an expiry")
            if expires_at is not None:
                if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                    raise ValueError("Guest expiry must include a timezone")
                expires_at = expires_at.astimezone(timezone.utc)
            encoder = self._require_encoder()
            existing = next(
                (profile for profile in self._profiles.values() if profile.name.casefold() == speaker_name.casefold()),
                None,
            )
            if existing is not None and not profile_kind_explicit:
                profile_kind = existing.profile_kind
            if existing is not None and profile_kind == "guest" and not expiry_was_explicit:
                expires_at = existing.expires_at
            elif existing is None and profile_kind == "guest" and expires_at is None and not expiry_was_explicit:
                expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            embeddings = [self._embed(encoder, audio) for audio in audio_inputs]
            now = datetime.now(timezone.utc)

            if existing is None:
                speaker_id = uuid.uuid4().hex
                profile = SpeakerInfo(
                    id=speaker_id,
                    name=speaker_name,
                    sample_count=len(embeddings),
                    created_at=now,
                    updated_at=now,
                    person_entity_id=person_entity_id,
                    profile_kind=profile_kind,
                    expires_at=expires_at,
                )
            else:
                speaker_id = existing.id
                profile = SpeakerInfo(
                    id=speaker_id,
                    name=speaker_name,
                    sample_count=len(embeddings) if replace else existing.sample_count + len(embeddings),
                    created_at=existing.created_at,
                    updated_at=now,
                    person_entity_id=(
                        None if profile_kind == "guest" else (
                            person_entity_id
                            if update_person_mapping
                            else existing.person_entity_id
                        )
                    ),
                    profile_kind=profile_kind,
                    expires_at=expires_at if profile_kind == "guest" else None,
                )

            previous_profile = self._profiles.get(speaker_id)
            previous_embedding = self._embeddings.get(speaker_id)
            staged: list[str] = []
            old_active = self.catalog.list_samples(speaker_id, active_only=True)
            try:
                for index, (audio, vector) in enumerate(zip(audio_inputs, embeddings)):
                    metadata = {
                        "source": "enroll",
                        "embedding": vector.tolist(),
                        "embedding_model": EMBEDDING_MODEL_VERSION,
                        "preprocess_version": PREPROCESS_VERSION,
                    }
                    if existing and not replace and not old_active and index == 0:
                        metadata["legacy_embedding"] = previous_embedding.tolist()
                        metadata["legacy_sample_count"] = existing.sample_count
                        metadata["legacy_embedding_model"] = EMBEDDING_MODEL_VERSION
                        metadata["legacy_preprocess_version"] = PREPROCESS_VERSION
                    sample = self.catalog.add_sample(
                        speaker_id, self._decode_pcm_bytes(audio), audio.sample_rate,
                        source_recording_id=source_recording_id,
                        metadata=metadata,
                        active=False,
                    )
                    staged.append(sample["id"])
                active_ids = staged if replace else [sample["id"] for sample in old_active] + staged
                sample_vectors = self._active_sample_vectors(speaker_id, active_ids)
                # Profiles predating per-sample vectors may have no permanent
                # WAVs. Preserve their existing reference when appending.
                new_embedding = self._normalized_mean(sample_vectors)
                profile = profile.model_copy(update={"sample_count": len(sample_vectors)})
                self._write_embedding(speaker_id, new_embedding)
                self._profiles[speaker_id] = profile
                self._embeddings[speaker_id] = new_embedding
                self._write_registry()
                self.catalog.replace_active_samples(speaker_id, active_ids)
            except Exception:
                if staged:
                    try:
                        self.catalog.replace_active_samples(
                            speaker_id,
                            [item["id"] for item in old_active],
                        )
                    except Exception:
                        pass
                    for sample_id in staged:
                        try:
                            self.catalog.delete_sample(sample_id, remove_audio=True)
                        except Exception:
                            pass
                if previous_profile is None or previous_embedding is None:
                    (self._profiles_dir / f"{speaker_id}.npy").unlink(missing_ok=True)
                    self._profiles.pop(speaker_id, None)
                    self._embeddings.pop(speaker_id, None)
                else:
                    self._write_embedding(speaker_id, previous_embedding)
                    self._profiles[speaker_id] = previous_profile
                    self._embeddings[speaker_id] = previous_embedding
                try:
                    self._write_registry()
                except OSError:
                    _LOGGER.exception("Could not restore speaker registry after failed enrollment")
                raise
            return profile

    def merge_profiles(self, source_id: str, target_id: str) -> SpeakerInfo:
        """Merge source samples into target and rebuild target from its active WAVs."""
        with self._lock:
            if source_id == target_id:
                raise ValueError("Choose two different profiles")
            source = self._profiles.get(source_id)
            target = self._profiles.get(target_id)
            if source is None or target is None:
                raise KeyError(source_id if source is None else target_id)
            source_samples = self.catalog.list_samples(source_id)
            target_samples = self.catalog.list_samples(target_id)
            active = [item for item in source_samples + target_samples if item["active"]]
            missing = [item["id"] for item in active if self.catalog.sample_path(item["id"]) is None]
            if missing:
                raise ValueError(f"Cannot merge: active enrollment WAV is missing ({missing[0]})")
            if not active:
                raise ValueError("Cannot merge profiles without active enrollment samples")

            source_vector = self._embeddings[source_id].copy()
            target_vector = self._embeddings[target_id].copy()
            target_embedding_path = self._profiles_dir / f"{target_id}.npy"
            source_embedding_path = self._profiles_dir / f"{source_id}.npy"
            target_file = target_embedding_path.read_bytes()
            source_file = source_embedding_path.read_bytes()
            journal = {
                "source_id": source_id,
                "target_id": target_id,
                "source_sample_ids": [item["id"] for item in source_samples],
            }
            self.catalog.set_setting(PROFILE_MERGE_JOURNAL_KEY, journal)
            try:
                self.catalog.move_samples(source_id, target_id)
                vectors = self._active_sample_vectors(target_id, [item["id"] for item in active])
                embedding = self._normalized_mean(vectors)
                merged = target.model_copy(update={"sample_count": len(vectors), "updated_at": datetime.now(timezone.utc)})
                self._write_embedding(target_id, embedding)
                self._profiles[target_id] = merged
                self._embeddings[target_id] = embedding
                self._profiles.pop(source_id)
                self._embeddings.pop(source_id)
                self._write_registry()
            except Exception:
                self._profiles[target_id] = target
                self._embeddings[target_id] = target_vector
                self._profiles[source_id] = source
                self._embeddings[source_id] = source_vector
                target_embedding_path.write_bytes(target_file)
                source_embedding_path.write_bytes(source_file)
                try:
                    self.catalog.restore_sample_ownership(
                        journal["source_sample_ids"], source_id, target_id
                    )
                except Exception:
                    _LOGGER.exception("Could not restore enrollment ownership after failed profile merge")
                    raise
                try:
                    self._write_registry()
                except Exception:
                    _LOGGER.exception("Could not restore profile registry after failed merge")
                    raise
                self.catalog.delete_setting(PROFILE_MERGE_JOURNAL_KEY)
                raise
            self.catalog.delete_setting(PROFILE_MERGE_JOURNAL_KEY)
            source_embedding_path.unlink(missing_ok=True)
            return merged

    def _recover_profile_merge_journal(self) -> None:
        """Resolve an interrupted merge against the atomically replaced registry."""
        journal = self.catalog.get_setting(PROFILE_MERGE_JOURNAL_KEY)
        if journal is None:
            return
        try:
            source_id = journal["source_id"]
            target_id = journal["target_id"]
            sample_ids = journal["source_sample_ids"]
            if (
                not isinstance(source_id, str)
                or not isinstance(target_id, str)
                or source_id == target_id
                or not isinstance(sample_ids, list)
                or not sample_ids
                or any(not isinstance(sample_id, str) for sample_id in sample_ids)
            ):
                raise ValueError("Malformed profile merge journal")
        except (KeyError, TypeError) as error:
            raise ValueError("Malformed profile merge journal") from error

        if source_id not in self._profiles:
            # Registry replacement committed: target is authoritative. The
            # journal may only have survived the final setting deletion.
            self.catalog.delete_setting(PROFILE_MERGE_JOURNAL_KEY)
            return
        if target_id not in self._profiles:
            raise RuntimeError("Cannot recover profile merge: target is absent from registry")

        # Old registry is still authoritative. Restore only rows recorded as
        # belonging to source before merge; target's original samples stay put.
        self.catalog.restore_sample_ownership(sample_ids, source_id, target_id)
        self.catalog.delete_setting(PROFILE_MERGE_JOURNAL_KEY)

    def expire_guest_profiles(self, now: datetime | None = None) -> list[str]:
        """Delete expired guest profiles and registration WAVs, scrubbing history labels."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Expiry cleanup time must include a timezone")
        moment = moment.astimezone(timezone.utc)
        with self._lock:
            expired = [profile for profile in self._profiles.values()
                       if profile.profile_kind == "guest" and profile.expires_at is not None
                       and profile.expires_at <= moment]
            removed: list[str] = []
            for profile in expired:
                # Keep the profile indexed until registration files and rows are
                # gone. An interrupted cleanup can then safely retry on startup.
                self.catalog.scrub_guest_history(profile.id, profile.name)
                self.catalog.archive_or_delete_speaker_samples(profile.id, delete_audio=True)
                previous_embedding = self._embeddings[profile.id]
                del self._profiles[profile.id]
                del self._embeddings[profile.id]
                try:
                    self._write_registry()
                except Exception:
                    self._profiles[profile.id] = profile
                    self._embeddings[profile.id] = previous_embedding
                    try:
                        self._write_registry()
                    except Exception:
                        _LOGGER.exception("Could not restore guest profile registry after failed expiry")
                    raise
                try:
                    (self._profiles_dir / f"{profile.id}.npy").unlink(missing_ok=True)
                except OSError:
                    _LOGGER.exception("Could not remove expired guest embedding %s", profile.id)
                removed.append(profile.id)
            return removed

    def delete(self, speaker_id: str, delete_audio: bool = True) -> bool:
        with self._lock:
            if speaker_id not in self._profiles:
                return False
            profile = self._profiles.pop(speaker_id)
            embedding = self._embeddings.pop(speaker_id)
            try:
                self._write_registry()
            except OSError:
                self._profiles[speaker_id] = profile
                self._embeddings[speaker_id] = embedding
                raise
            try:
                (self._profiles_dir / f"{speaker_id}.npy").unlink(missing_ok=True)
            except OSError as error:
                _LOGGER.warning("Could not remove obsolete embedding %s: %s", speaker_id, error)
            self.catalog.archive_or_delete_speaker_samples(speaker_id, delete_audio)
            return True

    def retrain_from_samples(self, speaker_id: str) -> SpeakerInfo:
        """Rebuild an embedding solely from active permanent enrollment WAVs."""
        with self._lock:
            profile = self._profiles.get(speaker_id)
            if not profile: raise KeyError(speaker_id)
            samples = self.catalog.list_samples(speaker_id, active_only=True)
            if not samples: raise ValueError("At least one active enrollment sample is required")
            inputs: list[AudioInput] = []
            for sample in samples:
                try:
                    with wave.open(str(self.catalog.sample_path(sample["id"])), "rb") as handle:
                        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                            continue
                        inputs.append(AudioInput(audio_data=base64.b64encode(handle.readframes(handle.getnframes())).decode(), sample_rate=handle.getframerate()))
                except (OSError, wave.Error):
                    _LOGGER.warning("Skipping unreadable enrollment sample %s", sample["id"])
            if not inputs: raise ValueError("No readable active enrollment samples")
            vectors = self._active_sample_vectors(speaker_id, [sample["id"] for sample in samples])
            embedding = self._normalized_mean(vectors)
            updated = profile.model_copy(update={"sample_count": len(vectors), "updated_at": datetime.now(timezone.utc)})
            previous_embedding = self._embeddings[speaker_id]
            self._write_embedding(speaker_id, embedding)
            self._profiles[speaker_id] = updated
            self._embeddings[speaker_id] = embedding
            try:
                self._write_registry()
            except Exception:
                self._profiles[speaker_id] = profile
                self._embeddings[speaker_id] = previous_embedding
                self._write_embedding(speaker_id, previous_embedding)
                try:
                    self._write_registry()
                except OSError:
                    _LOGGER.exception("Could not restore profile registry after failed retraining")
                raise
            return updated

    def set_sample_active_and_retrain(
        self, speaker_id: str, sample_id: str, active: bool,
    ) -> tuple[dict, SpeakerInfo]:
        """Serialize sample activation and profile revision as one operation."""
        with self._lock:
            sample = self.catalog.get_sample(sample_id)
            if not sample or sample["speaker_id"] != speaker_id:
                raise KeyError(sample_id)
            was_active = bool(sample["active"])
            updated = self.catalog.set_sample_active(sample_id, active)
            if was_active == active:
                return updated, self._profiles[speaker_id]
            try:
                profile = self.retrain_from_samples(speaker_id)
            except Exception:
                self.catalog.set_sample_active(sample_id, was_active)
                raise
            return updated, profile

    def delete_sample_and_retrain(self, speaker_id: str, sample_id: str) -> bool:
        """Publish a profile without an active sample before deleting its WAV."""
        with self._lock:
            sample = self.catalog.get_sample(sample_id)
            if not sample or sample["speaker_id"] != speaker_id:
                raise KeyError(sample_id)
            was_active = bool(sample["active"])
            if was_active:
                self.catalog.set_sample_active(sample_id, False)
                try:
                    self.retrain_from_samples(speaker_id)
                except Exception:
                    self.catalog.set_sample_active(sample_id, True)
                    raise
            return self.catalog.delete_sample(sample_id)

    def calibration_preview(self) -> dict[str, object]:
        """Estimate a conservative threshold from permanent labeled samples.

        Leave-one-out references avoid rewarding a sample for matching itself.
        False accepts are four times as expensive as misses, matching the UI's
        promise that a wrong person is worse than no person.
        """
        with self._lock:
            vectors: dict[str, list[NDArray[np.float32]]] = {}
            encoder = self._require_encoder()
            for profile in self._profiles.values():
                items: list[NDArray[np.float32]] = []
                for sample in self.catalog.list_samples(profile.id, active_only=True):
                    path = self.catalog.sample_path(sample["id"])
                    if not path: continue
                    try:
                        with wave.open(str(path), "rb") as handle:
                            if handle.getnchannels() != 1 or handle.getsampwidth() != 2: continue
                            pcm = handle.readframes(handle.getnframes())
                            input_ = AudioInput(audio_data=base64.b64encode(pcm).decode(), sample_rate=handle.getframerate())
                            items.append(self._embed(encoder, input_))
                    except (OSError, wave.Error, ValueError):
                        continue
                if items: vectors[profile.id] = items
            genuine: list[float] = []; impostor: list[float] = []
            genuine_margins: list[float] = []; impostor_margins: list[float] = []
            for speaker_id, items in vectors.items():
                for index, vector in enumerate(items):
                    same = [item for position, item in enumerate(items) if position != index]
                    if not same: continue
                    reference = self._normalized_mean(same)
                    score = float(np.dot(vector, reference)); genuine.append(score)
                    rivals = [float(np.dot(vector, self._normalized_mean(other))) for other_id, other in vectors.items() if other_id != speaker_id and other]
                    if rivals:
                        best_rival = max(rivals)
                        impostor.append(best_rival)
                        genuine_margins.append(score - best_rival)
                        impostor_margins.append(best_rival - score)
            if len(genuine) < 3 or len(impostor) < 3:
                return {"ready": False, "genuine_count": len(genuine), "impostor_count": len(impostor), "reason": "At least three genuine and three impostor observations are required"}
            candidates = sorted(set(genuine + impostor + [self._threshold]))
            threshold = min(candidates, key=lambda value: (4 * sum(item >= value for item in impostor) + sum(item < value for item in genuine), -value))
            margin_candidates = sorted(
                set([0.0] + [max(0.0, item) for item in genuine_margins + impostor_margins])
            )
            def margin_cost(value: float) -> tuple[int, int, float]:
                false_accepts = sum(
                    score >= threshold and margin_value >= value
                    for score, margin_value in zip(impostor, impostor_margins)
                )
                false_rejects = sum(
                    score < threshold or margin_value < value
                    for score, margin_value in zip(genuine, genuine_margins)
                )
                return 4 * false_accepts + false_rejects, false_accepts, -value
            margin = min(margin_candidates, key=margin_cost)
            false_accepts = sum(
                score >= threshold and margin_value >= margin
                for score, margin_value in zip(impostor, impostor_margins)
            )
            false_rejects = sum(
                score < threshold or margin_value < margin
                for score, margin_value in zip(genuine, genuine_margins)
            )
            return {
                "ready": True, "threshold": round(float(threshold), 4), "margin": round(float(max(0.0, margin)), 4),
                "genuine_count": len(genuine), "impostor_count": len(impostor),
                "false_accepts": false_accepts, "false_rejects": false_rejects,
                "genuine_scores": genuine, "impostor_scores": impostor, "margins": genuine_margins,
            }

    def recognize(self, audio_input: AudioInput) -> tuple[SpeakerInfo | None, float, dict[str, float]]:
        detailed = self.recognize_detailed(audio_input)
        return detailed.speaker, detailed.confidence, detailed.scores

    def recognize_detailed_with_snapshot(
        self,
        audio_input: AudioInput,
        *,
        threshold: float | None = None,
        min_margin: float | None = None,
    ) -> tuple[RecognitionAnalysis, dict[str, object]]:
        """Recognize and capture the exact profile revisions under one lock."""
        with self._lock:
            detailed = self.recognize_detailed(
                audio_input, threshold=threshold, min_margin=min_margin
            )
            effective_margin = self._min_margin if min_margin is None else float(min_margin)
            profile_revisions = [
                self.profile_revision_snapshot(
                    speaker_id, threshold=detailed.threshold, margin=effective_margin
                )
                for speaker_id in sorted(self._profiles)
            ]
            identity = {
                "profiles": [
                    {
                        "speaker_id": item["speaker_id"],
                        "revision_id": item["revision_id"],
                    }
                    for item in profile_revisions
                ],
                "threshold": detailed.threshold,
                "margin": effective_margin,
            }
            canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            snapshot: dict[str, object] = {
                "revision_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "profile_revisions": profile_revisions,
                "threshold": detailed.threshold,
                "margin": effective_margin,
            }
            return detailed, snapshot

    def process_target_audio(
        self,
        audio_input: AudioInput,
        _speaker_id: str | None = None,
        *,
        timeout_seconds: float = 12,
        priority: str = "live",
        min_margin: float | None = None,
    ) -> ProcessedAudioResult:
        """Compatibility alias for clients that still send a speaker ID."""
        del min_margin
        return self.denoise_audio(
            audio_input,
            timeout_seconds=timeout_seconds,
            priority=priority,
        )

    def denoise_audio(
        self,
        audio_input: AudioInput,
        *,
        timeout_seconds: float = 12,
        priority: str = "live",
    ) -> ProcessedAudioResult:
        """Run the optional general enhancement stage."""
        original = self._canonicalize(
            self._decode_audio(audio_input), audio_input.sample_rate
        )
        result = self._audio_processor.process(
            original,
            timeout_seconds=timeout_seconds,
            priority=priority,
        )
        return ProcessedAudioResult(
            denoised_pcm=result.denoised_pcm,
            isolated_pcm=None,
            sample_rate=result.sample_rate,
            stages=result.stages,
            timings=result.timings,
            quality=result.quality,
            fallback_reason=(
                None if result.denoised_pcm is not None else result.fallback_reason
            ),
        )

    def denoise_audio_stream(
        self,
        chunks: Iterable[bytes],
        sample_rate: int,
        *,
        timeout_seconds: float = 12,
    ) -> ProcessedAudioResult:
        """Run the configured stateful backend while PCM chunks arrive."""
        return self._audio_processor.process_stream(
            chunks,
            sample_rate,
            timeout_seconds=timeout_seconds,
        )

    def recognize_detailed(
        self,
        audio_input: AudioInput,
        *,
        threshold: float | None = None,
        min_margin: float | None = None,
        extract_for_speaker_id: str | None = None,
    ) -> RecognitionAnalysis:
        """Score a complete utterance and its likely speech regions.

        Energy VAD is deliberately dependency-free so this add-on keeps working
        on all supported Home Assistant architectures.  A full utterance is
        always a candidate, protecting short speech and VAD edge cases.
        """
        with self._lock:
            if not self._profiles:
                raise RuntimeError("No speakers have been enrolled")
            started = time.perf_counter()
            encoder = self._require_encoder()
            raw = self._decode_audio(audio_input)
            canonical = self._canonicalize(raw, audio_input.sample_rate)
            candidates = self._candidate_regions(canonical)
            scored: list[dict[str, object]] = []
            score_by_id: dict[str, float] = {speaker_id: -1.0 for speaker_id in self._profiles}
            best_by_id: dict[str, dict[str, object]] = {}
            for start, end, kind in candidates:
                segment = canonical[start:end]
                try:
                    embedding = self._embed_wav(encoder, segment, 16000)
                except ValueError:
                    continue
                item_scores = {speaker_id: float(np.dot(reference, embedding)) for speaker_id, reference in self._embeddings.items()}
                item: dict[str, object] = {
                    "start_seconds": round(start / 16000, 3), "end_seconds": round(end / 16000, 3),
                    "kind": kind, "scores": {self._profiles[key].name: value for key, value in item_scores.items()},
                }
                scored.append(item)
                for speaker_id, value in item_scores.items():
                    if value > score_by_id[speaker_id]:
                        score_by_id[speaker_id] = value
                        best_by_id[speaker_id] = item
            if not scored:
                raise ValueError("Audio sample is too short; provide at least 0.1 seconds of speech")
            best_id = max(score_by_id, key=score_by_id.__getitem__)
            best_score = score_by_id[best_id]
            # Compare alternatives on the same audio region. Independent peak
            # scores from different moments do not form a meaningful margin.
            winning_region = best_by_id[best_id]
            region_scores = winning_region["scores"]
            runner_up = max(
                (float(value) for name, value in region_scores.items()
                 if name != self._profiles[best_id].name),
                default=-1.0,
            )
            margin = best_score - runner_up
            effective_threshold = self._threshold if threshold is None else threshold
            effective_margin = self._min_margin if min_margin is None else min_margin
            detected_speakers = self._detect_multiple_speakers(
                scored, effective_threshold, effective_margin
            )
            if len(detected_speakers) > 1:
                outcome = "multiple_speakers"; match = None
                best_score = max(
                    float(item["confidence"]) for item in detected_speakers
                )
                margin = min(float(item["margin"]) for item in detected_speakers)
            elif best_score < effective_threshold:
                outcome = "unmatched"; match = None
            elif margin < effective_margin:
                outcome = "ambiguous"; match = None
            else:
                outcome = "matched"; match = self._profiles[best_id]
            recognition_finished = time.perf_counter()
            extracted: bytes | None = None
            extraction_status: str | None = None
            extraction_ms = 0.0
            if extract_for_speaker_id:
                extraction_started = time.perf_counter()
                extracted, extraction_status = self._extract_from_candidates(canonical, scored, extract_for_speaker_id, effective_threshold)
                extraction_ms = (time.perf_counter() - extraction_started) * 1000
            named_scores = {self._profiles[item].name: score for item, score in score_by_id.items()}
            return RecognitionAnalysis(
                speaker=match, confidence=best_score, scores=named_scores, threshold=effective_threshold,
                margin=margin, outcome=outcome, best_segment=(
                    None
                    if outcome == "multiple_speakers"
                    else self._public_segment(best_by_id.get(best_id))
                ),
                detected_speakers=detected_speakers, candidates=scored, timings={
                    "recognition_ms": round((recognition_finished - started) * 1000, 2),
                    "extraction_ms": round(extraction_ms, 2),
                },
                canonical_pcm=np.asarray(np.clip(canonical, -1, 0.9999695)*32768, dtype="<i2").tobytes(),
                extracted_pcm=extracted, extraction_status=extraction_status,
            )

    def _detect_multiple_speakers(
        self,
        candidates: list[dict[str, object]],
        threshold: float,
        min_margin: float,
    ) -> list[dict[str, object]]:
        """Find decisive different winners in non-overlapping speech regions."""
        profiles_by_name = {
            profile.name: profile for profile in self._profiles.values()
        }
        evidence: list[dict[str, object]] = []
        for candidate in candidates:
            if candidate.get("kind") == "utterance":
                continue
            start = float(candidate["start_seconds"])
            end = float(candidate["end_seconds"])
            if end - start < 0.5:
                continue
            scores = dict(candidate.get("scores") or {})
            ranked = sorted(
                (
                    (name, float(score))
                    for name, score in scores.items()
                    if name in profiles_by_name
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            if not ranked:
                continue
            name, confidence = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
            margin = confidence - runner_up
            if confidence < threshold or margin < min_margin:
                continue
            profile = profiles_by_name[name]
            evidence.append(
                {
                    "speaker_id": profile.id,
                    "speaker_name": profile.name,
                    "person_entity_id": profile.person_entity_id,
                    "confidence": confidence,
                    "margin": margin,
                    "start_seconds": start,
                    "end_seconds": end,
                    "kind": candidate.get("kind"),
                }
            )

        supported_ids: set[str] = set()
        for index, left in enumerate(evidence):
            for right in evidence[index + 1 :]:
                if left["speaker_id"] == right["speaker_id"]:
                    continue
                non_overlapping = (
                    float(left["end_seconds"]) <= float(right["start_seconds"])
                    or float(right["end_seconds"]) <= float(left["start_seconds"])
                )
                if non_overlapping:
                    supported_ids.update(
                        (str(left["speaker_id"]), str(right["speaker_id"]))
                    )
        if len(supported_ids) < 2:
            return []

        best_by_speaker: dict[str, dict[str, object]] = {}
        first_seen: dict[str, float] = {}
        for item in evidence:
            speaker_id = str(item["speaker_id"])
            if speaker_id not in supported_ids:
                continue
            first_seen[speaker_id] = min(
                first_seen.get(speaker_id, float(item["start_seconds"])),
                float(item["start_seconds"]),
            )
            if (
                speaker_id not in best_by_speaker
                or float(item["confidence"])
                > float(best_by_speaker[speaker_id]["confidence"])
            ):
                best_by_speaker[speaker_id] = item

        result: list[dict[str, object]] = []
        for speaker_id in sorted(best_by_speaker, key=first_seen.__getitem__):
            item = best_by_speaker[speaker_id]
            result.append(
                {
                    "speaker_id": item["speaker_id"],
                    "speaker_name": item["speaker_name"],
                    "person_entity_id": item["person_entity_id"],
                    "confidence": round(float(item["confidence"]), 6),
                    "margin": round(float(item["margin"]), 6),
                    "best_segment": {
                        "start_seconds": item["start_seconds"],
                        "end_seconds": item["end_seconds"],
                    },
                }
            )
        return result

    def _embed(self, encoder: Encoder, audio_input: AudioInput) -> NDArray[np.float32]:
        wav = self._decode_audio(audio_input)
        return self._embed_wav(encoder, wav, audio_input.sample_rate)

    def _embed_wav(self, encoder: Encoder, wav: NDArray[np.float32], sample_rate: int) -> NDArray[np.float32]:
        processed = self._preprocess(wav, sample_rate)
        if processed.size < max(1600, sample_rate // 10):
            raise ValueError("Audio sample is too short; provide at least 0.1 seconds of speech")
        embedding = np.asarray(encoder.embed_utterance(processed), dtype=np.float32)
        if embedding.ndim != 1 or not np.all(np.isfinite(embedding)):
            raise ValueError("Could not create a valid voice embedding")
        return self._normalize(embedding)

    @staticmethod
    def _canonicalize(wav: NDArray[np.float32], sample_rate: int) -> NDArray[np.float32]:
        if sample_rate == 16000:
            return np.asarray(wav, dtype=np.float32)
        return resample_audio(wav, sample_rate, 16000)

    @staticmethod
    def _candidate_regions(wav: NDArray[np.float32]) -> list[tuple[int, int, str]]:
        length = len(wav); candidates: list[tuple[int, int, str]] = [(0, length, "utterance")]
        frame = 320  # 20 ms at canonical 16 kHz
        energies = np.array([np.sqrt(np.mean(wav[index:index+frame] ** 2)) for index in range(0, length, frame)])
        if energies.size:
            floor = float(np.quantile(energies, 0.2))
            # A deliberately spoken test clip can have very even energy.  It
            # is speech, not silence; preserve it as one VAD region instead of
            # requiring a peak above its own noise floor.
            if float(np.max(energies)) >= 0.008 and float(np.ptp(energies)) < 0.005:
                speaking = np.ones_like(energies, dtype=bool)
            else:
                # A short silence may occupy exactly the lower quantile and
                # interpolation can otherwise lift ``floor`` close to speech
                # energy, eliminating every region. Cap the adaptive threshold
                # at half the observed peak so real pauses still split speakers.
                speaking = energies >= max(
                    0.008,
                    min(floor * 2.2, float(np.max(energies)) * 0.5),
                )
            start: int | None = None
            for index, active in enumerate(np.append(speaking, False)):
                if active and start is None: start = index
                elif not active and start is not None:
                    end = index
                    if (end - start) * frame >= 1600:
                        candidates.append((start * frame, min(length, end * frame), "vad"))
                    start = None
        # The first window catches short commands; remaining overlapping windows
        # make a clean later phrase available when the beginning is noisy.
        window = 40000; step = 16000
        if length > window:
            for start in range(0, length - 1600, step):
                candidates.append((start, min(length, start + window), "window"))
        unique: list[tuple[int, int, str]] = []
        bounds: set[tuple[int, int]] = set()
        for candidate in candidates:
            if candidate[1] - candidate[0] < 1600 or candidate[:2] in bounds: continue
            unique.append(candidate)
            bounds.add(candidate[:2])
        if len(unique) <= 12:
            return unique
        # Keep the full utterance and spread the remaining budget across time.
        # An early burst of VAD regions must not hide a clean speaker at the end.
        remaining = unique[1:]
        chosen: list[tuple[int, int, str]] = []
        for target in np.linspace(0, length, 11):
            if not remaining:
                break
            best = min(
                remaining,
                key=lambda item: (
                    abs((item[0] + item[1]) / 2 - target),
                    item[2] == "window",
                ),
            )
            chosen.append(best)
            remaining.remove(best)
        return [unique[0], *sorted(chosen, key=lambda item: item[0])]

    @staticmethod
    def _public_segment(item: dict[str, object] | None) -> dict[str, float] | None:
        if not item: return None
        return {"start_seconds": float(item["start_seconds"]), "end_seconds": float(item["end_seconds"])}

    def _extract_from_candidates(self, wav: NDArray[np.float32], candidates: list[dict[str, object]], speaker_id: str, threshold: float) -> tuple[bytes | None, str]:
        if speaker_id not in self._profiles: return None, "invalid_speaker"
        name = self._profiles[speaker_id].name
        regions: list[tuple[int, int]] = []
        for item in candidates:
            if item["kind"] != "vad": continue
            score = float(dict(item["scores"]).get(name, -1.0))
            if score >= threshold:
                regions.append((max(0, int(float(item["start_seconds"])*16000)-3200), min(len(wav), int(float(item["end_seconds"])*16000)+3200)))
        if not regions: return None, "no_matching_speech"
        regions.sort(); merged: list[list[int]] = []
        for start, end in regions:
            if merged and start - merged[-1][1] < 5600: merged[-1][1] = max(merged[-1][1], end)
            else: merged.append([start, end])
        pieces = [wav[start:end] for start, end in merged]
        extracted = np.concatenate([piece if index == 0 else np.concatenate((np.zeros(1600, dtype=np.float32), piece)) for index, piece in enumerate(pieces)])
        if extracted.size < 16000: return None, "too_short"
        return np.asarray(np.clip(extracted, -1, 0.9999695)*32768, dtype="<i2").tobytes(), "ready"

    def _decode_audio(self, audio_input: AudioInput) -> NDArray[np.float32]:
        raw = self._decode_pcm_bytes(audio_input)
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        if not np.any(pcm):
            raise ValueError("Audio is silent")
        return pcm / 32768.0

    def _decode_pcm_bytes(self, audio_input: AudioInput) -> bytes:
        try:
            raw = base64.b64decode(audio_input.audio_data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Audio data is not valid base64") from error
        if not raw or len(raw) % 2:
            raise ValueError("Audio must contain signed 16-bit PCM samples")
        max_bytes = audio_input.sample_rate * self._max_audio_seconds * 2
        if len(raw) > max_bytes:
            raise ValueError(f"Audio exceeds the {self._max_audio_seconds} second limit")
        return raw

    @staticmethod
    def _normalize(value: NDArray[np.float32]) -> NDArray[np.float32]:
        norm = float(np.linalg.norm(value))
        if norm <= 1e-12:
            raise ValueError("Voice embedding is empty")
        return np.asarray(value / norm, dtype=np.float32)

    def _normalized_mean(self, embeddings: list[NDArray[np.float32]]) -> NDArray[np.float32]:
        if not embeddings:
            raise ValueError("At least one valid voice embedding is required")
        ordered = sorted((np.asarray(item, dtype=np.float32) for item in embeddings), key=lambda item: item.tobytes())
        return self._normalize(np.mean(np.stack(ordered), axis=0, dtype=np.float64).astype(np.float32))

    def _active_sample_vectors(self, speaker_id: str, sample_ids: list[str]) -> list[NDArray[np.float32]]:
        """Load durable per-sample vectors, rebuilding legacy rows from WAV."""
        vectors: list[NDArray[np.float32]] = []
        legacy_vectors: list[NDArray[np.float32]] = []
        encoder = self._require_encoder()
        wanted = set(sample_ids)
        for sample in self.catalog.list_samples(speaker_id, include_internal=True):
            if sample["id"] not in wanted:
                continue
            metadata = sample.get("metadata") or {}
            if metadata.get("legacy_embedding") is not None:
                if (metadata.get("legacy_embedding_model") != EMBEDDING_MODEL_VERSION or
                        metadata.get("legacy_preprocess_version") != PREPROCESS_VERSION):
                    raise ValueError("Legacy profile vector uses a different model; new enrollment is required")
                vector = self._validate_embedding(np.asarray(metadata["legacy_embedding"], dtype=np.float32))
                count = metadata.get("legacy_sample_count", 1)
                if isinstance(count, int) and 1 <= count <= 10000:
                    legacy_vectors.extend([vector] * count)
                else:
                    raise ValueError("Stored legacy sample count is invalid")
            raw = metadata.get("embedding")
            if raw is not None and metadata.get("embedding_model") == EMBEDDING_MODEL_VERSION and metadata.get("preprocess_version") == PREPROCESS_VERSION:
                vector = self._validate_embedding(np.asarray(raw, dtype=np.float32))
            else:
                path = self.catalog.sample_path(sample["id"])
                if not path:
                    raise ValueError(f"Enrollment sample {sample['id']} has no readable audio or compatible vector")
                with wave.open(str(path), "rb") as handle:
                    if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                        raise ValueError("Enrollment audio must be mono 16-bit PCM")
                    pcm = handle.readframes(handle.getnframes())
                    vector = self._embed(encoder, AudioInput(audio_data=base64.b64encode(pcm).decode(), sample_rate=handle.getframerate()))
                self.catalog.store_sample_embedding(
                    sample["id"], vector.tolist(), EMBEDDING_MODEL_VERSION, PREPROCESS_VERSION
                )
            vectors.append(vector)
        if len(vectors) != len(wanted):
            raise ValueError("An enrollment sample is missing")
        if len(legacy_vectors) > 1:
            raise ValueError("Enrollment data contains duplicate legacy vectors")
        return legacy_vectors + vectors

    def _recover_profile_revisions(self) -> None:
        """Reconcile the cached profile against the last committed sample set."""
        changed = False
        for speaker_id, profile in list(self._profiles.items()):
            all_samples = self.catalog.list_samples(speaker_id, include_internal=True)
            active = [sample for sample in all_samples if sample["active"]]
            if not active and all_samples:
                # A newly staged first revision can be interrupted after its
                # profile files are written but before the SQLite activation.
                active = all_samples
                try:
                    self.catalog.replace_active_samples(speaker_id, [item["id"] for item in active])
                except (OSError, ValueError):
                    _LOGGER.exception("Could not recover staged profile revision %s", speaker_id)
                    continue
            if not active:
                continue  # Older profile without recoverable WAVs remains valid.
            try:
                vectors = self._active_sample_vectors(speaker_id, [item["id"] for item in active])
                embedding = self._normalized_mean(vectors)
                corrected = profile.model_copy(update={"sample_count": len(vectors)})
                if corrected.sample_count != profile.sample_count or not np.allclose(embedding, self._embeddings[speaker_id], rtol=0, atol=1e-7):
                    self._write_embedding(speaker_id, embedding)
                    self._profiles[speaker_id] = corrected
                    self._embeddings[speaker_id] = embedding
                    changed = True
            except (OSError, ValueError, wave.Error):
                _LOGGER.exception("Could not reconcile profile %s from enrollment samples", speaker_id)
        if changed:
            self._write_registry()

    @classmethod
    def _validate_embedding(cls, value: NDArray[np.float32]) -> NDArray[np.float32]:
        if value.ndim != 1 or not 1 <= value.size <= 4096 or not np.all(np.isfinite(value)):
            raise ValueError("Stored voice vector is invalid")
        return cls._normalize(value)

    def _require_encoder(self) -> Encoder:
        if self._encoder is None:
            raise RuntimeError("Recognition engine is still starting")
        return self._encoder

    def _load_profiles(self) -> None:
        self._profiles.clear()
        self._embeddings.clear()
        if not self._registry_path.exists():
            return
        try:
            entries = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            _LOGGER.error("Could not load speaker registry: %s", error)
            return
        for entry in entries:
            try:
                profile = SpeakerInfo.model_validate(entry)
                embedding_path = self._profiles_dir / f"{profile.id}.npy"
                embedding = self._validate_embedding(np.asarray(np.load(embedding_path, allow_pickle=False), dtype=np.float32))
                self._profiles[profile.id] = profile
                self._embeddings[profile.id] = embedding
            except (OSError, ValueError, json.JSONDecodeError) as error:
                _LOGGER.error("Skipping invalid speaker profile: %s", error)

    def _write_embedding(self, speaker_id: str, embedding: NDArray[np.float32]) -> None:
        destination = self._profiles_dir / f"{speaker_id}.npy"
        temporary = destination.with_suffix(".npy.tmp")
        with temporary.open("wb") as handle:
            np.save(handle, embedding, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)

    def _write_registry(self) -> None:
        temporary = self._registry_path.with_suffix(".json.tmp")
        payload = [profile.model_dump(mode="json") for profile in self._profiles.values()]
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self._registry_path)
