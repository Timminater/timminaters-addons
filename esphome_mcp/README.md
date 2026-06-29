# ESPHome MCP

Run an MCP server next to Home Assistant and connect it to an ESPHome 2026.6+
Device Builder dashboard.

The App exposes Streamable HTTP on port 8080 at `/mcp`. It can list ESPHome
devices, read and validate YAML, stream logs, and compile or install firmware.

The endpoint requires a bearer token. Local file access is disabled in the App
because MCP tools otherwise have no safe reason to read files from the container.

See [DOCS.md](DOCS.md) for configuration and client setup.
