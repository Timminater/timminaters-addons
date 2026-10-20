"""One bounded local ML child at a time; no forecasting web services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

MODEL = "mistralai/Timestral-209M"
REVISION = "9874d252ab0aae16f7f7c999f98f98d6ad7d8365"
WEIGHTS_SHA256 = "1456effb9927e3daa8c7234f10497e3424233869245a9c72b8ecebaf7ad2b7fa"
WEIGHTS_BYTES = 837271648
BUSY = threading.Lock()


def data_root(settings):
    return Path(settings.get("_data_dir") or os.environ.get("DATA_DIR", "/data"))


def timestral_status(data_dir):
    folder = Path(data_dir) / "models" / "timestral-209m"
    ready = False
    try:
        marker = json.loads((folder / "verified.json").read_text())
        ready = (marker.get("revision") == REVISION and marker.get("sha256") == WEIGHTS_SHA256
                 and (folder / "model.safetensors").stat().st_size == WEIGHTS_BYTES
                 and (folder / "config.json").is_file() and (folder / "LICENSE").is_file())
    except (OSError, ValueError, TypeError):
        pass
    partials = list((folder / ".cache" / "huggingface" / "download").glob("*.incomplete"))
    sizes = []
    for path in [*partials, folder / "model.safetensors"]:
        try: sizes.append(path.stat().st_size)
        except OSError: pass  # downloader may atomically rename a partial file
    downloaded = WEIGHTS_BYTES if ready else max(sizes, default=0)
    return {"ready": ready, "model": MODEL, "revision": REVISION,
            "download_bytes": WEIGHTS_BYTES, "downloaded_bytes": min(downloaded, WEIGHTS_BYTES),
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


def start_download(service, confirmed):
    if confirmed is not True:
        raise ValueError("Bevestig expliciet het downloaden en de Timestral-modellicentie")
    with service._download_lock:
        if service._download_thread and service._download_thread.is_alive():
            return dict(service._download_job)
        service._download_job = {"state": "running", "message": "Timestral downloaden en SHA-256 controleren"}
        def download():
            try:
                result = run_local("timestral_download", {}, {"_data_dir": str(service.store.data_dir),
                    "ml_threads": service.get_settings()["ml_threads"], "_cancel": service._stop.is_set}, timeout=1800)
                service._download_job = {"state": "completed", "message": "Timestral staat lokaal klaar", **result}
                service._wake.set()
            except Exception as exc:
                service._download_job = {"state": "failed", "message": str(exc)}
        service._download_thread = threading.Thread(target=download, name="timestral-download", daemon=True)
        service._download_thread.start()
        return dict(service._download_job)
