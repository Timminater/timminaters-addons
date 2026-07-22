"""Runtime configuration loaded from Home Assistant's persistent data volume."""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_level: str
    recognition_threshold: float
    max_audio_seconds: int
    api_token: str
    companion_token: str
    port: int

    @classmethod
    def load(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        options_path = data_dir / "options.json"
        options: dict[str, object] = {}
        if options_path.exists():
            try:
                options = json.loads(options_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                logging.getLogger(__name__).warning("Could not read %s: %s", options_path, error)

        threshold = min(1.0, max(0.0, float(options.get("recognition_threshold", 0.65))))
        max_seconds = min(120, max(5, int(options.get("max_audio_seconds", 120))))
        data_dir.mkdir(parents=True, exist_ok=True)
        companion_token_path = data_dir / "companion_token"
        try:
            companion_token = companion_token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            companion_token = secrets.token_urlsafe(32)
            temporary = companion_token_path.with_suffix(".tmp")
            temporary.write_text(companion_token, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, companion_token_path)
        if not companion_token:
            raise RuntimeError("The companion integration token is empty")
        return cls(
            data_dir=data_dir,
            log_level=str(options.get("log_level", "info")).upper(),
            recognition_threshold=threshold,
            max_audio_seconds=max_seconds,
            api_token=str(options.get("api_token", "")).strip(),
            companion_token=companion_token,
            port=int(os.environ.get("PORT", "8099")),
        )
