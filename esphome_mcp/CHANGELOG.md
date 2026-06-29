# Changelog

## 2026.06.1

- Generate and persist a secure MCP bearer token when none is configured.
- Show the active bearer token in the App log at every start and restart.
- Keep an optional manually configured token as an override.

## 2026.06.0

- Package ESPHome MCP as a Home Assistant App.
- Add Home Assistant options and multi-architecture metadata.
- Require bearer-token authentication for App deployments.
- Disable local filesystem tools in the App.
- Allow the MCP process to start before Device Builder is available.
