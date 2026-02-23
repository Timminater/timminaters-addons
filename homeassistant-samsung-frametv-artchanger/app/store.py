from __future__ import annotations

import copy
import json
import os
from threading import RLock
from typing import Any, Dict


SCHEMA_VERSION = 1


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "assets": {},
        "last_refresh": None,
    }


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if not os.path.exists(self.path):
                return default_state()

            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return default_state()

            state = default_state()
            state.update(loaded)
            if "assets" not in state or not isinstance(state["assets"], dict):
                state["assets"] = {}
            return state

    def save(self, state: Dict[str, Any]) -> None:
        with self._lock:
            serializable = copy.deepcopy(state)
            serializable["schema_version"] = SCHEMA_VERSION

            temp_path = f"{self.path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(serializable, handle, indent=2, sort_keys=True)
            os.replace(temp_path, self.path)
