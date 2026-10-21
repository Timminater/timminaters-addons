"""One bounded local ML child at a time; no forecasting web services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from .timestral_models import model_spec

# Legacy aliases retained for the original 209M verification interface.
MODEL = model_spec("209M")["repo"]
REVISION = model_spec("209M")["revision"]
WEIGHTS_SHA256 = model_spec("209M")["sha256"]
WEIGHTS_BYTES = model_spec("209M")["bytes"]
BUSY = threading.Lock()


def data_root(settings):
    return Path(settings.get("_data_dir") or os.environ.get("DATA_DIR", "/data"))


def timestral_status(data_dir, model="209M"):
    spec = model_spec(model)
    revision, sha256, size = spec["revision"], spec["sha256"], spec["bytes"]
    folder = Path(data_dir) / "models" / spec["folder"]
    ready = False
    try:
        marker = json.loads((folder / "verified.json").read_text())
        ready = (marker.get("revision") == revision and marker.get("sha256") == sha256
                 and (folder / "model.safetensors").stat().st_size == size
                 and (folder / "config.json").is_file() and (folder / "LICENSE").is_file())
    except (OSError, ValueError, TypeError):
        pass
    partials = list((folder / ".cache" / "huggingface" / "download").glob("*.incomplete"))
    sizes = []
    for path in [*partials, folder / "model.safetensors"]:
        try: sizes.append(path.stat().st_size)
        except OSError: pass  # downloader may atomically rename a partial file
    downloaded = size if ready else max(sizes, default=0)
    return {"ready": ready, "model": spec["repo"], "size": model, "revision": revision,
            "download_bytes": size, "downloaded_bytes": min(downloaded, size),
            "busy": BUSY.locked(), "job": {"state": "idle"}}


def run_local(method, payload, settings, *, timeout=300):
    """Use stdin JSON, fixed module/operation, bounded threads, no shell or URLs.

    Only this function starts heavy work. Killing the child releases model RAM.
    Disabled-method checks in callers are repeated while the child runs.
    """
    if method not in {"epex", "timestral", "timestral_download"}:
        raise ValueError("Onbekende lokale rekenopdracht")
    if not BUSY.acquire(blocking=False):
        raise ValueError("Een lokale ML-berekening of modeldownload is al bezig; probeer later opnieuw")
    try:
        root = data_root(settings)
        root.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({"DATA_DIR": str(root), "HF_HOME": str(root / "models" / "hub"),
                    "HF_HUB_OFFLINE": "1" if method == "timestral" else "0",
                    "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_XET": "1",
                    "OMP_NUM_THREADS": str(settings.get("ml_threads", 2)),
                    "OPENBLAS_NUM_THREADS": str(settings.get("ml_threads", 2)),
                    "MKL_NUM_THREADS": str(settings.get("ml_threads", 2))})
        request = {**payload, "threads": settings.get("ml_threads", 2)}
        if method.startswith("timestral"):
            selected = settings.get("timestral_model", "209M")
            model_spec(selected)
            request["model_size"] = selected
        with subprocess.Popen([sys.executable, "-m", "app.local_worker", method],
                cwd=Path(__file__).resolve().parents[1], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            import time
            deadline = time.monotonic() + timeout
            raw = json.dumps(request, allow_nan=False, default=str)
            while True:
                try:
                    output, _ = child.communicate(input=raw, timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    raw = None  # stdin has already been sent
                    cancelled = settings.get("_cancel", lambda: False)()
                    if cancelled or time.monotonic() >= deadline:
                        child.kill()
                        child.communicate()
                        raise ValueError("Lokale berekening gestopt" if cancelled else
                                         "Lokale berekening duurde te lang; verklein de historie of verhoog beschikbare resources")
            try:
                result = json.loads(output)
            except (ValueError, TypeError):
                raise ValueError("Lokale rekenmodule is gestopt; controleer beschikbare RAM en geïnstalleerde bibliotheken") from None
            if child.returncode or result.get("error"):
                raise ValueError(result.get("error", "Lokale rekenmodule is mislukt"))
            return result
    finally:
        BUSY.release()


def start_download(service, confirmed, model=None):
    if confirmed is not True:
        raise ValueError("Bevestig expliciet het downloaden en de Timestral-modellicentie")
    selected = model if model is not None else service.get_settings()["timestral_model"]
    model_spec(selected)
    with service._download_lock:
        if service._download_thread and service._download_thread.is_alive():
            return dict(service._download_job)
        service._download_job = {"state": "running", "message": f"Timestral-{selected} downloaden en SHA-256 controleren", "size": selected}
        def download():
            try:
                result = run_local("timestral_download", {}, {"_data_dir": str(service.store.data_dir),
                    "ml_threads": service.get_settings()["ml_threads"], "timestral_model": selected, "_cancel": service._stop.is_set}, timeout=1800)
                service._download_job = {"state": "completed", "message": f"Timestral-{selected} staat lokaal klaar", "size": selected, **result}
                service._wake.set()
            except Exception as exc:
                service._download_job = {"state": "failed", "message": str(exc), "size": selected}
        service._download_thread = threading.Thread(target=download, name="timestral-download", daemon=True)
        service._download_thread.start()
        return dict(service._download_job)
