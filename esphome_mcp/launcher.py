"""Translate Home Assistant App options to the service environment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")


def _load_options() -> dict[str, object]:
    if not OPTIONS_PATH.is_file():
        return {}
    with OPTIONS_PATH.open(encoding="utf-8") as options_file:
        options = json.load(options_file)
    if not isinstance(options, dict):
        raise SystemExit("/data/options.json must contain a JSON object")
    return options


def main() -> None:
    options = _load_options()
    mapping = {
        "esphome_dashboard_url": "ESPHOME_DASHBOARD_URL",
        "esphome_dashboard_username": "ESPHOME_DASHBOARD_USERNAME",
        "esphome_dashboard_password": "ESPHOME_DASHBOARD_PASSWORD",
        "mcp_auth_token": "MCP_AUTH_TOKEN",
        "log_level": "LOG_LEVEL",
    }
    for option_name, environment_name in mapping.items():
        value = options.get(option_name)
        if value is not None:
            os.environ[environment_name] = str(value)

    required = ("ESPHOME_DASHBOARD_URL", "MCP_AUTH_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing required configuration: {', '.join(missing)}")
    if len(os.environ["MCP_AUTH_TOKEN"]) < 16:
        raise SystemExit("MCP_AUTH_TOKEN must contain at least 16 characters")

    os.environ.setdefault("ESPHOME_MCP_ALLOW_LOCAL_FILES", "false")
    os.environ.setdefault("ESPHOME_MCP_STARTUP_CHECK", "false")
    os.execvp("esphome-mcp-web", ["esphome-mcp-web"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as err:
        print(f"Unable to start ESPHome MCP: {err}", file=sys.stderr)
        raise SystemExit(1) from err
