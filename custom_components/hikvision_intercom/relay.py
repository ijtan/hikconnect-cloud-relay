"""Cloud VTM to MJPEG relay for Home Assistant and local consumers."""

from __future__ import annotations

from collections.abc import Iterator
import logging
import queue
import shutil
import subprocess
import threading
import time
from typing import BinaryIO

from .cloud import HikConnectClient
from .vtm import rtp_payload, rtp_timestamp

_LOGGER = logging.getLogger(__name__)


class H264Depacketizer:
    """Reassemble RFC 6184 single-NAL, STAP-A and FU-A packets."""

    def __init__(self) -> None:
        self._fragment = bytearray()
        self._fragment_active = False

    def feed(self, payload: bytes) -> Iterator[bytes]:
        if not payload:
            return
        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            yield payload
            return
        if nal_type == 24:
            position = 1
            while position + 2 <= len(payload):
                length = int.from_bytes(payload[position : position + 2], "big")
                position += 2
                if not length or position + length > len(payload):
                    return
                yield payload[position : position + length]
                position += length
            return
        if nal_type != 28 or len(payload) < 2:
            return
        indicator, header = payload[0], payload[1]
        start, end = bool(header & 0x80), bool(header & 0x40)
        if start:
            self._fragment = bytearray(
                bytes([(indicator & 0xE0) | (header & 0x1F)]) + payload[2:]
            )
            self._fragment_active = True
        elif self._fragment_active:
            self._fragment.extend(payload[2:])
        else:
            return
        if end and self._fragment_active:
            result = bytes(self._fragment)
            self._fragment.clear()
            self._fragment_active = False
            yield result


class H264ParameterSetInjector:
    """Make a stream decodable even when a relay joins between keyframes."""

    def __init__(self) -> None:
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._sent_parameter_sets = False

    def feed(self, nals: list[bytes]) -> list[bytes]:
        for nal in nals:
            if not nal:
                continue
            if nal[0] & 0x1F == 7:
                self._sps = nal
            elif nal[0] & 0x1F == 8:
                self._pps = nal
        if not nals:
            return []
        has_parameter_set = any(
            nal and (nal[0] & 0x1F) in (7, 8) for nal in nals
        )
        result: list[bytes] = []
        has_vcl = False
        for nal in nals:
            if not nal:
                continue
            if (nal[0] & 0x1F) in (1, 2, 3, 4, 5):
                has_vcl = True
                if self._sps is None or self._pps is None:
                    continue
                if not self._sent_parameter_sets and not has_parameter_set:
                    result.extend((self._sps, self._pps))
                    self._sent_parameter_sets = True
            result.append(nal)
        if has_parameter_set and has_vcl and self._sps is not None and self._pps is not None:
            self._sent_parameter_sets = True
        return result


