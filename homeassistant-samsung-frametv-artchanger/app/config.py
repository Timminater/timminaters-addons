from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import List


DEFAULT_MEDIA_DIR = "/media/frame"
DEFAULT_DATA_DIR = "/data"
DEFAULT_STATE_FILENAME = "gallery_state.json"
DEFAULT_REFRESH_SECONDS = 30


def parse_tv_ips(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    tv_ips: List[str]
    media_dir: str
    data_dir: str
    state_path: str
    automation_token: str
    refresh_interval_seconds: int


def _read_options_file() -> dict:
    options_path = "/data/options.json"
    if not os.path.exists(options_path):
        return {}
    try:
        with open(options_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings() -> Settings:
    options = _read_options_file()

    tv_raw = os.getenv("TV_IPS") or options.get("tv")
    token = os.getenv("AUTOMATION_TOKEN") or options.get("automation_token") or ""

    media_dir = os.getenv("MEDIA_DIR", DEFAULT_MEDIA_DIR)
    data_dir = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)
    state_path = os.path.join(data_dir, DEFAULT_STATE_FILENAME)

    refresh_interval_raw = os.getenv("REFRESH_INTERVAL_SECONDS", str(DEFAULT_REFRESH_SECONDS))
    try:
        refresh_interval = max(5, int(refresh_interval_raw))
    except ValueError:
        refresh_interval = DEFAULT_REFRESH_SECONDS

    return Settings(
        tv_ips=parse_tv_ips(tv_raw),
        media_dir=media_dir,
        data_dir=data_dir,
        state_path=state_path,
        automation_token=str(token),
        refresh_interval_seconds=refresh_interval,
    )
