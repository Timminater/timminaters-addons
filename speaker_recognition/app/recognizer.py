"""Persistent, thread-safe speaker profile storage and recognition."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
from numpy.typing import NDArray

from app.models import AudioInput, SpeakerInfo

_LOGGER = logging.getLogger(__name__)


class Encoder(Protocol):
    def embed_utterance(self, wav: NDArray[np.float32]) -> NDArray[np.float32]: ...


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
    ) -> None:
        self._profiles_dir = data_dir / "speakers"
        self._registry_path = self._profiles_dir / "registry.json"
        self._threshold = threshold
        self._max_audio_seconds = max_audio_seconds
        self._encoder_factory = encoder_factory
        self._preprocess = preprocess
        self._encoder: Encoder | None = None
        self._profiles: dict[str, SpeakerInfo] = {}
        self._embeddings: dict[str, NDArray[np.float32]] = {}
        self._lock = threading.RLock()

    @property
    def ready(self) -> bool:
        return self._encoder is not None

    def initialize(self) -> None:
        with self._lock:
            self._profiles_dir.mkdir(parents=True, exist_ok=True)
            self._load_profiles()
            self._encoder = self._encoder_factory()
            _LOGGER.info("Recognition engine ready with %d speaker(s)", len(self._profiles))

    def list_speakers(self) -> list[SpeakerInfo]:
        with self._lock:
            return sorted(self._profiles.values(), key=lambda item: item.name.casefold())

    def enroll(
        self, speaker_name: str, audio_inputs: list[AudioInput], replace: bool = False
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
                )

            self._write_embedding(speaker_id, new_embedding)
            self._profiles[speaker_id] = profile
            self._embeddings[speaker_id] = new_embedding
            self._write_registry()
            return profile

    def delete(self, speaker_id: str) -> bool:
        with self._lock:
            if speaker_id not in self._profiles:
                return False
            (self._profiles_dir / f"{speaker_id}.npy").unlink(missing_ok=True)
            del self._profiles[speaker_id]
            self._embeddings.pop(speaker_id, None)
            self._write_registry()
            return True

    def recognize(self, audio_input: AudioInput) -> tuple[SpeakerInfo | None, float, dict[str, float]]:
        with self._lock:
            if not self._profiles:
                raise RuntimeError("No speakers have been enrolled")
            embedding = self._embed(self._require_encoder(), audio_input)
            scores_by_id = {
                speaker_id: float(np.dot(reference, embedding))
                for speaker_id, reference in self._embeddings.items()
            }
            best_id = max(scores_by_id, key=scores_by_id.__getitem__)
            best_score = scores_by_id[best_id]
            named_scores = {self._profiles[item].name: score for item, score in scores_by_id.items()}
            match = self._profiles[best_id] if best_score >= self._threshold else None
            return match, best_score, named_scores

    def _embed(self, encoder: Encoder, audio_input: AudioInput) -> NDArray[np.float32]:
        wav = self._decode_audio(audio_input)
        processed = self._preprocess(wav, audio_input.sample_rate)
        if processed.size < max(1600, audio_input.sample_rate // 10):
            raise ValueError("Audio sample is too short; provide at least 0.1 seconds of speech")
        embedding = np.asarray(encoder.embed_utterance(processed), dtype=np.float32)
        if embedding.ndim != 1 or not np.all(np.isfinite(embedding)):
            raise ValueError("Could not create a valid voice embedding")
        return self._normalize(embedding)

    def _decode_audio(self, audio_input: AudioInput) -> NDArray[np.float32]:
        try:
            raw = base64.b64decode(audio_input.audio_data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Audio data is not valid base64") from error
        if not raw or len(raw) % 2:
            raise ValueError("Audio must contain signed 16-bit PCM samples")
        max_bytes = audio_input.sample_rate * self._max_audio_seconds * 2
        if len(raw) > max_bytes:
            raise ValueError(f"Audio exceeds the {self._max_audio_seconds} second limit")
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        if not np.any(pcm):
            raise ValueError("Audio is silent")
        return pcm / 32768.0

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
            for entry in entries:
                profile = SpeakerInfo.model_validate(entry)
                embedding_path = self._profiles_dir / f"{profile.id}.npy"
                embedding = np.asarray(np.load(embedding_path, allow_pickle=False), dtype=np.float32)
                self._profiles[profile.id] = profile
                self._embeddings[profile.id] = self._normalize(embedding)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            _LOGGER.error("Could not load speaker profiles: %s", error)
            self._profiles.clear()
            self._embeddings.clear()

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
