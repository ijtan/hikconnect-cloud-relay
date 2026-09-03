"""Hikvision Intercom Home Assistant integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_API_HOST,
    CONF_CHANNEL,
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
    DEFAULT_STREAM_TYPE,
    DOMAIN,
)
from .media_view import HikvisionMediaView
from .relay import CloudRelay

_PLATFORMS = [Platform.CAMERA]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the local media endpoint once."""

    hass.data.setdefault(DOMAIN, {})
    if not hass.data[DOMAIN].get("_view_registered"):
        hass.http.register_view(HikvisionMediaView())
        hass.data[DOMAIN]["_view_registered"] = True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start one cloud relay for the selected linked channel."""

    options = entry.options
    data = entry.data
    relay = CloudRelay(
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data.get(CONF_API_HOST, DEFAULT_API_HOST),
        data[CONF_DEVICE_SERIAL],
        int(data[CONF_CHANNEL]),
        int(options.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)),
        float(options.get(CONF_FPS, DEFAULT_FPS)),
        int(options.get(CONF_JPEG_QUALITY, DEFAULT_JPEG_QUALITY)),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "relay": relay,
        "entry": entry,
        "relay_host": str(options.get(CONF_RELAY_HOST, "127.0.0.1")),
    }
    relay.start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the relay and unload the camera entity."""

    unloaded = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime:
        await hass.async_add_executor_job(runtime["relay"].stop)
    return unloaded
