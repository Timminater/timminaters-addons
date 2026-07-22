"""Config flow for Speaker Recognition."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import SpeakerRecognitionApi, SpeakerRecognitionApiError
from .const import (
    CONF_BACKEND_URL,
    CONF_ENTRY_TYPE,
    CONF_STT_ENTITY,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_URL,
    DOMAIN,
    ENTRY_TYPE_MAIN,
    ENTRY_TYPE_STT,
)


def _main_entry(flow: ConfigFlow) -> ConfigEntry | None:
    return next(
        (
            entry
            for entry in flow._async_current_entries()
            if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_MAIN) == ENTRY_TYPE_MAIN
        ),
        None,
    )


async def _can_connect(flow: ConfigFlow, url: str, token: str) -> bool:
    try:
        api = SpeakerRecognitionApi(async_get_clientsession(flow.hass), url, token)
        health = await api.async_health()
        await api.async_speakers()
    except SpeakerRecognitionApiError:
        return False
    return bool(health.get("ready"))


class SpeakerRecognitionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure the companion integration and proxy entities."""

    VERSION = 3

    def __init__(self) -> None:
        self._discovery: dict[str, Any] | None = None
        self._discovered_instance_id: str | None = None
        self._existing_entry: ConfigEntry | None = None

    def _update_discovered_entry(self, existing: ConfigEntry, data: dict[str, Any]) -> None:
        """Apply confirmed discovery and reload dependent proxies once."""
        options = {**existing.options, CONF_URL: data[CONF_URL], CONF_TOKEN: data[CONF_TOKEN]}
        changed = self.hass.config_entries.async_update_entry(
            existing,
            data=data,
            options=options,
            unique_id=self._discovered_instance_id,
        )
        if not changed:
            return
        if not existing.update_listeners:
            self.hass.config_entries.async_schedule_reload(existing.entry_id)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle Supervisor App discovery and require user confirmation."""
        config = discovery_info.config
        data = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_MAIN,
            CONF_URL: f"http://{config['host']}:{config.get('port', 8099)}",
            CONF_TOKEN: config["token"],
        }
        instance_id = str(config.get("instance_id", discovery_info.slug))
        self._discovered_instance_id = instance_id
        existing = _main_entry(self)
        if existing is not None:
            if existing.unique_id == instance_id:
                self._update_discovered_entry(existing, data)
                return self.async_abort(reason="already_configured")
            # A legacy/manual entry or a different App may not be replaced silently.
            self._existing_entry = existing
        await self.async_set_unique_id(instance_id)
        self._abort_if_unique_id_configured(updates=data)
        self._discovery = data
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm a discovered App."""
        if self._discovery is None:
            return self.async_abort(reason="discovery_error")
        errors: dict[str, str] = {}
        if user_input is not None:
            if await _can_connect(self, self._discovery[CONF_URL], self._discovery[CONF_TOKEN]):
                if self._existing_entry is not None:
                    self._update_discovered_entry(self._existing_entry, self._discovery)
                    return self.async_abort(reason="reconfigured")
                return self.async_create_entry(title="Speaker Recognition App", data=self._discovery)
            errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), errors=errors)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the backend manually or add a proxy."""
        if _main_entry(self) is not None:
            return self.async_show_menu(
                step_id="user", menu_options=["add_stt"]
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            if await _can_connect(self, user_input[CONF_URL], user_input[CONF_TOKEN]):
                await self.async_set_unique_id("speaker_recognition_manual")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Speaker Recognition App",
                    data={CONF_ENTRY_TYPE: ENTRY_TYPE_MAIN, **user_input},
                )
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): selector.TextSelector(),
                    vol.Required(CONF_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_add_stt(self, user_input=None) -> ConfigFlowResult:
        """Add an STT wrapper."""
        if user_input is not None:
            entity = user_input[CONF_STT_ENTITY]
            await self.async_set_unique_id(f"{ENTRY_TYPE_STT}_{entity}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"STT: {entity.split('.', 1)[-1]}",
                data={CONF_ENTRY_TYPE: ENTRY_TYPE_STT, CONF_STT_ENTITY: entity},
            )
        return self.async_show_form(
            step_id="add_stt",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STT_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=Platform.STT)
                    )
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SpeakerRecognitionOptionsFlow()


class SpeakerRecognitionOptionsFlow(OptionsFlow):
    """Edit a proxy or backend entry."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        entry_type = self.config_entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_MAIN)
        if entry_type == ENTRY_TYPE_MAIN:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_URL,
                        default=self.config_entry.options.get(
                            CONF_URL,
                            self.config_entry.data.get(
                                CONF_URL, self.config_entry.data.get(CONF_BACKEND_URL, DEFAULT_URL)
                            ),
                        ),
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_TOKEN,
                        default=self.config_entry.options.get(
                            CONF_TOKEN, self.config_entry.data.get(CONF_TOKEN, "")
                        ),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            )
        elif entry_type == ENTRY_TYPE_STT:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_STT_ENTITY,
                        default=self.config_entry.options.get(
                            CONF_STT_ENTITY, self.config_entry.data[CONF_STT_ENTITY]
                        ),
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain=Platform.STT))
                }
            )
        else:
            return self.async_abort(reason="not_supported")
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
