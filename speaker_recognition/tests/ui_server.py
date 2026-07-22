"""Local fake-ML server used for manual browser acceptance checks."""

from pathlib import Path
import sys

import numpy as np
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).parents[1]))

import app.api as api
from app.models import AssistSatelliteInfo, HomeAssistantPersonInfo
from app.recognizer import SpeakerRecognizer


class DemoEncoder:
    def embed_utterance(self, wav: np.ndarray) -> np.ndarray:
        return np.asarray([float(np.mean(wav > 0)), float(np.mean(wav <= 0))], dtype=np.float32)


api.recognizer = SpeakerRecognizer(
    Path(".ui-test-data"),
    threshold=0.65,
    max_audio_seconds=120,
    encoder_factory=DemoEncoder,
    preprocess=lambda wav, _rate: wav,
)


class DemoHomeAssistant:
    def satellites(self):
        return [
            AssistSatelliteInfo(
                entity_id="assist_satellite.home_assistant_voice_095b3e_assist_satellite",
                name="Home Assistant Voice 095b3e Assist satellite",
                state="idle",
            )
        ]

    def ask_for_enrollment_sample(self, _entity_id):
        return None

    def confirm_enrollment_sample(self, _entity_id):
        return None

    def persons(self):
        return [HomeAssistantPersonInfo(entity_id="person.tim", name="Tim")]


api.home_assistant = DemoHomeAssistant()
api._is_supervisor_request = lambda _request: True


class SimulatedIngress(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.scope["headers"].append((b"x-ingress-path", b"/"))
        return await call_next(request)


api.app.add_middleware(SimulatedIngress)

if __name__ == "__main__":
    uvicorn.run(api.app, host="127.0.0.1", port=8766, log_level="warning")
