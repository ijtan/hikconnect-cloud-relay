"""Minimal unencrypted Hik/EZVIZ VTM ysproto client."""

from __future__ import annotations

from dataclasses import dataclass
import socket
import time
from typing import Iterator
from urllib.parse import parse_qsl, urlparse

VTM_MAGIC = 0x24
MESSAGE = 0x00
STREAM = 0x01
ENCRYPTED_STREAM = 0x0B
KEEPALIVE_REQ = 0x132
KEEPALIVE_RSP = 0x133
STREAMINFO_REQ = 0x13B
STREAMINFO_RSP = 0x13C


class VtmError(RuntimeError):
    """VTM framing or stream negotiation failed."""


@dataclass(frozen=True)
class VtmPacket:
    channel: int
    sequence: int
    message_code: int
    body: bytes


@dataclass(frozen=True)
class StreamInfo:
    result: int | None
    stream_session: str | None
    redirect_url: str | None
    redirect_key: str | None


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise VtmError("negative protobuf varints are unsupported")
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(data):
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
        if shift >= 64:
            break
    raise VtmError("malformed protobuf varint")


def _proto_bytes(field: int, value: bytes) -> bytes:
    return _encode_varint((field << 3) | 2) + _encode_varint(len(value)) + value


def _proto_string(field: int, value: str) -> bytes:
    return _proto_bytes(field, value.encode())


def _proto_varint(field: int, value: int) -> bytes:
    return _encode_varint(field << 3) + _encode_varint(value)


def _fields(data: bytes) -> dict[int, list[int | bytes]]:
    result: dict[int, list[int | bytes]] = {}
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        field, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, position = _read_varint(data, position)
        elif wire_type == 2:
            length, position = _read_varint(data, position)
            if position + length > len(data):
                raise VtmError("protobuf field exceeds response")
            value = data[position : position + length]
            position += length
        else:
            raise VtmError(f"unsupported protobuf wire type {wire_type}")
        result.setdefault(field, []).append(value)
    return result


def _last_int(values: dict[int, list[int | bytes]], field: int) -> int | None:
    value = (values.get(field) or [None])[-1]
    return value if isinstance(value, int) else None


def _last_string(values: dict[int, list[int | bytes]], field: int) -> str | None:
    value = (values.get(field) or [None])[-1]
    return value.decode(errors="replace") if isinstance(value, bytes) else None


def _stream_info_request(url: str, key: str | None = None) -> bytes:
    parts = [_proto_string(1, url)]
    if key:
        parts.append(_proto_string(2, key))
    parts.extend((_proto_string(3, "v3.6.3.20221124"), _proto_varint(4, 0), _proto_string(6, "v3.6.3.20221124")))
    return b"".join(parts)


def _keepalive_request(stream_session: str) -> bytes:
    return _proto_bytes(1, stream_session.encode())


def _packet(body: bytes, channel: int, code: int, sequence: int) -> bytes:
    if len(body) > 0xFFFF:
        raise VtmError("VTM body is too large")
    return bytes(
        (VTM_MAGIC, channel)
    ) + len(body).to_bytes(2, "big") + sequence.to_bytes(2, "big") + code.to_bytes(2, "big") + body


class VtmStreamClient:
    """Synchronous VTM client used from the relay worker thread."""

    def __init__(self, stream_url: str, timeout: float = 10.0) -> None:
        self.stream_url = stream_url
        self.timeout = timeout
        self.stream_session: str | None = None
        self._socket: socket.socket | None = None
        self._sequence = 0

    def connect(self) -> None:
        parsed = urlparse(self.stream_url)
        if parsed.scheme != "ysproto" or not parsed.hostname or parsed.port is None:
            raise VtmError("invalid VTM URL")
        self._socket = socket.create_connection((parsed.hostname, parsed.port), self.timeout)
        self._socket.settimeout(self.timeout)

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _send(self, body: bytes, channel: int, code: int) -> None:
        if self._socket is None:
            raise VtmError("VTM socket is not connected")
        packet = _packet(body, channel, code, self._sequence)
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._socket.sendall(packet)

    def _read_exact(self, length: int) -> bytes:
        if self._socket is None:
            raise VtmError("VTM socket is not connected")
        result = bytearray()
        while len(result) < length:
            chunk = self._socket.recv(length - len(result))
            if not chunk:
                raise VtmError("VTM socket closed")
            result.extend(chunk)
        return bytes(result)

    def _read_packet(self) -> VtmPacket:
        header = self._read_exact(8)
        if header[0] != VTM_MAGIC:
            raise VtmError("invalid VTM packet magic")
        length = int.from_bytes(header[2:4], "big")
        return VtmPacket(
            channel=header[1],
            sequence=int.from_bytes(header[4:6], "big"),
            message_code=int.from_bytes(header[6:8], "big"),
            body=self._read_exact(length),
        )

    def start(self) -> StreamInfo:
        redirect_key: str | None = None
        for _ in range(4):
            self.connect()
            self._send(_stream_info_request(self.stream_url, redirect_key), MESSAGE, STREAMINFO_REQ)
            redirected = False
            for _ in range(20):
                packet = self._read_packet()
                if packet.message_code != STREAMINFO_RSP:
                    continue
                values = _fields(packet.body)
                info = StreamInfo(
                    result=_last_int(values, 1),
                    stream_session=_last_string(values, 4),
                    redirect_url=_last_string(values, 7),
                    redirect_key=_last_string(values, 5),
                )
                if info.result and info.redirect_url and info.redirect_key:
                    self.close()
                    self.stream_url = info.redirect_url
                    redirect_key = info.redirect_key
                    redirected = True
                    break
                if info.result not in (0, None):
                    raise VtmError(f"VTM stream negotiation failed (code={info.result})")
                self.stream_session = info.stream_session
                return info
            self.close()
            if redirected:
                continue
        raise VtmError("timed out waiting for VTM stream info")

    def iter_packets(self) -> Iterator[VtmPacket]:
        last_keepalive = time.monotonic()
        while self._socket is not None:
            if self.stream_session and time.monotonic() - last_keepalive >= 5:
                self._send(_keepalive_request(self.stream_session), MESSAGE, KEEPALIVE_REQ)
                last_keepalive = time.monotonic()
            packet = self._read_packet()
            if packet.message_code == KEEPALIVE_REQ:
                if self.stream_session:
                    self._send(_keepalive_request(self.stream_session), MESSAGE, KEEPALIVE_RSP)
                last_keepalive = time.monotonic()
                continue
            if packet.channel in (STREAM, ENCRYPTED_STREAM):
                yield packet


def rtp_payload(data: bytes) -> bytes:
    """Strip a normal RTP header, including CSRC, extension and padding."""

    if len(data) < 12 or data[0] >> 6 != 2:
        return b""
    has_padding = bool(data[0] & 0x20)
    offset = 12 + 4 * (data[0] & 0x0F)
    if data[0] & 0x10:
        if len(data) < offset + 4:
            return b""
        offset += 4 + 4 * int.from_bytes(data[offset + 2 : offset + 4], "big")
    if offset > len(data):
        return b""
    payload = data[offset:]
    if has_padding:
        amount = payload[-1] if payload else 0
        if not amount or amount > len(payload):
            return b""
        payload = payload[:-amount]
    return payload


def rtp_timestamp(data: bytes) -> int | None:
    if len(data) < 8 or data[0] >> 6 != 2:
        return None
    return int.from_bytes(data[4:8], "big")
