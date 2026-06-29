"""Translate Home Assistant App options to the service environment."""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")
TOKEN_PATH = Path("/data/mcp_auth_token")


def _load_options() -> dict[str, object]:
    if not OPTIONS_PATH.is_file():
        return {}
    with OPTIONS_PATH.open(encoding="utf-8") as options_file:
        options = json.load(options_file)
    if not isinstance(options, dict):
        raise SystemExit("/data/options.json must contain a JSON object")
    return options


def _store_token(token: str, token_path: Path = TOKEN_PATH) -> None:
    """Persist a token with owner-only permissions."""
    token_path.write_text(f"{token}\n", encoding="utf-8")
    token_path.chmod(0o600)


def _resolve_auth_token(
    options: dict[str, object],
    token_path: Path = TOKEN_PATH,
) -> tuple[str, str]:
    """Return a configured, persisted, or newly generated token."""
    configured = str(options.get("mcp_auth_token") or os.environ.get("MCP_AUTH_TOKEN") or "")
    if configured:
        if len(configured) < 16:
            raise SystemExit("MCP bearer token must contain at least 16 characters")
        if token_path.parent.is_dir():
            _store_token(configured, token_path)
        return configured, "configured"

    if token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token, "stored"

    if not token_path.parent.is_dir():
        raise SystemExit(
            "Missing MCP_AUTH_TOKEN; automatic generation requires the Home Assistant /data volume"
        )

    token = secrets.token_urlsafe(48)
    _store_token(token, token_path)
    return token, "generated"


def main() -> None:
    options = _load_options()
    mapping = {
        "esphome_dashboard_url": "ESPHOME_DASHBOARD_URL",
        "esphome_dashboard_username": "ESPHOME_DASHBOARD_USERNAME",
        "esphome_dashboard_password": "ESPHOME_DASHBOARD_PASSWORD",
        "log_level": "LOG_LEVEL",
    }
    for option_name, environment_name in mapping.items():
        value = options.get(option_name)
        if value is not None:
            os.environ[environment_name] = str(value)

    required = ("ESPHOME_DASHBOARD_URL",)
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing required configuration: {', '.join(missing)}")

    token, token_source = _resolve_auth_token(options, TOKEN_PATH)
    os.environ["MCP_AUTH_TOKEN"] = token
    print(
        f"ESPHome MCP bearer token ({token_source}): {token}",
        flush=True,
    )
    print(
        "Use this value in the Authorization header: Bearer <token>",
        flush=True,
    )

    os.environ.setdefault("ESPHOME_MCP_ALLOW_LOCAL_FILES", "false")
    os.environ.setdefault("ESPHOME_MCP_STARTUP_CHECK", "false")
    os.execvp("esphome-mcp-web", ["esphome-mcp-web"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as err:
        print(f"Unable to start ESPHome MCP: {err}", file=sys.stderr)
        raise SystemExit(1) from err
