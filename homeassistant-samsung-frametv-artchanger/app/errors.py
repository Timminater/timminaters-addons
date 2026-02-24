from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class AppError(Exception):
    message: str
    code: str
    status: int
    retryable: bool = False

    def as_dict(self, request_id: str) -> Dict[str, Any]:
        return {
            "detail": self.message,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": request_id,
            },
        }


class InvalidInputError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="INVALID_INPUT", status=400, retryable=False)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Unauthorized") -> None:
        super().__init__(message=message, code="UNAUTHORIZED", status=401, retryable=False)


class NotFoundError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="NOT_FOUND", status=404, retryable=False)


class OperationError(AppError):
    pass


class NoRandomAssetsError(AppError):
    def __init__(self, message: str = "No local gallery images are available for random selection") -> None:
        super().__init__(message=message, code="NO_RANDOM_ASSETS", status=409, retryable=False)


class InternalError(AppError):
    def __init__(self, message: str = "Internal server error") -> None:
        super().__init__(message=message, code="INTERNAL_ERROR", status=500, retryable=False)


def error_payload(code: str, message: str, retryable: bool, request_id: str) -> Dict[str, Any]:
    return {
        "detail": message,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
        },
    }


def classify_operation_exception(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, AppError):
        return {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        }

    message = str(exc)
    lowered = message.lower()

    if any(token in lowered for token in ("timed out", "timeout", "connection", "refused", "unreachable", "reset")):
        return {
            "code": "TV_OFFLINE",
            "message": message,
            "retryable": True,
        }

    return {
        "code": "INTERNAL_ERROR",
        "message": message,
        "retryable": False,
    }
