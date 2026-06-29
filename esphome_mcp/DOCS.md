# ESPHome MCP App documentation

## Configuration

- `esphome_dashboard_url`: URL reachable from the App container. For an ESPHome
  App installed on the same Home Assistant system, use that App's internal
  hostname and port 6052. The repository prefix is installation-specific, so it
  is intentionally not hard-coded here.
- `esphome_dashboard_username` and `esphome_dashboard_password`: optional
  Device Builder credentials.
- `mcp_auth_token`: required secret of at least 16 characters. Generate a long,
  random value. Every MCP client must send it as a bearer token.
- `log_level`: `DEBUG`, `INFO`, `WARNING`, or `ERROR`.

Restart the App after changing its configuration.

## MCP client

The endpoint is:

```text
http://HOME_ASSISTANT_HOST:8080/mcp
```

Configure the client to send:

```text
Authorization: Bearer YOUR_TOKEN
```

Do not forward port 8080 to the public internet. The server exposes tools that
can edit ESPHome configuration and install firmware.

## Home Assistant MCP integration

Home Assistant 2026.6 documents SSE plus OAuth for its MCP client integration.
This App currently exposes Streamable HTTP with a static bearer token, so it is
intended for MCP clients that support that transport and authentication method.

## Troubleshooting

- If startup reports a missing option, set both the dashboard URL and token.
- If tools report dashboard connection errors, verify the URL from inside the
  Home Assistant App network and confirm Device Builder is running.
- The Docker healthcheck includes a real `list_device_names` MCP call. A stopped
  or unreachable Device Builder therefore makes the container unhealthy, while
  the MCP process remains available and can recover when Device Builder returns.
