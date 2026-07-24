"""Manual real-model quality matrix against a running 2.1 App container."""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"Expected mono PCM16 WAV: {path}")
        audio = np.frombuffer(
            handle.readframes(handle.getnframes()), dtype="<i2"
        ).astype(np.float32) / 32768.0
        return audio, handle.getframerate()


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    length = round(audio.size * target_rate / source_rate)
    return np.interp(
        np.linspace(0, max(0, audio.size - 1), length),
        np.arange(audio.size),
        audio,
    ).astype(np.float32)


class Api:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=190) as response:
            return json.load(response)

    def audio(self, path: str) -> tuple[np.ndarray, int]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            with wave.open(io.BytesIO(response.read()), "rb") as handle:
                audio = np.frombuffer(
                    handle.readframes(handle.getnframes()), dtype="<i2"
                ).astype(np.float32) / 32768
                return audio, handle.getframerate()


def audio_document(audio: np.ndarray) -> dict:
    pcm = np.asarray(np.clip(audio, -1, 0.9999695) * 32768, dtype="<i2")
    return {
        "audio_data": base64.b64encode(pcm.tobytes()).decode(),
        "sample_rate": 16_000,
    }


def correlation(left: np.ndarray, right: np.ndarray) -> float:
    count = min(left.size, right.size)
    left, right = left[:count], right[:count]
    return float(
        abs(np.dot(left, right))
        / max(1e-12, np.linalg.norm(left) * np.linalg.norm(right))
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18099")
    parser.add_argument("--token", default="codex-210-local-test")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--competitor", type=Path, required=True)
    args = parser.parse_args()

    api = Api(args.url, args.token)
    speakers = api.request("GET", "/api/speakers")
    target_profile = next(item for item in speakers if item["name"] == "Tim")
    target, target_rate = read_wav(args.target)
    competitor, competitor_rate = read_wav(args.competitor)
    target = resample(target, target_rate, 16_000)
    competitor = resample(competitor, competitor_rate, 16_000)
    length = min(target.size, competitor.size)
    target = target[:length]
    competitor = competitor[:length]
    timeline = np.arange(length, dtype=np.float32) / 16_000
    rng = np.random.default_rng(210)

    cases = {
        "clean": target,
        "constant_noise": target + 0.035 * rng.normal(size=length),
        "music_like": target
        + 0.04 * np.sin(2 * np.pi * 220 * timeline)
        + 0.03 * np.sin(2 * np.pi * 330 * timeline),
        "two_speakers": target + 0.65 * competitor,
        "absent_target": competitor,
        "clipping": np.clip(target * 8, -1, 0.9999695),
    }
    results: dict[str, dict] = {}
    for name, audio in cases.items():
        analyzed = api.request(
            "POST",
            "/api/analyze",
            {
                "audio": audio_document(audio),
                "source": "test",
                "extraction_mode": "off",
            },
        )
        recording_id = analyzed["recording_id"]
        api.request(
            "POST",
            f"/api/analysis/{recording_id}/process",
            {},
        )
        deadline = time.monotonic() + 185
        while True:
            analyzed = api.request("GET", f"/api/analysis/{recording_id}")
            if analyzed["processing_status"] not in {"queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(name)
            time.sleep(0.25)
        results[name] = {
            "outcome": analyzed.get("outcome"),
            "speaker": analyzed.get("speaker_name"),
            "confidence": analyzed.get("confidence"),
            "denoised": analyzed.get("denoised_available")
            or bool(analyzed.get("denoised_audio")),
            "stages": analyzed.get("processing_stages"),
            "fallback": analyzed.get("processing_fallback_reason"),
            "quality": analyzed.get("processing_quality"),
            "timings": analyzed.get("timings"),
        }
    warm_started = time.monotonic()
    warm_live = api.request(
        "POST",
        "/api/analyze",
        {
            "audio": audio_document(target),
            "source": "test",
            "extraction_mode": "before_stt",
        },
    )
    warm_elapsed = time.monotonic() - warm_started
    results["warm_live_clean"] = {
        "denoised": warm_live.get("denoised_available")
        or bool(warm_live.get("denoised_audio")),
        "fallback": warm_live.get("processing_fallback_reason"),
        "stages": warm_live.get("processing_stages"),
        "quality": warm_live.get("processing_quality"),
        "timings": warm_live.get("timings"),
        "wall_seconds": round(warm_elapsed, 3),
    }

    try:
        api.request(
            "POST",
            "/api/analyze",
            {
                "audio": audio_document(np.zeros(16_000, dtype=np.float32)),
                "source": "test",
                "extraction_mode": "before_stt",
            },
        )
        raise AssertionError("Silence should be rejected")
    except urllib.error.HTTPError as error:
        if error.code not in {400, 409}:
            raise
        results["silence"] = {"rejected_http": error.code}

    for name, result in results.items():
        if name != "silence" and result["quality"]:
            if result["quality"].get("duration_preserved") is not True:
                raise AssertionError(f"{name}: duration was not preserved")
            if float(result["quality"].get("clipping_ratio", 1.0)) >= 0.01:
                raise AssertionError(f"{name}: processed audio clips excessively")
    if not results["clean"]["denoised"]:
        raise AssertionError("Clean speech did not produce accepted denoised audio")
    if not results["constant_noise"]["denoised"]:
        raise AssertionError("Noisy speech did not produce accepted denoised audio")
    if not results["warm_live_clean"]["denoised"]:
        raise AssertionError("Warm before_stt speech did not produce denoised audio")
    if results["warm_live_clean"]["wall_seconds"] > 12:
        raise AssertionError("Warm before_stt processing exceeded 12 seconds")
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
