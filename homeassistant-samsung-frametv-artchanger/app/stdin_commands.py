from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from typing import Any, Dict

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
            finally:
                clear_request_id()

    def _handle_line(self, line: str, request_id: str) -> None:
        try:
            payload: Dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            _LOGGER.warning("stdin command invalid JSON request_id=%s error=%s payload=%s", request_id, exc, line)
            return

        action = str(payload.get("action", "")).strip()
        if action == "random_activate":
            tv_ips = payload.get("tv_ips") or None
            ensure_upload = bool(payload.get("ensure_upload", True))
            activate = bool(payload.get("activate", True))

            result = self.service.random_activate(tv_ips=tv_ips, ensure_upload=ensure_upload, activate=activate)
            _LOGGER.info("stdin random_activate completed request_id=%s result=%s", request_id, result)
            return

        if action == "refresh":
            triggered = self.service.trigger_refresh(force=True, wait=False)
            _LOGGER.info("stdin refresh completed request_id=%s triggered=%s", request_id, triggered)
            return

        _LOGGER.warning("stdin command unknown action request_id=%s action=%s payload=%s", request_id, action, payload)
