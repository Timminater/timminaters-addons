from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from typing import Any, Dict, Optional

from app.request_context import clear_request_id, set_request_id
from app.service import GalleryService


_LOGGER = logging.getLogger(__name__)


class StdinCommandProcessor:
    def __init__(self, service: GalleryService) -> None:
        self.service = service
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True

        thread = threading.Thread(target=self._run, name="stdin-command-processor", daemon=True)
        thread.start()
        _LOGGER.info("stdin command processor started")

    def _run(self) -> None:
        while True:
            try:
                raw_line = sys.stdin.readline()
            except Exception as exc:
                _LOGGER.warning("stdin read error: %s", exc)
                break

            if raw_line == "":
                _LOGGER.info("stdin is closed, stopping stdin command processor")
                break

            line = raw_line.strip()
            if not line:
                continue

            request_id = uuid.uuid4().hex
            set_request_id(request_id)
            try:
                self._handle_line(line, request_id)
            except Exception as exc:  # pragma: no cover - defensive runtime handling
                _LOGGER.exception("stdin command handling failed request_id=%s error=%s", request_id, exc)
            finally:
                clear_request_id()

    def _parse_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _coerce_payload(self, loaded: Any) -> Optional[Dict[str, Any]]:
        if isinstance(loaded, dict):
            return loaded

        # Home Assistant can sometimes pass a JSON string containing a JSON object.
        if isinstance(loaded, str):
            nested = loaded.strip()
            if not nested:
                return None
            try:
                nested_loaded = json.loads(nested)
            except json.JSONDecodeError:
                return None
            if isinstance(nested_loaded, dict):
                return nested_loaded

        return None

    def _handle_line(self, line: str, request_id: str) -> None:
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError as exc:
            _LOGGER.warning("stdin command invalid JSON request_id=%s error=%s payload=%s", request_id, exc, line)
            return

        payload = self._coerce_payload(loaded)
        if payload is None:
            _LOGGER.warning(
                "stdin command invalid payload type request_id=%s payload_type=%s payload=%s",
                request_id,
                type(loaded).__name__,
                loaded,
            )
            return

        action = str(payload.get("action", "")).strip()
        if action == "random_activate":
            tv_ips = payload.get("tv_ips") or None
            ensure_upload = self._parse_bool(payload.get("ensure_upload", True), default=True)
            activate = self._parse_bool(payload.get("activate", True), default=True)

            result = self.service.random_activate(tv_ips=tv_ips, ensure_upload=ensure_upload, activate=activate)
            _LOGGER.info("stdin random_activate completed request_id=%s result=%s", request_id, result)
            return

        if action == "refresh":
            triggered = self.service.trigger_refresh(force=True, wait=False)
            _LOGGER.info("stdin refresh completed request_id=%s triggered=%s", request_id, triggered)
            return

        _LOGGER.warning("stdin command unknown action request_id=%s action=%s payload=%s", request_id, action, payload)
