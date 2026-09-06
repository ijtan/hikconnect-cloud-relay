"""Constants for the Hikvision Intercom integration."""

from __future__ import annotations

DOMAIN = "hikconnect_cloud_relay"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_API_HOST = "api_host"
CONF_DEVICE_SERIAL = "device_serial"
CONF_CHANNEL = "channel"
CONF_CHANNEL_NAME = "channel_name"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_MODEL = "device_model"
CONF_STREAM_TYPE = "stream_type"
CONF_FPS = "fps"
CONF_JPEG_QUALITY = "jpeg_quality"
CONF_RELAY_HOST = "relay_host"
CONF_RTSP_PUBLISH_URL = "rtsp_publish_url"
CONF_OUTPUT_MODE = "output_mode"

DEFAULT_API_HOST = "https://api.hik-connect.com"
DEFAULT_STREAM_TYPE = 1
DEFAULT_FPS = 0.0
DEFAULT_JPEG_QUALITY = 5
DEFAULT_RELAY_HOST = "127.0.0.1"
DEFAULT_RTSP_PUBLISH_URL = ""
OUTPUT_MODE_LEGACY = "legacy"
OUTPUT_MODE_RTSP = "rtsp"
OUTPUT_MODE_BOTH = "both"
OUTPUT_MODES = (OUTPUT_MODE_LEGACY, OUTPUT_MODE_RTSP, OUTPUT_MODE_BOTH)
DEFAULT_OUTPUT_MODE = OUTPUT_MODE_LEGACY

STREAM_TYPES = (1, 2, 3)
MIN_JPEG_QUALITY = 2
MAX_JPEG_QUALITY = 31
