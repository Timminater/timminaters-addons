from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


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