class FrameBuffer:
    """Keep the latest frame and provide bounded per-client queues."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._clients: set[queue.Queue[bytes]] = set()

    def subscribe(self) -> queue.Queue[bytes]:
        client: queue.Queue[bytes] = queue.Queue(maxsize=2)
        with self._lock:
            self._clients.add(client)
            if self._latest is not None:
                client.put_nowait(self._latest)
        return client

    def unsubscribe(self, client: queue.Queue[bytes]) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, frame: bytes) -> None:
        with self._lock:
            self._latest = frame
            clients = tuple(self._clients)
        for client in clients:
            try:
                client.put_nowait(frame)
            except queue.Full:
                try:
                    client.get_nowait()
                except queue.Empty:
                    pass
                try:
                    client.put_nowait(frame)
                except queue.Full:
                    pass

    def snapshot(self, timeout: float = 15.0) -> bytes:
        client = self.subscribe()
        try:
            return client.get(timeout=timeout)
        finally:
            self.unsubscribe(client)


class CloudRelay:
    """Maintain one reconnecting cloud session and publish MJPEG frames."""

    def __init__(
        self,
        username: str,
        password: str,
        api_host: str,
        serial: str,
        channel: int,
        stream_type: int,
        fps: float,
        jpeg_quality: int,
    ) -> None:
        self.username = username
        self.password = password
        self.api_host = api_host
        self.serial = serial
        self.channel = channel
        self.stream_type = stream_type
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.frames = FrameBuffer()
        self.stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._status = "starting"
        self._error: str | None = None
        self._reconnect_attempt = 0
        self._next_retry_at = 0.0
        self._metrics_lock = threading.Lock()
        self._rtp_packets = 0
        self._nals = 0
        self._picture_timestamps = 0
        self._last_rtp_timestamp: int | None = None
        self._jpeg_frames = 0
        self._jpeg_bytes = 0
        self._started_at = time.monotonic()
        self._active_lock = threading.Lock()
        self._active_stream = None
        self._thread = threading.Thread(target=self._run, name="hikvision-vtm", daemon=True)

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    def _set_state(self, status: str, error: str | None = None) -> None:
        with self._state_lock:
            self._status = status
            self._error = error

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self._active_lock:
            stream = self._active_stream
        if stream is not None:
            stream.close()
        self._thread.join(timeout=12)
        self._set_state("stopped")

    def snapshot(self, timeout: float = 15.0) -> bytes:
        return self.frames.snapshot(timeout)

    def subscribe(self) -> queue.Queue[bytes]:
        return self.frames.subscribe()

    def unsubscribe(self, client: queue.Queue[bytes]) -> None:
        self.frames.unsubscribe(client)

    def note_rtp(self) -> None:
        with self._metrics_lock:
            self._rtp_packets += 1

    def note_nals(self, count: int) -> None:
        with self._metrics_lock:
            self._nals += count

    def note_picture_timestamp(self, timestamp: int | None) -> None:
        if timestamp is None:
            return
        with self._metrics_lock:
            if timestamp != self._last_rtp_timestamp:
                self._picture_timestamps += 1
                self._last_rtp_timestamp = timestamp

    def note_jpeg(self, size: int) -> None:
        with self._metrics_lock:
            self._jpeg_frames += 1
            self._jpeg_bytes += size

    def stats(self) -> dict[str, object]:
        with self._state_lock:
            status, error = self._status, self._error
        with self._metrics_lock:
            values = {
                "rtp_packets": self._rtp_packets,
                "nals": self._nals,
                "picture_timestamps": self._picture_timestamps,
                "jpeg_frames": self._jpeg_frames,
                "jpeg_bytes": self._jpeg_bytes,
            }
        values.update(
            {
                "status": status,
                "error": error,
                "serial": self.serial,
                "channel": self.channel,
                "stream_type": self.stream_type,
                "fps_target": self.fps,
                "jpeg_quality": self.jpeg_quality,
                "uptime_seconds": round(time.monotonic() - self._started_at, 1),
                "reconnect_attempt": self._reconnect_attempt,
                "retry_in_seconds": round(max(0.0, self._next_retry_at - time.monotonic()), 1),
            }
        )
        return values

    def _run(self) -> None:
        retry_delay = 2.0
        while not self.stop_event.is_set():
            try:
                self._run_once()
            except Exception as exc:  # noqa: BLE001
                if self.stop_event.is_set():
                    break
                self._set_state("error", f"{type(exc).__name__}: {str(exc)[:300]}")
                _LOGGER.warning("Hikvision cloud relay stopped: %s", self._error)
                self._reconnect_attempt += 1
                delay = retry_delay
                retry_delay = min(retry_delay * 2, 120.0)
            else:
                self._reconnect_attempt = 0
                delay = 2.0
                retry_delay = 2.0
            self._next_retry_at = time.monotonic() + delay
            if self.stop_event.wait(delay):
                break
            self._next_retry_at = 0.0
            self._set_state("reconnecting")

    def _run_once(self) -> None:
        client = HikConnectClient(self.username, self.password, self.api_host)
        stream = None
        process: subprocess.Popen[bytes] | None = None
        output_thread: threading.Thread | None = None
        try:
            client.login()
            stream = client.open_vtm_stream(
                self.serial, self.channel, self.stream_type, timeout=10.0
            )
            with self._active_lock:
                self._active_stream = stream
            info = stream.start()
            _LOGGER.debug(
                "Hikvision VTM stream up serial=%s channel=%s result=%s",
                self.serial, self.channel, info.result
            )
            process = self._start_ffmpeg()
            output_thread = threading.Thread(
                target=self._publish_jpegs,
                args=(process.stdout,),
                name="hikvision-vtm-jpeg",
                daemon=True,
            )
            output_thread.start()
            decoder = H264Depacketizer()
            parameter_sets = H264ParameterSetInjector()
            self._set_state("streaming")
            with self._metrics_lock:
                self._last_rtp_timestamp = None
            stdin = process.stdin
            if stdin is None:
                raise RuntimeError("FFmpeg stdin is unavailable")
            for packet in stream.iter_packets():
                if self.stop_event.is_set():
                    break
                self.note_rtp()
                payload = rtp_payload(packet.body)
                if not payload:
                    continue
                self.note_picture_timestamp(rtp_timestamp(packet.body))
                nals = list(decoder.feed(payload))
                self.note_nals(len(nals))
                for nal in parameter_sets.feed(nals):
                    stdin.write(b"\x00\x00\x00\x01" + nal)
                if nals:
                    stdin.flush()
        finally:
            with self._active_lock:
                if self._active_stream is stream:
                    self._active_stream = None
            if stream is not None:
                stream.close()
            if process is not None:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                if output_thread is not None:
                    output_thread.join(timeout=2)
            client.close()

    def _start_ffmpeg(self) -> subprocess.Popen[bytes]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg executable was not found")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-an",
        ]
        if self.fps > 0:
            command.extend(("-vf", f"fps={self.fps:g}"))
        command.extend(("-q:v", str(self.jpeg_quality), "-f", "mjpeg", "pipe:1"))
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _publish_jpegs(self, output: BinaryIO | None) -> None:
        if output is None:
            return
        buffer = bytearray()
        while not self.stop_event.is_set():
            chunk = output.read(65536)
            if not chunk:
                return
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                self.note_jpeg(len(frame))
                self.frames.publish(frame)
