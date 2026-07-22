"""HTTP request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AudioInput(BaseModel):
    audio_data: str = Field(min_length=1, description="Base64 encoded signed 16-bit mono PCM")
    sample_rate: int = Field(default=16000, ge=8000, le=48000)


class VoiceSample(BaseModel):
    audio: AudioInput


class EnrollmentRequest(BaseModel):
    speaker_name: str = Field(min_length=1, max_length=80)
    samples: list[VoiceSample] = Field(min_length=1, max_length=10)
    replace: bool = False

    @field_validator("speaker_name")
    @classmethod
    def clean_speaker_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or any(ord(character) < 32 for character in cleaned):
            raise ValueError("Speaker name contains invalid characters")
        return cleaned


class RecognitionRequest(BaseModel):
    audio: AudioInput


class SpeakerInfo(BaseModel):
    id: str
    name: str
    sample_count: int
    created_at: datetime
    updated_at: datetime


class EnrollmentResult(BaseModel):
    status: str = "success"
    speaker: SpeakerInfo


class AssistSatelliteInfo(BaseModel):
    entity_id: str
    name: str
    state: str


class SatelliteEnrollmentStartRequest(BaseModel):
    satellite_entity_id: str = Field(pattern=r"^assist_satellite\.[a-z0-9_]+$")


class SatelliteEnrollmentCompleteRequest(BaseModel):
    audio: AudioInput


class SatelliteEnrollmentFailureRequest(BaseModel):
    error: str = Field(default="Opname mislukt", max_length=300)


class SatelliteEnrollmentSession(BaseModel):
    id: str
    satellite_entity_id: str
    status: Literal["armed", "capturing", "complete", "failed", "cancelled", "expired"]
    created_at: datetime
    expires_at: datetime
    error: str | None = None
    audio: AudioInput | None = None


class SatelliteEnrollmentClaim(BaseModel):
    session: SatelliteEnrollmentSession | None = None


class RecognitionResult(BaseModel):
    matched: bool
    speaker: SpeakerInfo | None
    confidence: float
    threshold: float
    scores: dict[str, float]


class HealthResponse(BaseModel):
    status: str
    ready: bool
    speakers: int
