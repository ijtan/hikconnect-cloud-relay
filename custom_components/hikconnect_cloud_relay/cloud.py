"""Small Hik-Connect client used by the HACS integration.

This module intentionally implements the account/device/channel and VTM
bootstrap surface only. It does not contain HPP developer credentials, local
device writes, or native SDK bindings.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import logging
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

import requests

from .const import DEFAULT_API_HOST
from .vtm import VtmStreamClient

_LOGGER = logging.getLogger(__name__)

CLIENT_HEADERS = {"clientType": "55", "lang": "en-US"}
AUTH_HOST = "https://euauth.ezvizlife.com"
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MIN_DELAY = 1.0
RATE_LIMIT_MAX_DELAY = 60.0


class HikConnectError(RuntimeError):
    """A Hik-Connect request or media bootstrap failed."""


class HikConnectAuthError(HikConnectError):
    """Hik-Connect rejected the account credentials."""


def _meta(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("meta")
    return value if isinstance(value, dict) else {}


def _meta_code(payload: dict[str, Any]) -> int | None:
    try:
        return int(_meta(payload).get("code"))
    except (TypeError, ValueError):
        return None


def _meta_message(payload: dict[str, Any]) -> str:
    value = _meta(payload).get("message")
    return str(value) if value else ""


def _redact(value: str) -> str:
    """Keep errors useful without returning URLs or credential material."""

    text = str(value)
    for key in ("password", "token", "sign", "sessionId", "ssn", "auth"):
        text = text.replace(key + "=", key + "=[redacted]")
    return text[:400]


def _bool(value: Any) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class HikDevice:
    serial: str
    name: str
    model: str
    version: str
    local_ip: str | None


@dataclass(frozen=True)
class HikChannel:
    serial: str
    channel: int
    name: str
    signal_status: int | None
    related_ipc: bool
    stream_biz_url: str
    vtm_host: str | None
    vtm_port: int | None
    raw: dict[str, Any]

    @property
    def linked(self) -> bool:
        """Return whether this row looks like a usable linked channel."""

        # The API often gives every placeholder row the same VTM server
        # address. That address is not evidence that a channel has a usable
        # linked camera; signal/related-IPC state is the useful discriminator.
        return bool(self.related_ipc or self.signal_status == 1)

    @property
    def label(self) -> str:
        state = "linked" if self.linked else "inactive"
        return f"{self.channel}: {self.name} ({state})"


class HikConnectClient:
    """Synchronous account client; callers run it outside HA's event loop."""

    def __init__(self, username: str, password: str, base_url: str = DEFAULT_API_HOST) -> None:
        self.username = username.strip()
        self._password = password
        self.base_url = self._normalise_base(base_url)
        self._session = requests.Session()
        self._session.headers.update(CLIENT_HEADERS)
        self._session.headers["featureCode"] = hashlib.md5(
            f"hikconnect_cloud_relay:{self.username}".encode(), usedforsecurity=False
        ).hexdigest()[:16]
        self.session_id: str | None = None
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_until = 0.0
        self._rate_limit_delay = 0.0

    @staticmethod
    def _normalise_base(value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        return value.rstrip("/")

    def close(self) -> None:
        self._session.close()

    def login(self) -> None:
        password_hash = hashlib.md5(
            self._password.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        data = {"account": self.username, "password": password_hash}
        reply = self._raw("POST", "/v3/users/login/v2", data=data)
        if _meta_code(reply) == 1100:
            area = reply.get("loginArea") or {}
            redirect = str(area.get("apiDomain") or "")
            if not redirect:
                raise HikConnectError("Hik-Connect returned a region redirect without a domain")
            self.base_url = self._normalise_base(redirect)
            reply = self._raw("POST", "/v3/users/login/v2", data=data)

        code = _meta_code(reply)
        if code in (1013, 1014, 1226):
            raise HikConnectAuthError(
                f"Hik-Connect rejected the account/password (code={code})"
            )
        if code == 1015:
            raise HikConnectAuthError(
                "Hik-Connect requires CAPTCHA/MFA; open the Hik-Connect app once and retry"
            )
        login_session = reply.get("loginSession")
        if not isinstance(login_session, dict) or not login_session.get("sessionId"):
            raise HikConnectAuthError(
                f"Hik-Connect login failed (code={code}, message={_meta_message(reply) or 'unknown'})"
            )
        self.session_id = str(login_session["sessionId"])
        self._session.headers["sessionId"] = self.session_id
        # This call also confirms that the session can access the account API.
        self._raw("GET", "/v3/configurations/system/info")

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | tuple[float, float] = (10, 30),
    ) -> requests.Response:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            with self._rate_limit_lock:
                delay = max(0.0, self._rate_limit_until - time.monotonic())
            if delay:
                time.sleep(delay)
            try:
                response = self._session.request(
                    method, url, data=data, params=params, timeout=timeout
                )
            except requests.RequestException as exc:
                raise HikConnectError(f"Hik-Connect network error: {_redact(exc)}") from exc
            rate_limited = response.status_code == 429
            if not rate_limited:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    rate_limited = str(_meta(payload).get("code")) == "429"
            if not rate_limited:
                with self._rate_limit_lock:
                    self._rate_limit_delay = 0.0
                    self._rate_limit_until = 0.0
                return response

            retry_after = response.headers.get("Retry-After")
            try:
                requested = float(retry_after) if retry_after else 0.0
            except ValueError:
                requested = 0.0
            with self._rate_limit_lock:
                delay = max(requested, self._rate_limit_delay * 2 or RATE_LIMIT_MIN_DELAY)
                delay = min(delay, RATE_LIMIT_MAX_DELAY)
                self._rate_limit_delay = delay
                self._rate_limit_until = time.monotonic() + delay
            response.close()
            if attempt == RATE_LIMIT_RETRIES:
                raise HikConnectError("Hik-Connect rate limit persisted after bounded retries")
        raise HikConnectError("Hik-Connect request retry loop ended unexpectedly")

    def _raw(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(method, f"{self.base_url}{path}", data=data)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HikConnectError(
                f"{method} {path}: non-JSON response ({response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise HikConnectError(f"{method} {path}: unexpected JSON response")
        if response.status_code >= 500:
            raise HikConnectError(f"{method} {path}: Hik-Connect server error")
        return payload

    def _call(self, method: str, path: str) -> dict[str, Any]:
        if not self.session_id:
            raise HikConnectError("call login() first")
        reply = self._raw(method, path)
        if _meta_code(reply) == 401:
            self.login()
            reply = self._raw(method, path)
        return reply

    def get_devices(self) -> list[HikDevice]:
        """Return devices visible to this Hik-Connect account."""

        devices: list[HikDevice] = []
        limit, offset = 50, 0
        while True:
            path = (
                "/v3/userdevices/v1/devices/pagelist"
                f"?groupId=-1&limit={limit}&offset={offset}"
                "&filter=CONNECTION,STATUS,STATUS_EXT,WIFI,P2P"
            )
            reply = self._call("GET", path)
            connections = reply.get("connectionInfos") or {}
            for item in reply.get("deviceInfos") or []:
                if not isinstance(item, dict):
                    continue
                serial = str(item.get("deviceSerial") or "")
                if not serial:
                    continue
                connection = connections.get(serial) or {}
                local_ip = connection.get("localIp")
                devices.append(
                    HikDevice(
                        serial=serial,
                        name=str(item.get("name") or serial),
                        model=str(item.get("deviceType") or ""),
                        version=str(item.get("version") or ""),
                        local_ip=str(local_ip) if local_ip and local_ip != "0.0.0.0" else None,
                    )
                )
            page = reply.get("page") or {}
            if not page.get("hasNext"):
                break
            offset += limit
        return devices

    def get_channels(self, serial: str) -> list[HikChannel]:
        """Return the account's channel rows, including linked-channel metadata."""

        reply = self._call(
            "GET", f"/v3/userdevices/v1/cameras/info?deviceSerial={quote(serial)}"
        )
        channels: list[HikChannel] = []
        for item in reply.get("cameraInfos") or []:
            if not isinstance(item, dict):
                continue
            try:
                channel = int(item.get("channelNo"))
            except (TypeError, ValueError):
                continue
            info = item.get("deviceChannelInfo") or {}
            vtm = item.get("vtmInfo") or {}
            signal = info.get("signalStatus")
            try:
                signal_status = int(signal)
            except (TypeError, ValueError):
                signal_status = None
            try:
                port = int(vtm.get("port")) if vtm.get("port") else None
            except (TypeError, ValueError):
                port = None
            channels.append(
                HikChannel(
                    serial=serial,
                    channel=channel,
                    name=str(item.get("cameraName") or f"Channel {channel}"),
                    signal_status=signal_status,
                    related_ipc=_bool(info.get("relatedIpc"))
                    or bool(info.get("ipcSerial")),
                    stream_biz_url=str(item.get("streamBizUrl") or "biz=1"),
                    vtm_host=str(vtm.get("domain") or vtm.get("externalIp") or "") or None,
                    vtm_port=port,
                    raw=item,
                )
            )
        return sorted(channels, key=lambda value: value.channel)

    def _vtm_token(self) -> str:
        if not self.session_id:
            raise HikConnectError("call login() first")
        parts = self.session_id.split(".")
        if len(parts) < 2:
            raise HikConnectError("Hik-Connect session is not a JWT")
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HikConnectError("could not decode the Hik-Connect session") from exc
        sign = claims.get("s") if isinstance(claims, dict) else None
        if not isinstance(sign, str) or not sign:
            raise HikConnectError("Hik-Connect session has no VTM sign claim")
        response = self._request(
            "GET", f"{AUTH_HOST}/vtdutoken2", params={"ssid": self.session_id, "sign": sign}
        )
        try:
            reply = response.json()
        except ValueError as exc:
            raise HikConnectError("VTM token service returned non-JSON data") from exc
        if not isinstance(reply, dict) or reply.get("retcode") not in (0, "0"):
            raise HikConnectError("VTM token request failed")
        tokens = reply.get("tokens")
        if not isinstance(tokens, list) or not tokens or not isinstance(tokens[0], str):
            raise HikConnectError("VTM token response did not contain a token")
        return tokens[0]

    def open_vtm_stream(
        self, serial: str, channel: int, stream_type: int = 1, timeout: float = 10.0
    ) -> VtmStreamClient:
        rows = self.get_channels(serial)
        selected = next((row for row in rows if row.channel == channel), None)
        if selected is None:
            raise HikConnectError(f"channel {channel} is not present for {serial}")
        host, port = selected.vtm_host, selected.vtm_port
        if not host or not port:
            info = self._call("GET", f"/v3/streaming/vtm/{serial}/{channel}")
            config = info.get("streamServerConfig") or {}
            host = str(config.get("domain") or config.get("externalIp") or "")
            try:
                port = int(config.get("port"))
            except (TypeError, ValueError) as exc:
                raise HikConnectError("Hik-Connect returned no usable VTM server") from exc
        params = dict(parse_qsl(selected.stream_biz_url.lstrip("?"), keep_blank_values=True))
        params.update(
            {
                "dev": serial,
                "chn": str(channel),
                "stream": str(stream_type),
                "cln": "9",
                "isp": "0",
                "auth": "1",
                "ssn": self._vtm_token(),
                "vip": "0",
                "timestamp": str(int(time.time() * 1000)),
            }
        )
        url = f"ysproto://{host}:{port}/live?{urlencode(params)}"
        return VtmStreamClient(url, timeout=timeout)
