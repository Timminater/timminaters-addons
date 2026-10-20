"""Optional CPU worker. A child process releases ALL model RAM after each request.

Keep this service on a private container network: it intentionally has no public
API authentication. No model imports or downloads happen before a forecast call.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mistralai/Timestral-209M"
REVISION = "9874d252ab0aae16f7f7c999f98f98d6ad7d8365"
BUSY = threading.Lock()


def validate(payload):
    context, horizon = payload.get("context"), payload.get("prediction_length")
    if (not isinstance(context, list) or not 192 <= len(context) <= 90 * 96
            or type(horizon) is not int or not 1 <= horizon <= 672):
        raise ValueError("Expected 192–8640 context quarters and 1–672 forecast quarters")
    if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)) for v in context):
        raise ValueError("Context must contain finite prices or null gaps")
    if sum(v is not None for v in context) < 192:
        raise ValueError("At least 192 observed quarters required")
    return context, horizon


def infer(payload):
    context, horizon = validate(payload)
    import numpy as np
    import torch
    from timestral import TimestralForecaster
    torch.set_num_threads(max(1, min(4, int(os.environ.get("TIMESTRAL_THREADS", "2")))))
    model = TimestralForecaster.from_pretrained(MODEL, revision=REVISION, device="cpu", token=False)
    forecast = model.forecast(np.asarray([np.nan if v is None else v for v in context], dtype=np.float32),
                              prediction_length=horizon, clip_nonnegative=False)
    return {"model": MODEL, "revision": REVISION,
            "median": forecast.median.reshape(-1).tolist(),
            "lower": forecast.quantile(.1).reshape(-1).tolist(),
            "upper": forecast.quantile(.9).reshape(-1).tolist()}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        raw = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404,
                   {"model": MODEL, "revision": REVISION, "busy": BUSY.locked(),
                    "weights_loaded": False, "note": "Health is not an inference readiness check"})

    def do_POST(self):
        if self.path != "/forecast":
            return self.reply(404, {"error": "not found"})
        if not BUSY.acquire(blocking=False):
            return self.reply(429, {"error": "Worker busy"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 500_000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            validate(payload)
            child = subprocess.run([sys.executable, __file__, "--infer"],
                input=json.dumps(payload), text=True, capture_output=True, timeout=170, check=False)
            if child.returncode:
                # Library errors can include input: don't echo their output to clients.
                return self.reply(503, {"error": "Timestral inference failed; check worker dependencies/cache access"})
            self.reply(200, json.loads(child.stdout))
        except (ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except subprocess.TimeoutExpired:
            self.reply(504, {"error": "Timestral inference timed out; reduce context or add CPU/RAM"})
        finally:
            BUSY.release()


if __name__ == "__main__":
    if "--infer" in sys.argv:
        print(json.dumps(infer(json.load(sys.stdin)), allow_nan=False))
    else:
        ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8098"))), Handler).serve_forever()
