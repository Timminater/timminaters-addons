"""Bounded multipart reader for mono signed 16-bit PCM recordings.

The request is accumulated only up to ``MAX_MULTIPART_BYTES`` and then parsed
with Python's MIME parser. This keeps parsing dependency-free while bounding
memory and rejecting malformed/oversized inputs before MIME decoding.
"""

from __future__ import annotations

import email.policy
import re
from collections.abc import AsyncIterable
from dataclasses import dataclass
from email.parser import BytesParser


MAX_MULTIPART_BYTES = 40 * 1024 * 1024
MAX_RECORDINGS = 8
MAX_RECORDING_BYTES = 8 * 1024 * 1024
MAX_RECORDING_SECONDS = 30
MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 96_000
_BOUNDARY_RE = re.compile(r"^[0-9A-Za-z'()+_,./:=?-]{1,70}$")


class MultipartPcmError(ValueError):
    """Invalid or over-limit multipart PCM request."""


@dataclass(frozen=True, slots=True)
class PcmRecording:
    """One validated mono pcm_s16le part."""

    filename: str
    sample_rate: int
    pcm: bytes


async def read_multipart_pcm(
    chunks: AsyncIterable[bytes],
    content_type: str | None,
    sample_rate: int,
    *,
    max_request_bytes: int = MAX_MULTIPART_BYTES,
    max_recordings: int = MAX_RECORDINGS,
    max_recording_bytes: int = MAX_RECORDING_BYTES,
) -> list[PcmRecording]:
    """Read and validate repeated ``recordings`` file parts from a stream.

    Each part must be a binary/8-bit ``application/octet-stream`` or
    ``audio/pcm`` attachment named ``recordings``. The caller supplies the
    shared sample rate as request metadata; channels and sample format are
    fixed to mono ``pcm_s16le`` by this contract.
    """
    if not isinstance(content_type, str):
        raise MultipartPcmError("missing_content_type")
    try:
        header = BytesParser(policy=email.policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
                "ascii"
            )
        )
    except (UnicodeEncodeError, ValueError) as error:
        raise MultipartPcmError("invalid_content_type") from error
    boundary = header.get_boundary()
    if (
        header.get_content_type() != "multipart/form-data"
        or not boundary
        or not _BOUNDARY_RE.fullmatch(boundary)
    ):
        raise MultipartPcmError("invalid_multipart_boundary")
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
    ):
        raise MultipartPcmError("invalid_sample_rate")
    if max_request_bytes <= 0 or max_recordings <= 0 or max_recording_bytes <= 0:
        raise ValueError("limits must be positive")

    body = bytearray()
    async for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise MultipartPcmError("invalid_body_chunk")
        if len(body) + len(chunk) > max_request_bytes:
            raise MultipartPcmError("request_too_large")
        body.extend(chunk)
    if not body:
        raise MultipartPcmError("empty_request")

    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
            "ascii"
        )
        + body
    )
    try:
        message = BytesParser(policy=email.policy.default).parsebytes(envelope)
    except (ValueError, TypeError) as error:
        raise MultipartPcmError("malformed_multipart") from error
    if not message.is_multipart():
        raise MultipartPcmError("malformed_multipart")

    recordings: list[PcmRecording] = []
    total_audio_bytes = 0
    for part in message.iter_parts():
        if part.is_multipart():
            raise MultipartPcmError("nested_multipart_not_allowed")
        disposition = part.get_content_disposition()
        if disposition != "form-data" or part.get_param(
            "name", header="content-disposition"
        ) != "recordings":
            raise MultipartPcmError("unexpected_multipart_field")
        filename = part.get_filename()
        if not filename:
            raise MultipartPcmError("recording_filename_required")
        if part.get_content_type() not in {
            "application/octet-stream",
            "audio/pcm",
        }:
            raise MultipartPcmError("unsupported_audio_content_type")
        transfer_encoding = (part.get("Content-Transfer-Encoding") or "binary").lower()
        if transfer_encoding not in {"binary", "8bit"}:
            raise MultipartPcmError("unsupported_transfer_encoding")
        if len(recordings) >= max_recordings:
            raise MultipartPcmError("too_many_recordings")
        pcm = part.get_payload(decode=True)
        if not isinstance(pcm, bytes) or not pcm:
            raise MultipartPcmError("empty_recording")
        if len(pcm) > max_recording_bytes:
            raise MultipartPcmError("recording_too_large")
        if len(pcm) % 2:
            raise MultipartPcmError("invalid_pcm_alignment")
        if len(pcm) > sample_rate * 2 * MAX_RECORDING_SECONDS:
            raise MultipartPcmError("recording_too_long")
        total_audio_bytes += len(pcm)
        # Filenames are only used as display labels. Generate them locally so
        # client-supplied paths, control characters, or MIME quoting cannot
        # become filesystem paths or misleading labels.
        safe_filename = f"recording-{len(recordings) + 1}.pcm"
        recordings.append(PcmRecording(safe_filename, sample_rate, pcm))

    if not recordings:
        raise MultipartPcmError("no_recordings")
    if total_audio_bytes > max_request_bytes:
        raise MultipartPcmError("request_too_large")
    return recordings
