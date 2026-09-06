"""Home Assistant camera entity backed by the cloud VTM relay."""

from __future__ import annotations

import queue
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CHANNEL,
    CONF_CHANNEL_NAME,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SERIAL,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HikvisionIntercomCamera(hass, entry, runtime)])


class HikvisionIntercomCamera(Camera):
    """Selected linked door-station channel as a stream-capable camera."""

    _attr_has_entity_name = True
    _attr_translation_key = "live"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_frame_interval = 0.1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__()
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        data = entry.data
        serial = str(data[CONF_DEVICE_SERIAL])
        channel = int(data[CONF_CHANNEL])
        self._attr_unique_id = f"{DOMAIN}_{serial}_{channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=str(data.get(CONF_DEVICE_NAME) or serial),
            manufacturer="Hikvision",
            model=str(data.get(CONF_DEVICE_MODEL) or "Hikvision intercom"),
        )
        self._attr_name = str(data.get(CONF_CHANNEL_NAME) or f"Channel {channel}")

    @property
    def available(self) -> bool:
        return self._runtime["relay"].status != "stopped"

    @property
    def is_streaming(self) -> bool:
        return self._runtime["relay"].status == "streaming"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        stats = self._runtime["relay"].stats()
        return {
            "stream_status": stats["status"],
            "output_mode": stats["output_mode"],
            "stream_type": stats["stream_type"],
            "source_picture_timestamps": stats["picture_timestamps"],
            "jpeg_frames": stats["jpeg_frames"],
            "rtsp_enabled": stats["rtsp_enabled"],
            "rtsp_status": stats["rtsp_status"],
            "rtsp_restarts": stats["rtsp_restarts"],
            "rtsp_server_enabled": stats["rtsp_server_enabled"],
            "rtsp_server_status": stats["rtsp_server_status"],
            "rtsp_server_restarts": stats["rtsp_server_restarts"],
            "rtsp_reader_url": self._runtime.get("rtsp_reader_url"),
            "relay_stats_url": self._url("stats"),
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._runtime["relay"].rtsp_enabled:
            return await async_get_image(
                self.hass,
                self._runtime["rtsp_reader_url"],
                width=width,
                height=height,
            )
        try:
            return await self.hass.async_add_executor_job(self._runtime["relay"].snapshot, 15.0)
        except queue.Empty:
            return None

    async def stream_source(self) -> str | None:
        if self._runtime["relay"].rtsp_enabled:
            return self._runtime.get("rtsp_reader_url")
        return self._url("stream.mjpeg")

    def _url(self, resource: str) -> str:
        host = self._runtime.get("relay_host") or "127.0.0.1"
        host = str(host).strip()
        if "://" in host:
            host = host.split("://", 1)[1].rstrip("/")
        port = getattr(self.hass.http, "server_port", 8123)
        scheme = "https" if getattr(self.hass.config.api, "use_ssl", False) else "http"
        return f"{scheme}://{host}:{port}/api/{DOMAIN}/{self._entry.entry_id}/{resource}"
