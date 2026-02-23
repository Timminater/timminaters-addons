# Samsung Frame TV Art Changer add-on for Home Assistant

This add-on now runs as a continuous web service with a Home Assistant Ingress interface.

## Features

- Ingress web gallery for `/media/frame` images.
- Sync flags for Home Assistant vs TV availability.
- Upload with interactive crop and fixed export resolution `3840x2160`.
- Per-image actions:
  - Set active on selected TV(s)
  - Delete on TV, Home Assistant, or both
- Automation endpoint for random art selection:
  - Picks a random local gallery item
  - Uploads to TV if missing
  - Activates it on selected TV(s)

## Installation

Install this addon by adding the repository:

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvivalatech%2Fhomeassistant-addons)

## Configuration Options

1. `tv`: Comma-separated list of Samsung The Frame TV IP addresses.
2. `automation_token`: Bearer token required for `POST /api/automation/random`.

## UI Access

Open the add-on and click **Open Web UI**. The UI is exposed through Home Assistant Ingress only.

## Automation: Random Art Trigger

The old `hassio.addon_start` random-flow is deprecated in v2.
Use a REST call to the add-on endpoint instead.

Example Home Assistant configuration:

```yaml
rest_command:
  frame_random_art:
    url: "http://a0d7b954-hass-frametv-artchanger:8099/api/automation/random"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTOMATION_TOKEN"
      Content-Type: "application/json"
    payload: >-
      {
        "tv_ips": ["192.168.1.199"],
        "ensure_upload": true,
        "activate": true
      }
```

Example automation:

```yaml
description: "Random Frame Art"
mode: single
trigger:
  - platform: time
    at: "23:00:00"
action:
  - service: rest_command.frame_random_art
```

## Migration Notes

- Existing `/media/frame` images are indexed automatically.
- Legacy `uploaded_files.json` entries are migrated automatically on startup.
