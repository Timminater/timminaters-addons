from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import List


DEFAULT_MEDIA_DIR = "/media/frame"
DEFAULT_DATA_DIR = "/data"
DEFAULT_STATE_FILENAME = "gallery_state.json"
DEFAULT_RUNTIME_SETTINGS_FILENAME = "runtime_settings.json"
DEFAULT_REFRESH_SECONDS = 30
DEFAULT_SNAPSHOT_TTL_SECONDS = 20


def parse_tv_ips(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    tv_ips: List[str]
    media_dir: str
    data_dir: str
    state_path: str
    automation_token: str
    refresh_interval_seconds: int
    snapshot_ttl_seconds: int
    runtime_settings_path: str


def _read_options_file() -> dict:
    options_path = "/data/options.json"
    if not os.path.exists(options_path):
        return {}
    try:
        with open(options_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings() -> Settings:
    options = _read_options_file()

    media_dir = os.getenv("MEDIA_DIR", DEFAULT_MEDIA_DIR)
    data_dir = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)
    state_path = os.path.join(data_dir, DEFAULT_STATE_FILENAME)
    runtime_settings_path = os.path.join(data_dir, DEFAULT_RUNTIME_SETTINGS_FILENAME)
    runtime_overrides = _read_json_file(runtime_settings_path)

    tv_raw = os.getenv("TV_IPS") or options.get("tv")
    if runtime_overrides.get("tv_ips"):
        tv_raw = ",".join(runtime_overrides["tv_ips"])

    token = os.getenv("AUTOMATION_TOKEN") or options.get("automation_token") or ""

    refresh_interval_raw = os.getenv("REFRESH_INTERVAL_SECONDS", str(DEFAULT_REFRESH_SECONDS))
    try:
        refresh_interval = max(5, int(refresh_interval_raw))
    except ValueError:
        refresh_interval = DEFAULT_REFRESH_SECONDS
    if runtime_overrides.get("refresh_interval_seconds") is not None:
        try:
            refresh_interval = max(5, int(runtime_overrides["refresh_interval_seconds"]))
        except (TypeError, ValueError):
            pass

    snapshot_ttl_raw = os.getenv("SNAPSHOT_TTL_SECONDS", str(DEFAULT_SNAPSHOT_TTL_SECONDS))
    try:
        snapshot_ttl = max(1, int(snapshot_ttl_raw))
    except ValueError:
        snapshot_ttl = DEFAULT_SNAPSHOT_TTL_SECONDS
    if runtime_overrides.get("snapshot_ttl_seconds") is not None:
        try:
            snapshot_ttl = max(1, int(runtime_overrides["snapshot_ttl_seconds"]))
        except (TypeError, ValueError):
            pass

    return Settings(
        tv_ips=parse_tv_ips(tv_raw),
        media_dir=media_dir,
        data_dir=data_dir,
        state_path=state_path,
        automation_token=str(token),
        refresh_interval_seconds=refresh_interval,
        snapshot_ttl_seconds=snapshot_ttl,
        runtime_settings_path=runtime_settings_path,
    )
