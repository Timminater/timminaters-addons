from __future__ import annotations

from contextvars import ContextVar
from typing import Optional


_request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    return _request_id_var.get()


def clear_request_id() -> None:
    _request_id_var.set(None)
