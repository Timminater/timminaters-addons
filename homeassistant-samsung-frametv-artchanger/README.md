# Samsung Frame TV Art Changer (Home Assistant Add-on)

Deze add-on beheert Samsung Frame TV Art via een Home Assistant Ingress webapp.

## Kernfunctionaliteit

- Galerij met art uit Home Assistant (`/media/frame`) en van de TV.
- Sync-status per item (`HA`, `TV`, `SYNC`, `ACTIVE`).
- 2-weg zichtbaarheid in de galerij:
  - HA-items worden zichtbaar en kunnen naar TV geactiveerd worden.
  - TV-items worden zichtbaar, inclusief background thumbnail-fetch via queue.
- Upload met interactieve crop naar vaste output `3840x2160` (JPEG quality 90).
- Crop tools:
  - Zoom
  - Rotatie slider (`-45..45`)
  - Extra `90 graden` rotatie
  - Horizontaal spiegelen
- Acties per item:
  - Activeren op geselecteerde TV's
  - Verwijderen van `tv`, `ha` of `both`
- Non-blocking refresh met metadata (`stale`, `refresh_in_progress`, `last_refresh`).
- Caching:
  - TV snapshot cache in memory (TTL)
  - Thumbnail cache op disk (`/data/cache/thumbs`)
- Instellingenpagina in de UI:
  - TV IP's beheren
  - Refresh interval
  - Snapshot TTL
  - Netwerkscan voor ondersteunde Samsung TV's

## Add-on Configuratie

In `config.yaml` van de add-on:

- `tv`: comma-separated IP lijst (initiele TV lijst).
- `automation_token`: token voor REST fallback endpoint.
- `ingress: true`: UI via Home Assistant Ingress.
- `stdin: true`: native automation trigger via `hassio.addon_stdin`.

Voorbeeld add-on opties:

```yaml
tv: "192.168.10.170"
automation_token: "kies-een-lang-geheim-token"
```

## UI Gebruik

Open in Home Assistant:
`Instellingen -> Add-ons -> Samsung Frame TV Art Changer -> Open Web UI`.

Belangrijk:

- De zijbalk-optie (`In zijbalk tonen`) is een Home Assistant UI-optie op de add-on pagina.
- De add-on hoeft daarvoor niet apart geconfigureerd te worden.

## Automation (Native, Aanbevolen)

Gebruik Home Assistant service `hassio.addon_stdin`.

Voorbeeld: random item kiezen, zo nodig uploaden, en activeren.

```yaml
action:
  - service: hassio.addon_stdin
    data:
      addon: 88e5264e_hass-frametv-artchanger
      input: >-
        {"action":"random_activate","tv_ips":["192.168.10.170"],"ensure_upload":true,"activate":true}
```

Voorbeeld: handmatige refresh triggeren.

```yaml
action:
  - service: hassio.addon_stdin
    data:
      addon: 88e5264e_hass-frametv-artchanger
      input: '{"action":"refresh"}'
```

Belangrijk:

- Gebruik bij `addon:` altijd jouw echte add-on id uit Home Assistant.
- Voor custom repositories is dit vaak `<repo_hash>_hass-frametv-artchanger` (zoals `88e5264e_hass-frametv-artchanger`).
- Je vindt deze id op de add-on pagina of via Ontwikkelaarstools bij de service call.

Ondersteunde stdin acties:

- `{"action":"random_activate","tv_ips":[...],"ensure_upload":true,"activate":true}`
- `{"action":"refresh"}`

## Automation (REST Fallback)

Gebruik alleen als fallback wanneer `hassio.addon_stdin` niet past in je flow.

Endpoint:

- `POST /api/automation/random`
- Auth: `Authorization: Bearer <automation_token>`

Voorbeeld `rest_command` in Home Assistant:

```yaml
rest_command:
  frame_random_art:
    url: "http://<jouw-addon-hostname>:8099/api/automation/random"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTOMATION_TOKEN"
      Content-Type: "application/json"
    payload: >-
      {
        "tv_ips": ["192.168.10.170"],
        "ensure_upload": true,
        "activate": true
      }
```

## API Overzicht

- `GET /api/health`
- `GET /api/tvs`
- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/settings/discover`
- `GET /api/gallery`
- `POST /api/refresh`
- `GET /api/thumb/{asset_id}`
- `POST /api/upload`
- `POST /api/items/{asset_id}/activate`
- `DELETE /api/items/{asset_id}`
- `POST /api/automation/random`

## API Meta & Errors

Responses bevatten (waar van toepassing):

- `meta.stale`
- `meta.refresh_in_progress`
- `meta.last_refresh`
- `meta.request_id`

Errors zijn backward-compatible:

- `detail` blijft bestaan
- extra `error.code`, `error.message`, `error.retryable`, `error.request_id`

Voorbeelden van error codes:

- `INVALID_INPUT`
- `UNAUTHORIZED`
- `NOT_FOUND`
- `TV_OFFLINE`
- `TV_UNSUPPORTED`
- `UPLOAD_FAILED`
- `DELETE_FAILED`
- `NO_RANDOM_ASSETS`
- `INTERNAL_ERROR`

## Migratie

- Bestaande `/media/frame` afbeeldingen worden geindexeerd.
- Legacy `uploaded_files.json` wordt automatisch gemigreerd.

## Standalone Lokaal Starten (Ontwikkel/Test)

Voor lokaal testen vanaf je eigen systeem in hetzelfde netwerk:

```powershell
cd c:\HomeAssistant\homeassistant-addons\homeassistant-samsung-frametv-artchanger
.\run-local.ps1 -TvIps "192.168.10.170"
```

`run-local.ps1` maakt `standalone-media` en `standalone-data` automatisch aan als ze nog niet bestaan.

Open daarna:

- `http://localhost:8099`
- of vanaf een ander device in je LAN: `http://<jouw-pc-ip>:8099`

## Troubleshooting

- Geen items zichtbaar:
  - Controleer TV IP en of de TV online is.
  - Trigger handmatig `Refresh`.
  - Controleer add-on logs.
- Thumbnails blijven op `Bezig...`:
  - TV thumbnail fetching loopt via queue (een voor een); geef het even tijd.
  - Controleer netwerkbereikbaarheid van de TV.
- `hassio.addon_stdin` werkt niet:
  - Controleer dat `addon` het echte add-on id is (bijv. `88e5264e_hass-frametv-artchanger`).
  - Controleer dat de add-on draait en `stdin: true` actief is.
