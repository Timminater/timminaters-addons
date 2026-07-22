from __future__ import annotations

import base64
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models import AudioInput


class FakeEncoder:
    def embed_utterance(self, wav: np.ndarray) -> np.ndarray:
        positive = float(np.mean(wav > 0))
        return np.asarray([positive, 1.0 - positive], dtype=np.float32)


def audio(value: int, seconds: float = 1.0, sample_rate: int = 16000) -> AudioInput:
    pcm = np.full(int(seconds * sample_rate), value, dtype="<i2")
    return AudioInput(audio_data=base64.b64encode(pcm.tobytes()).decode(), sample_rate=sample_rate)


@pytest.fixture
def fake_factory():
    return FakeEncoder


@pytest.fixture
def identity_preprocess():
    return lambda wav, _rate: wav
