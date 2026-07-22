"""Speaker Recognition companion integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SpeakerRecognitionApi, SpeakerRecognitionApiError
from .const import (
    CONF_BACKEND_URL,
    CONF_ENTRY_TYPE,
    CONF_TOKEN,
    CONF_URL,
    ENTRY_TYPE_MAIN,
    ENTRY_TYPE_STT,
)

SpeakerRecognitionConfigEntry = ConfigEntry[SpeakerRecognitionApi]


def get_main_entry(hass: HomeAssistant) -> SpeakerRecognitionConfigEntry | None:
    """Return the loaded backend entry."""
    for entry in hass.config_entries.async_entries("speaker_recognition"):
        if (
            entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_MAIN) == ENTRY_TYPE_MAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            return entry
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a backend, STT proxy, or conversation proxy entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_MAIN)
    if entry_type == ENTRY_TYPE_MAIN:
        url = entry.options.get(
            CONF_URL, entry.data.get(CONF_URL, entry.data.get(CONF_BACKEND_URL, ""))
        )
        token = entry.options.get(CONF_TOKEN, entry.data.get(CONF_TOKEN, ""))
        api = SpeakerRecognitionApi(async_get_clientsession(hass), url, token)
        try:
            health = await api.async_health()
            await api.async_speakers()
        except SpeakerRecognitionApiError as error:
            raise ConfigEntryNotReady(f"Speaker Recognition App is unavailable: {error}") from error
        if not health.get("ready"):
            raise ConfigEntryNotReady("Speaker Recognition App is still starting")
        entry.runtime_data = api
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        return True

    if get_main_entry(hass) is None:
        raise ConfigEntryNotReady("The Speaker Recognition backend is not configured")
    if entry_type != ENTRY_TYPE_STT:
        # Upstream conversation entries are kept loadable during migration, but no
        # longer create an entity that could turn voice matching into authorization.
        return True
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.STT])
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate upstream v1/v2 entries to the companion API schema."""
    if entry.version > 3:
        return False
    if entry.version < 3:
        data = dict(entry.data)
        data.setdefault(CONF_ENTRY_TYPE, ENTRY_TYPE_MAIN)
        if data[CONF_ENTRY_TYPE] == ENTRY_TYPE_MAIN and CONF_URL not in data:
            data[CONF_URL] = data.get(CONF_BACKEND_URL, "")
        hass.config_entries.async_update_entry(entry, data=data, version=3)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_MAIN)
    if entry_type == ENTRY_TYPE_MAIN:
        return True
    if entry_type != ENTRY_TYPE_STT:
        return True
    return await hass.config_entries.async_unload_platforms(entry, [Platform.STT])


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
