from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class CropPayload(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    rotation: float = Field(default=0.0, ge=-45.0, le=45.0)
    quarter_turns: int = Field(default=0, ge=0, le=3)
    flip_horizontal: bool = False

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.x is not None:
            payload["x"] = float(self.x)
        if self.y is not None:
            payload["y"] = float(self.y)
        if self.width is not None:
            payload["width"] = float(self.width)
        if self.height is not None:
            payload["height"] = float(self.height)
        payload["rotation"] = float(self.rotation)
        payload["quarter_turns"] = int(self.quarter_turns)
        payload["flip_horizontal"] = bool(self.flip_horizontal)
        return payload


class ActivateRequest(BaseModel):
    tv_ips: Optional[List[str]] = None
    ensure_upload: bool = True
    activate: bool = True


class DeleteRequest(BaseModel):
    targets: Literal["tv", "ha", "both"] = Field(default="both")
    tv_ips: Optional[List[str]] = None


class RandomRequest(BaseModel):
    tv_ips: Optional[List[str]] = None
    ensure_upload: bool = True
    activate: bool = True


class SettingsUpdateRequest(BaseModel):
    tv_ips: List[str] = Field(default_factory=list)
    refresh_interval_seconds: int = Field(default=30, ge=5, le=3600)
    snapshot_ttl_seconds: int = Field(default=20, ge=1, le=600)


class DiscoveryRequest(BaseModel):
    subnet: Optional[str] = None


class ApiMeta(BaseModel):
    stale: bool
    refresh_in_progress: bool
    last_refresh: Optional[str]
    request_id: str


class ApiError(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str


class OperationResult(BaseModel):
    ok: bool
    code: Optional[str] = None
    message: Optional[str] = None
    retryable: Optional[bool] = None
    raw: Optional[Dict[str, Any]] = None
