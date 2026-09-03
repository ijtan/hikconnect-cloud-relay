"""Config and options flows for Hikvision Intercom."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .cloud import HikChannel, HikConnectAuthError, HikConnectClient, HikDevice
from .const import (
    CONF_API_HOST,
    CONF_CHANNEL,
    CONF_CHANNEL_NAME,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SERIAL,
    CONF_FPS,
    CONF_JPEG_QUALITY,
    CONF_PASSWORD,
    CONF_RELAY_HOST,
    CONF_STREAM_TYPE,
    CONF_USERNAME,
    DEFAULT_API_HOST,
    DEFAULT_FPS,
    DEFAULT_JPEG_QUALITY,
    DEFAULT_RELAY_HOST,
    DEFAULT_STREAM_TYPE,
    DOMAIN,
    MAX_JPEG_QUALITY,
    MIN_JPEG_QUALITY,
    STREAM_TYPES,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): vol.All(str, vol.Length(min=1)),
    }
)


def _discover_devices(username: str, password: str) -> list[HikDevice]:
    client = HikConnectClient(username, password, DEFAULT_API_HOST)
    try:
        client.login()
        return client.get_devices()
    finally:
        client.close()


def _discover_channels(username: str, password: str, serial: str) -> list[HikChannel]:
    client = HikConnectClient(username, password, DEFAULT_API_HOST)
    try:
        client.login()
        return client.get_channels(serial)
    finally:
        client.close()


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Let the user choose a Hik-Connect device and linked channel."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._devices: dict[str, HikDevice] = {}
        self._channels: dict[int, HikChannel] = {}
        self._selected_device: HikDevice | None = None

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "OptionsFlowHandler":
        return OptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)
        try:
            devices = await self.hass.async_add_executor_job(
                _discover_devices, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
        except HikConnectAuthError:
            return self.async_show_form(
                step_id="user", data_schema=USER_SCHEMA, errors={"base": "auth"}
            )
        except Exception:
            return self.async_show_form(
                step_id="user", data_schema=USER_SCHEMA, errors={"base": "cannot_connect"}
            )
        if not devices:
            return self.async_show_form(
                step_id="user", data_schema=USER_SCHEMA, errors={"base": "no_devices"}
            )
        self._credentials = {
            CONF_USERNAME: user_input[CONF_USERNAME],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
        }
        self._devices = {device.serial: device for device in devices}
        return await self.async_step_device()

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        options = {
            serial: f"{device.name} — {device.model or 'Hikvision device'} ({serial})"
            for serial, device in self._devices.items()
        }
        schema = vol.Schema({vol.Required(CONF_DEVICE_SERIAL): vol.In(options)})
        if user_input is None:
            return self.async_show_form(step_id="device", data_schema=schema)
        serial = str(user_input[CONF_DEVICE_SERIAL])
        device = self._devices[serial]
        try:
            channels = await self.hass.async_add_executor_job(
                _discover_channels,
                self._credentials[CONF_USERNAME],
                self._credentials[CONF_PASSWORD],
                serial,
            )
        except HikConnectAuthError:
            return self.async_show_form(
                step_id="device", data_schema=schema, errors={"base": "auth"}
            )
        except Exception:
            return self.async_show_form(
                step_id="device", data_schema=schema, errors={"base": "cannot_connect"}
            )
        linked = [channel for channel in channels if channel.linked]
        self._channels = {channel.channel: channel for channel in (linked or channels)}
        if not self._channels:
            return self.async_show_form(
                step_id="device", data_schema=schema, errors={"base": "no_channels"}
            )
        self._selected_device = device
        return await self.async_step_channel()

    async def async_step_channel(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        options = {channel: item.label for channel, item in self._channels.items()}
        schema = vol.Schema({vol.Required(CONF_CHANNEL): vol.In(options)})
        if user_input is None:
            return self.async_show_form(step_id="channel", data_schema=schema)
        channel = int(user_input[CONF_CHANNEL])
        selected = self._channels[channel]
        serial = selected.serial
        await self.async_set_unique_id(f"{serial}:{channel}")
        self._abort_if_unique_id_configured()
        device = self._selected_device
        data = {
            **self._credentials,
            CONF_API_HOST: DEFAULT_API_HOST,
            CONF_DEVICE_SERIAL: serial,
            CONF_CHANNEL: channel,
            CONF_CHANNEL_NAME: selected.name,
            CONF_DEVICE_NAME: device.name if device else serial,
            CONF_DEVICE_MODEL: device.model if device else "Hikvision",
        }
        return self.async_create_entry(
            title=f"{selected.name} ({serial}/{channel})", data=data
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Configure the cloud selector and browser/consumer relay output."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_STREAM_TYPE,
                    default=current.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE),
                ): vol.In(list(STREAM_TYPES)),
                vol.Required(
                    CONF_FPS, default=current.get(CONF_FPS, DEFAULT_FPS)
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=60)),
                vol.Required(
                    CONF_JPEG_QUALITY,
                    default=current.get(CONF_JPEG_QUALITY, DEFAULT_JPEG_QUALITY),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_JPEG_QUALITY, max=MAX_JPEG_QUALITY),
                ),
                vol.Required(
                    CONF_RELAY_HOST,
                    default=current.get(CONF_RELAY_HOST, DEFAULT_RELAY_HOST),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
