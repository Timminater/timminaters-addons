"""Persistent, thread-safe speaker profile storage and recognition."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import uuid
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
        self._audio_processor = TargetAudioProcessor()

    @property
    def ready(self) -> bool:
        return self._encoder is not None

    def initialize(self) -> None:
        with self._lock:
            self._profiles_dir.mkdir(parents=True, exist_ok=True)
            self.catalog.initialize()
            self._load_profiles()
            self._encoder = self._encoder_factory()
            _LOGGER.info("Recognition engine ready with %d speaker(s)", len(self._profiles))

    def list_speakers(self) -> list[SpeakerInfo]:
        with self._lock:
            return sorted(self._profiles.values(), key=lambda item: item.name.casefold())

    def close(self) -> None:
        """Release the optional model worker without affecting profile storage."""
        self._audio_processor.close()

    def enroll(
        self,
        speaker_name: str,
        audio_inputs: list[AudioInput],
        replace: bool = False,
        person_entity_id: str | None = None,
        update_person_mapping: bool = False,
    ) -> SpeakerInfo:
        with self._lock:
            encoder = self._require_encoder()
            existing = next(
                (profile for profile in self._profiles.values() if profile.name.casefold() == speaker_name.casefold()),
                None,
            )
            embeddings = [self._embed(encoder, audio) for audio in audio_inputs]
            new_embedding = self._normalized_mean(embeddings)
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
                )
            else:
                speaker_id = existing.id
                if not replace:
                    old_weight = existing.sample_count
                    combined = (self._embeddings[speaker_id] * old_weight) + (new_embedding * len(embeddings))
                    new_embedding = self._normalize(combined)
                profile = SpeakerInfo(
                    id=speaker_id,
                    name=speaker_name,
                    sample_count=len(embeddings) if replace else existing.sample_count + len(embeddings),
                    created_at=existing.created_at,
                    updated_at=now,
                    person_entity_id=(
                        person_entity_id
                        if update_person_mapping
                        else existing.person_entity_id
                    ),
                )

            previous_profile = self._profiles.get(speaker_id)
            previous_embedding = self._embeddings.get(speaker_id)
            self._write_embedding(speaker_id, new_embedding)
            self._profiles[speaker_id] = profile
            self._embeddings[speaker_id] = new_embedding
            try:
                self._write_registry()
            except OSError:
                if previous_profile is None or previous_embedding is None:
                    (self._profiles_dir / f"{speaker_id}.npy").unlink(missing_ok=True)
                    self._profiles.pop(speaker_id, None)
                    self._embeddings.pop(speaker_id, None)
                else:
                    self._write_embedding(speaker_id, previous_embedding)
                    self._profiles[speaker_id] = previous_profile
                    self._embeddings[speaker_id] = previous_embedding
                raise
            # Raw samples are intentionally permanent.  Old profiles from
            # 1.x stay valid even though they have no reconstructable WAV.
            if replace:
                for sample in self.catalog.list_samples(speaker_id, active_only=True):
                    self.catalog.set_sample_active(sample["id"], False)
            for audio in audio_inputs:
                pcm = self._decode_pcm_bytes(audio)
                self.catalog.add_sample(speaker_id, pcm, audio.sample_rate, metadata={"source": "enroll"})
            return profile

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
            embedding = self._normalized_mean([self._embed(self._require_encoder(), item) for item in inputs])
            updated = SpeakerInfo(id=profile.id, name=profile.name, sample_count=len(inputs), created_at=profile.created_at, updated_at=datetime.now(timezone.utc), person_entity_id=profile.person_entity_id)
            self._write_embedding(speaker_id, embedding)
            self._profiles[speaker_id] = updated; self._embeddings[speaker_id] = embedding
            self._write_registry()
            return updated

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

    def process_target_audio(
        self,
        audio_input: AudioInput,
        speaker_id: str,
        *,
        timeout_seconds: float = 12,
        priority: str = "live",
        min_margin: float | None = None,
    ) -> ProcessedAudioResult:
        """Denoise and isolate one enrolled speaker, retaining safe fallbacks."""
        with self._lock:
            if speaker_id not in self._profiles:
                raise KeyError(speaker_id)
            original = self._canonicalize(
                self._decode_audio(audio_input), audio_input.sample_rate
            )
            reference = self._reference_audio(speaker_id)
            embeddings = {
                key: np.asarray(value, dtype=np.float32).copy()
                for key, value in self._embeddings.items()
            }
            threshold = self._threshold
            required_margin = self._min_margin if min_margin is None else min_margin

        result = self._audio_processor.process(
            original,
            reference,
            timeout_seconds=timeout_seconds,
            priority=priority,
        )
        quality = dict(result.quality)
        denoised = self._pcm_array(result.denoised_pcm)
        isolated = self._pcm_array(result.isolated_pcm)
        fallback_reason = result.fallback_reason
        stages = dict(result.stages)
        original_scores = self._speaker_scores(original, embeddings)
        original_target_score = original_scores.get(speaker_id, -1.0)
        quality["original_target_score"] = round(original_target_score, 6)
        denoised_target_score = -1.0

        # Denoising may alter the embedding. A confident conflicting identity
        # is never allowed to select or condition a different target speaker.
        if denoised is not None:
            denoised_scores = self._speaker_scores(denoised, embeddings)
            target_score = denoised_scores.get(speaker_id, -1.0)
            denoised_target_score = target_score
            best_id = max(denoised_scores, key=denoised_scores.__getitem__)
            runner_up = max(
                (
                    score
                    for candidate_id, score in denoised_scores.items()
                    if candidate_id != speaker_id
                ),
                default=-1.0,
            )
            quality["denoised_target_score"] = round(target_score, 6)
            denoised_safe = not (
                best_id != speaker_id and denoised_scores[best_id] >= threshold
            ) and target_score - runner_up >= required_margin
            quality["denoised_safe_for_stt"] = denoised_safe
            if not denoised_safe:
                isolated = None
                stages["isolation"] = "rejected_identity_conflict"
                fallback_reason = "denoised_identity_conflict"

        if isolated is not None:
            isolated_scores = self._speaker_scores(isolated, embeddings)
            target_score = isolated_scores.get(speaker_id, -1.0)
            best_id = max(isolated_scores, key=isolated_scores.__getitem__)
            runner_up = max(
                (
                    score
                    for candidate_id, score in isolated_scores.items()
                    if candidate_id != speaker_id
                ),
                default=-1.0,
            )
            quality["isolated_target_score"] = round(target_score, 6)
            quality["isolated_margin"] = round(target_score - runner_up, 6)
            # Speaker separation changes the embedding more strongly than
            # denoising. Require a modest score on the separated signal plus
            # independent evidence for the requested profile in the source or
            # denoised signal. This rejects an absent target without discarding
            # otherwise clean SpEx+ output.
            source_evidence = max(original_target_score, denoised_target_score)
            minimum_source_evidence = max(0.50, threshold - 0.10)
            quality["source_target_evidence"] = round(source_evidence, 6)
            quality["source_evidence_required"] = round(
                minimum_source_evidence, 6
            )
            if (
                best_id != speaker_id
                or target_score < 0.45
                or target_score - runner_up < required_margin
                or source_evidence < minimum_source_evidence
            ):
                isolated = None
                stages["isolation"] = "rejected_speaker_validation"
                fallback_reason = "isolated_speaker_validation_failed"

        return ProcessedAudioResult(
            denoised_pcm=result.denoised_pcm,
            isolated_pcm=(
                np.asarray(np.clip(isolated, -1, 0.9999695) * 32768, dtype="<i2").tobytes()
                if isolated is not None
                else None
            ),
            sample_rate=result.sample_rate,
            stages=stages,
            timings=result.timings,
            quality=quality,
            fallback_reason=fallback_reason,
        )

    def denoise_audio(
        self,
        audio_input: AudioInput,
        *,
        timeout_seconds: float = 12,
        priority: str = "live",
    ) -> ProcessedAudioResult:
        """Run only the general enhancement stage for an unknown speaker."""
        original = self._canonicalize(
            self._decode_audio(audio_input), audio_input.sample_rate
        )
        result = self._audio_processor.process(
            original,
            np.empty(0, dtype=np.float32),
            timeout_seconds=timeout_seconds,
            priority=priority,
        )
        stages = dict(result.stages)
        if stages.get("isolation") == "missing_reference":
            stages["isolation"] = "not_requested"
        return ProcessedAudioResult(
            denoised_pcm=result.denoised_pcm,
            isolated_pcm=None,
            sample_rate=result.sample_rate,
            stages=stages,
            timings=result.timings,
            quality=result.quality,
            fallback_reason=(
                None if result.denoised_pcm is not None else result.fallback_reason
            ),
        )

    def _reference_audio(self, speaker_id: str) -> NDArray[np.float32]:
        """Build a normalized, deterministic reference capped at 30 seconds."""
        pieces: list[NDArray[np.float32]] = []
        remaining = 30 * CANONICAL_RATE
        for sample in reversed(self.catalog.list_samples(speaker_id, active_only=True)):
            path = self.catalog.sample_path(sample["id"])
            if not path or remaining <= 0:
                continue
            try:
                with wave.open(str(path), "rb") as handle:
                    if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                        continue
                    raw = np.frombuffer(
                        handle.readframes(handle.getnframes()), dtype="<i2"
                    ).astype(np.float32) / 32768.0
                    value = resample_audio(raw, handle.getframerate(), CANONICAL_RATE)
            except (OSError, wave.Error, ValueError):
                continue
            speech_regions = [
                value[start:end]
                for start, end, kind in self._candidate_regions(value)
                if kind == "vad"
            ]
            if speech_regions:
                value = np.concatenate(
                    [
                        region
                        if index == 0
                        else np.concatenate(
                            (np.zeros(1600, dtype=np.float32), region)
                        )
                        for index, region in enumerate(speech_regions)
                    ]
                )
            value = value - float(np.mean(value))
            peak = float(np.max(np.abs(value))) if value.size else 0.0
            if value.size < CANONICAL_RATE or peak < 0.003:
                continue
            value = value * min(4.0, 0.85 / peak)
            value = value[:remaining]
            pieces.append(value)
            remaining -= value.size
            if remaining >= 1600:
                pieces.append(np.zeros(min(1600, remaining), dtype=np.float32))
                remaining -= min(1600, remaining)
        if not pieces:
            raise ValueError("No usable active enrollment WAV is available")
        return np.concatenate(pieces)[: 30 * CANONICAL_RATE]

    @staticmethod
    def _pcm_array(pcm: bytes | None) -> NDArray[np.float32] | None:
        if not pcm:
            return None
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

    def _speaker_scores(
        self,
        audio: NDArray[np.float32],
        embeddings: dict[str, NDArray[np.float32]],
    ) -> dict[str, float]:
        try:
            vector = self._embed_wav(self._require_encoder(), audio, CANONICAL_RATE)
        except ValueError:
            return {speaker_id: -1.0 for speaker_id in embeddings}
        return {
            speaker_id: float(np.dot(reference, vector))
            for speaker_id, reference in embeddings.items()
        }

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
            sorted_scores = sorted(score_by_id.values(), reverse=True)
            margin = best_score - (sorted_scores[1] if len(sorted_scores) > 1 else -1.0)
            effective_threshold = self._threshold if threshold is None else threshold
            effective_margin = self._min_margin if min_margin is None else min_margin
            if best_score < effective_threshold:
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
                margin=margin, outcome=outcome, best_segment=self._public_segment(best_by_id.get(best_id)),
                candidates=scored, timings={
                    "recognition_ms": round((recognition_finished - started) * 1000, 2),
                    "extraction_ms": round(extraction_ms, 2),
                },
                canonical_pcm=np.asarray(np.clip(canonical, -1, 0.9999695)*32768, dtype="<i2").tobytes(),
                extracted_pcm=extracted, extraction_status=extraction_status,
            )

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
        count = max(1, round(wav.size * 16000 / sample_rate))
        positions = np.linspace(0, max(0, wav.size - 1), count)
        return np.asarray(np.interp(positions, np.arange(wav.size), wav), dtype=np.float32)

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
                speaking = energies >= max(0.008, floor * 2.2)
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
        for candidate in candidates:
            if candidate[1] - candidate[0] < 1600 or candidate in unique: continue
            unique.append(candidate)
            if len(unique) == 12: break
        return unique

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
        return self._normalize(np.mean(np.stack(embeddings), axis=0, dtype=np.float32))

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
                embedding = np.asarray(np.load(embedding_path, allow_pickle=False), dtype=np.float32)
                embedding = self._normalize(embedding)
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
