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
    person_entity_id: str | None = Field(
        default=None, pattern=r"^person\.[a-z0-9_]+$"
    )

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
    person_entity_id: str | None = None


class HomeAssistantPersonInfo(BaseModel):
    entity_id: str
    name: str


class EnrollmentResult(BaseModel):
    status: str = "success"
    speaker: SpeakerInfo


class AssistSatelliteInfo(BaseModel):
    entity_id: str
    name: str
    state: str


class SatelliteEnrollmentStartRequest(BaseModel):
    satellite_entity_id: str = Field(pattern=r"^assist_satellite\.[a-z0-9_]+$")
    start_mode: Literal["button", "remote"] = "remote"


class SatelliteEnrollmentClaimRequest(BaseModel):
    """Identify the satellite observed locally when its STT stream began."""

    satellite_entity_id: str | None = Field(
        default=None, pattern=r"^assist_satellite\.[a-z0-9_]+$"
    )


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


# v2 analysis contracts.  They intentionally use the same PCM input as the
# companion integration, avoiding WAV parsing ambiguity between add-on and HA.
class PipelinePolicy(BaseModel):
    unknown_speaker_policy: Literal["allow", "block"] = "allow"
    extraction_mode: Literal["off", "compare", "before_stt"] = "off"
    recognition_threshold: float = Field(ge=0, le=1)
    min_margin: float = Field(default=0.0, ge=0, le=2)
    retention_days: int = Field(default=7, ge=1, le=365)
    max_storage_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    calibration: dict | None = None


class PipelinePolicyPatch(BaseModel):
    unknown_speaker_policy: Literal["allow", "block"] | None = None
    extraction_mode: Literal["off", "compare", "before_stt"] | None = None
    min_margin: float | None = Field(default=None, ge=0, le=2)


class AnalyzeRequest(BaseModel):
    audio: AudioInput
    source: Literal["pipeline", "test", "home_assistant_stt"] = "pipeline"
    satellite_id: str | None = Field(default=None, max_length=255)
    stt_entity_id: str | None = Field(default=None, max_length=255)
    extraction_mode: Literal["off", "compare", "before_stt"] | None = None


class FinalizeRecordingRequest(BaseModel):
    transcript: str | None = Field(default=None, max_length=10000)
    outcome: Literal["matched", "unmatched", "ambiguous", "error", "blocked"] | None = None
    stt_entity_id: str | None = Field(default=None, max_length=255)
    timings: dict[str, float] | None = None
    conversation_forwarded: bool | None = None
    # ``extracted`` remains accepted for a 2.0 integration during upgrade.
    audio_variant: Literal["original", "denoised", "isolated", "extracted"] | None = None
    fallback: bool | None = None
    fallback_reason: str | None = Field(default=None, max_length=300)
    quality: dict[str, object] | None = None


class ConversationRecordingRequest(BaseModel):
    conversation_forwarded: bool
    person_entity_id: str | None = Field(default=None, pattern=r"^person\.[a-z0-9_]+$")
    conversation_reason: str | None = Field(default=None, max_length=300)
    timings: dict[str, float] | None = None


class ExtractRequest(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=64)


class ProcessTargetAudioRequest(BaseModel):
    """Queue real target-speaker processing for a persisted recording."""

    speaker_id: str = Field(min_length=1, max_length=64)


class PromoteRecordingRequest(BaseModel):
    speaker_id: str | None = Field(default=None, min_length=1, max_length=64)
    new_speaker_name: str | None = Field(default=None, min_length=1, max_length=80)
    person_entity_id: str | None = Field(default=None, pattern=r"^person\.[a-z0-9_]+$")
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)


class BulkDeleteRequest(BaseModel):
    ids: list[str] | None = Field(default_factory=list, max_length=500)
    filters: dict[str, str] | None = None
    all_filtered: bool = False


class SampleActiveRequest(BaseModel):
    active: bool


class DeleteSpeakerRequest(BaseModel):
    audio_action: Literal["delete", "archive"] = "delete"


class CalibrationApplyRequest(BaseModel):
    threshold: float = Field(ge=0, le=1)
    margin: float = Field(default=0.0, ge=0, le=2)
