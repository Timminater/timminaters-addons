"""Optional real-model smoke test using four external WAV fixtures."""

from __future__ import annotations

import argparse
import base64
import shutil
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models import AudioInput
from app.recognizer import SpeakerRecognizer


def read_wav(path: Path) -> AudioInput:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"Fixture must be mono 16-bit PCM: {path}")
        return AudioInput(
            audio_data=base64.b64encode(source.readframes(source.getnframes())).decode(),
            sample_rate=source.getframerate(),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    if args.data_dir.exists():
        shutil.rmtree(args.data_dir)

    recognizer = SpeakerRecognizer(args.data_dir, threshold=0.65, max_audio_seconds=120)
    recognizer.initialize()
    recognizer.enroll("speaker1", [read_wav(args.fixtures / "speaker1_1.wav")])
    recognizer.enroll("speaker2", [read_wav(args.fixtures / "speaker2_1.wav")])

    for expected in ("speaker1", "speaker2"):
        matched, confidence, scores = recognizer.recognize(
            read_wav(args.fixtures / f"{expected}_2.wav")
        )
        actual = matched.name if matched else "unknown"
        print(f"{expected}: {actual} ({confidence:.4f}) {scores}")
        if actual != expected:
            raise SystemExit(f"Expected {expected}, got {actual}")


if __name__ == "__main__":
    main()
