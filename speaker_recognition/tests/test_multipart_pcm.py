from __future__ import annotations

import asyncio

import pytest

from app.multipart_pcm import MultipartPcmError, read_multipart_pcm


BOUNDARY = "----speaker-test-boundary"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def multipart(*parts: tuple[str, bytes], content_type: str = "application/octet-stream") -> bytes:
    chunks = []
    for filename, pcm in parts:
        chunks.extend(
            [
                f"--{BOUNDARY}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="recordings"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                pcm,
                b"\r\n",
            ]
        )
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


async def chunks(data: bytes):
    for start in range(0, len(data), 7):
        yield data[start : start + 7]


def read(data: bytes, sample_rate: int = 16_000, **kwargs):
    return asyncio.run(
        read_multipart_pcm(chunks(data), CONTENT_TYPE, sample_rate, **kwargs)
    )


def test_reads_multiple_binary_recordings_from_split_chunks():
    body = multipart(
        ("first.wav", b"\x01\x00\x02\x00"),
        ("folder\\second.pcm", b"\xff\x7f\x00\x80"),
    )

    recordings = read(body)

    assert [(item.filename, item.sample_rate, item.pcm) for item in recordings] == [
        ("recording-1.pcm", 16_000, b"\x01\x00\x02\x00"),
        ("recording-2.pcm", 16_000, b"\xff\x7f\x00\x80"),
    ]


@pytest.mark.parametrize(
    ("body", "content_type", "sample_rate", "error"),
    [
        (b"", CONTENT_TYPE, 16_000, "empty_request"),
        (b"junk", "application/octet-stream", 16_000, "invalid_multipart_boundary"),
        (multipart(("empty.pcm", b"")), CONTENT_TYPE, 16_000, "empty_recording"),
        (multipart(("odd.pcm", b"\x01")), CONTENT_TYPE, 16_000, "invalid_pcm_alignment"),
        (
            multipart(("text.txt", b"\x00\x00"), content_type="text/plain"),
            CONTENT_TYPE,
            16_000,
            "unsupported_audio_content_type",
        ),
    ],
)
def test_rejects_invalid_multipart_audio(body, content_type, sample_rate, error):
    with pytest.raises(MultipartPcmError, match=error):
        asyncio.run(read_multipart_pcm(chunks(body), content_type, sample_rate))


def test_rejects_missing_boundary_and_out_of_range_sample_rate():
    with pytest.raises(MultipartPcmError, match="invalid_multipart_boundary"):
        asyncio.run(read_multipart_pcm(chunks(b"body"), "multipart/form-data", 16_000))
    with pytest.raises(MultipartPcmError, match="invalid_sample_rate"):
        read(multipart(("one.pcm", b"\x00\x00")), sample_rate=192_000)


def test_request_recording_and_count_limits_are_enforced():
    body = multipart(("one.pcm", b"\x00\x00"), ("two.pcm", b"\x00\x00"))
    with pytest.raises(MultipartPcmError, match="request_too_large"):
        read(body, max_request_bytes=16)
    with pytest.raises(MultipartPcmError, match="recording_too_large"):
        read(multipart(("one.pcm", b"\x00" * 8)), max_recording_bytes=4)
    with pytest.raises(MultipartPcmError, match="too_many_recordings"):
        read(body, max_recordings=1)


def test_per_recording_duration_is_bounded():
    body = multipart(("long.pcm", b"\x00" * (16_000 * 2 * 30 + 2)))
    with pytest.raises(MultipartPcmError, match="recording_too_long"):
        read(body)
