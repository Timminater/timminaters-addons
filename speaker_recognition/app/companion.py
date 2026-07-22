"""Install and announce the bundled Home Assistant companion integration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
DOMAIN = "speaker_recognition"
SOURCE = Path(__file__).parents[1] / "integration" / DOMAIN
CONFIG_ROOT = Path(os.environ.get("HOMEASSISTANT_CONFIG", "/homeassistant"))
MARKER = ".speaker_recognition_app_managed"
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")


def _source_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in SOURCE.rglob("*") if item.is_file()):
        digest.update(path.relative_to(SOURCE).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def install_integration() -> bool:
    """Copy the bundled integration into Home Assistant's custom_components."""
    if not SOURCE.is_dir():
        _LOGGER.warning("Bundled companion integration is missing: %s", SOURCE)
        return False

    components = CONFIG_ROOT / "custom_components"
    target = components / DOMAIN
    backup = components / f"{DOMAIN}.pre-app-backup"
    digest = _source_digest()
    if target.is_symlink():
        _LOGGER.error("Refusing to replace symlinked companion integration: %s", target)
        return False
    marker = target / MARKER
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        _LOGGER.debug("Companion integration is already current")
        return False

    components.mkdir(parents=True, exist_ok=True)
    staging = components / f".{DOMAIN}-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copytree(SOURCE, staging)
        (staging / MARKER).write_text(digest, encoding="utf-8")
        if target.exists():
            if not marker.is_file():
                if backup.exists():
                    _LOGGER.error(
                        "Cannot preserve unmanaged integration because %s already exists",
                        backup,
                    )
                    return False
                target.rename(backup)
                _LOGGER.warning("Preserved unmanaged integration as %s", backup)
            else:
                shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    _LOGGER.info("Installed companion integration in %s", target)
    return True


def _supervisor_request(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{SUPERVISOR_URL}{path}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        document = json.load(response)
    if document.get("result") != "ok":
        raise RuntimeError(f"Supervisor returned an error for {path}")
    return document.get("data", {})


def publish_discovery(token: str, port: int) -> bool:
    """Publish Supervisor discovery data for the companion config flow."""
    try:
        addon = _supervisor_request("/addons/self/info")
        hostname = addon["hostname"]
        slug = addon["slug"]
        _supervisor_request(
            "/discovery",
            method="POST",
            payload={
                "service": DOMAIN,
                "config": {
                    "host": hostname,
                    "port": port,
                    "token": token,
                    "instance_id": slug,
                },
            },
        )
    except (KeyError, OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as error:
        _LOGGER.warning("Could not publish Home Assistant discovery: %s", error)
        return False
    _LOGGER.info("Published Home Assistant discovery for %s", hostname)
    return True
