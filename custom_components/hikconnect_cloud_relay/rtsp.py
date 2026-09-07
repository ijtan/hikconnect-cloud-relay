"""Optional H.264 stream-copy publishing through a local RTSP server."""

from __future__ import annotations

from collections import deque
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import BinaryIO
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)
_START_CODE = b"\x00\x00\x00\x01"
_DEFAULT_QUEUE_BYTES = 512 * 1024
_MAX_RETRY_DELAY = 30.0
_RTSP_PROBE_INTERVAL = 5.0
_RTSP_PROBE_GRACE_PERIOD = 10.0
_RTSP_PROBE_FAILURE_LIMIT = 2
RTSP_DEFAULT_HOST = "127.0.0.1"
RTSP_DEFAULT_PORT = 8554


def validate_rtsp_publish_url(value: str | None) -> str:
    """Validate an RTSP publisher URL without permitting credentials in argv."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("RTSP publish URL must be text")
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme.lower() != "rtsp":
        raise ValueError("RTSP publish URL must use the rtsp:// scheme")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("RTSP publish URL has an invalid host or port") from exc
    if not hostname:
        raise ValueError("RTSP publish URL must include a hostname")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("RTSP publish URL port must be between 1 and 65535")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("RTSP publish URLs must not contain credentials")
    if not parsed.path or not parsed.path.strip("/"):
        raise ValueError("RTSP publish URL must include a stream path")
    if parsed.fragment:
        raise ValueError("RTSP publish URL must not include a fragment")
    return value


def rtsp_stream_path(serial: str, channel: int) -> str:
    """Build a stable path without exposing Home Assistant's opaque entry ID."""

    safe_serial = re.sub(r"[^A-Za-z0-9_-]", "_", serial).strip("_") or "device"
    return f"/hikconnect/{safe_serial}_{int(channel)}"


def default_rtsp_publish_url(serial: str, channel: int) -> str:
    """Return the default local MediaMTX publisher URL for a linked channel."""

    return f"rtsp://{RTSP_DEFAULT_HOST}:{RTSP_DEFAULT_PORT}{rtsp_stream_path(serial, channel)}"


def rtsp_reader_url(host: str, serial: str, channel: int) -> str:
    """Return the LAN URL that external readers such as Frigate should use."""

    clean_host = host.strip()
    if "://" in clean_host:
        clean_host = clean_host.split("://", 1)[1].rstrip("/")
    return f"rtsp://{clean_host}:{RTSP_DEFAULT_PORT}{rtsp_stream_path(serial, channel)}"


def uses_managed_local_server(value: str) -> bool:
    """Return whether the integration should own the default local server."""

    parsed = urlparse(validate_rtsp_publish_url(value))
    return parsed.hostname in {"127.0.0.1", "localhost"} and (
        parsed.port or 554
    ) == RTSP_DEFAULT_PORT


def redact_rtsp_text(value: str | None) -> str | None:
    """Remove RTSP URLs from child-process diagnostics before exposing them."""

    if value is None:
        return None
    return re.sub(r"rtsp://[^\s'\"]+", "rtsp://<redacted>", value)


def build_rtsp_copy_command(ffmpeg: str, publish_url: str) -> list[str]:
    """Build an FFmpeg command that publishes raw Annex-B H.264 without encoding."""

    publish_url = validate_rtsp_publish_url(publish_url)
    if not publish_url:
        raise ValueError("RTSP publish URL is required")
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-xerror",
        "-fflags",
        "+genpts",
        "-framerate",
        "25",
        "-f",
        "h264",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "copy",
        "-rtsp_transport",
        "tcp",
        "-rw_timeout",
        "10000000",
        "-f",
        "rtsp",
        publish_url,
    ]


def rtsp_path_is_ready(publish_url: str, timeout: float = 2.0) -> bool:
    """Return whether an RTSP publisher path currently answers DESCRIBE."""

    parsed = urlparse(validate_rtsp_publish_url(publish_url))
    port = parsed.port or RTSP_DEFAULT_PORT
    request = (
        f"DESCRIBE {publish_url} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: hikconnect-cloud-relay\r\n"
        "\r\n"
    ).encode()
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout) as connection:
            connection.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 8192:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
    except OSError:
        return False
    return bytes(response).startswith(b"RTSP/1.0 200 ")


class H264CopyGate:
    """Wait for a decodable SPS/PPS/IDR point before publishing H.264."""

    def __init__(self) -> None:
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def reset(self, *, clear_parameter_sets: bool) -> None:
        """Require another IDR; clear cached headers when the source changes."""

        self._ready = False
        if clear_parameter_sets:
            self._sps = None
            self._pps = None

    def feed(self, nals: list[bytes]) -> list[bytes]:
        """Return NALs safe for a new copy-mode publisher, or an empty list."""

        clean = [nal for nal in nals if nal]
        if not clean:
            return []
        for nal in clean:
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                self._sps = nal
            elif nal_type == 8:
                self._pps = nal

        if self._ready:
            return clean
        if self._sps is None or self._pps is None:
            return []
        if not any(nal[0] & 0x1F == 5 for nal in clean):
            return []

        prefix: list[bytes] = []
        if not any(nal[0] & 0x1F == 7 for nal in clean):
            prefix.append(self._sps)
        if not any(nal[0] & 0x1F == 8 for nal in clean):
            prefix.append(self._pps)
        self._ready = True
        return prefix + clean


class _ByteQueue:
    """A bounded queue that drops a complete pending stream on overflow."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("RTSP queue size must be positive")
        self._max_bytes = max_bytes
        self._items: deque[bytes] = deque()
        self._bytes = 0
        self._condition = threading.Condition()

    @property
    def bytes(self) -> int:
        with self._condition:
            return self._bytes

    def put(self, item: bytes) -> bool:
        """Queue one access-unit chunk without waiting for the RTSP child."""

        with self._condition:
            if len(item) > self._max_bytes or self._bytes + len(item) > self._max_bytes:
                self._items.clear()
                self._bytes = 0
                self._condition.notify_all()
                return False
            self._items.append(item)
            self._bytes += len(item)
            self._condition.notify()
            return True

    def get(self, timeout: float) -> bytes | None:
        with self._condition:
            if not self._items:
                self._condition.wait(timeout)
            if not self._items:
                return None
            item = self._items.popleft()
            self._bytes -= len(item)
            return item

    def clear(self) -> None:
        with self._condition:
            self._items.clear()
            self._bytes = 0
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()


class RtspCopyPublisher:
    """Publish a copy-mode H.264 stream without coupling it to the cloud loop."""

    def __init__(
        self,
        publish_url: str,
        *,
        ffmpeg: str = "ffmpeg",
        max_queue_bytes: int = _DEFAULT_QUEUE_BYTES,
    ) -> None:
        self.publish_url = validate_rtsp_publish_url(publish_url)
        if not self.publish_url:
            raise ValueError("RTSP publish URL is required")
        self._ffmpeg = ffmpeg
        self._queue = _ByteQueue(max_queue_bytes)
        self._probe_url = self.publish_url if uses_managed_local_server(self.publish_url) else None
        self._gate = H264CopyGate()
        self._stop_event = threading.Event()
        self._restart_event = threading.Event()
        self._state_lock = threading.Lock()
        self._status = "stopped"
        self._last_error: str | None = None
        self._metrics_lock = threading.Lock()
        self._restarts = 0
        self._dropped_chunks = 0
        self._dropped_bytes = 0
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    def _set_status(self, status: str) -> None:
        with self._state_lock:
            self._status = status

    def start(self) -> None:
        """Start the publisher supervisor once."""

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._restart_event.clear()
            self._status = "starting"
            self._thread = threading.Thread(
                target=self._run,
                name="hikvision-rtsp-publisher",
                daemon=True,
            )
            thread = self._thread
        self._gate.reset(clear_parameter_sets=True)
        self._queue.clear()
        thread.start()

    def stop(self) -> None:
        """Stop the supervisor and its FFmpeg child."""

        self._stop_event.set()
        self._restart_event.set()
        self._queue.wake()
        self._terminate_active_process()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=12)
        self._set_status("stopped")

    def reset(self) -> None:
        """Reset the publisher after the cloud source reconnects."""

        self._gate.reset(clear_parameter_sets=True)
        self._queue.clear()
        self._restart_event.set()
        self._queue.wake()
        self._terminate_active_process()

    def submit(self, nals: list[bytes]) -> None:
        """Submit NALs without blocking the VTM reader thread."""

        if self._stop_event.is_set():
            return
        ready = self._gate.feed(nals)
        if not ready:
            if self.status not in {"reconnecting", "error", "stopped"}:
                self._set_status("waiting_for_keyframe")
            return
        chunk = b"".join(_START_CODE + nal for nal in ready)
        if not self._queue.put(chunk):
            self._gate.reset(clear_parameter_sets=False)
            with self._metrics_lock:
                self._dropped_chunks += 1
                self._dropped_bytes += len(chunk)
            self._set_status("waiting_for_keyframe")
            return
        self._set_status("streaming")

    def stats(self) -> dict[str, object]:
        with self._state_lock:
            status = self._status
        with self._metrics_lock:
            restarts = self._restarts
            dropped_chunks = self._dropped_chunks
            dropped_bytes = self._dropped_bytes
            last_error = self._last_error
        return {
            "enabled": True,
            "status": status,
            "restarts": restarts,
            "dropped_chunks": dropped_chunks,
            "dropped_bytes": dropped_bytes,
            "queued_bytes": self._queue.bytes,
            "last_error": last_error,
        }

    def _run(self) -> None:
        retry_delay = 2.0
        while not self._stop_event.is_set():
            try:
                process = self._start_process()
            except Exception as exc:  # noqa: BLE001
                self._record_error(exc)
                self._set_status("error")
                self._note_restart()
                self._gate.reset(clear_parameter_sets=False)
                if self._stop_event.wait(retry_delay):
                    break
                retry_delay = min(retry_delay * 2, _MAX_RETRY_DELAY)
                continue

            self._set_status("waiting_for_keyframe")
            self._forward_to_process(process)
            self._cleanup_process(process)
            if self._stop_event.is_set():
                break

            self._note_restart()
            self._gate.reset(clear_parameter_sets=False)
            self._queue.clear()
            self._set_status("reconnecting")
            if self._restart_event.is_set():
                self._restart_event.clear()
                retry_delay = 2.0
                continue
            if self._stop_event.wait(retry_delay):
                break
            retry_delay = min(retry_delay * 2, _MAX_RETRY_DELAY)

    def _start_process(self) -> subprocess.Popen[bytes]:
        ffmpeg = shutil.which(self._ffmpeg) or self._ffmpeg
        command = build_rtsp_copy_command(ffmpeg, self.publish_url)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self._process_lock:
            self._process = process
        if process.stderr is not None:
            threading.Thread(
                target=self._drain_stderr,
                args=(process.stderr,),
                name="hikvision-rtsp-stderr",
                daemon=True,
            ).start()
        threading.Thread(
            target=self._watch_process,
            args=(process,),
            name="hikvision-rtsp-watchdog",
            daemon=True,
        ).start()
        return process

    def _forward_to_process(self, process: subprocess.Popen[bytes]) -> None:
        stdin = process.stdin
        if stdin is None:
            self._record_error(RuntimeError("RTSP FFmpeg stdin is unavailable"))
            return
        while not self._stop_event.is_set():
            if self._restart_event.is_set():
                self._restart_event.clear()
                return
            if process.poll() is not None:
                return
            chunk = self._queue.get(0.5)
            if chunk is None:
                continue
            try:
                stdin.write(chunk)
                stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._record_error(exc)
                return

    def _watch_process(self, process: subprocess.Popen[bytes]) -> None:
        """Detect a lost publisher even if forwarding blocks on FFmpeg stdin."""

        started_at = time.monotonic()
        last_probe = started_at
        probe_failures = 0
        while not self._stop_event.is_set() and process.poll() is None:
            now = time.monotonic()
            if self.status != "streaming":
                probe_failures = 0
            if (
                self._probe_url is not None
                and self.status == "streaming"
                and now - started_at >= _RTSP_PROBE_GRACE_PERIOD
                and now - last_probe >= _RTSP_PROBE_INTERVAL
            ):
                last_probe = now
                if not rtsp_path_is_ready(self._probe_url):
                    probe_failures += 1
                    if probe_failures >= _RTSP_PROBE_FAILURE_LIMIT:
                        self._set_status("error")
                        self._record_error(RuntimeError("RTSP publisher path is unavailable"))
                        self._terminate_process(process)
                        return
                else:
                    probe_failures = 0
            if self._stop_event.wait(0.5):
                return

    def _drain_stderr(self, stderr: BinaryIO) -> None:
        while True:
            try:
                line = stderr.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            text = line.decode(errors="replace").strip()
            if text:
                with self._metrics_lock:
                    self._last_error = redact_rtsp_text(text)

    def _cleanup_process(self, process: subprocess.Popen[bytes]) -> None:
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
            process.wait(timeout=3)
        if process.stderr is not None:
            try:
                process.stderr.close()
            except OSError:
                pass
        with self._process_lock:
            if self._process is process:
                self._process = None

    def _terminate_active_process(self) -> None:
        with self._process_lock:
            process = self._process
        if process is not None:
            self._terminate_process(process)

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._process_lock:
            if self._process is not process:
                return
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                return
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    return
                process.wait(timeout=1)

    def _record_error(self, error: Exception) -> None:
        text = redact_rtsp_text(f"{type(error).__name__}: {error}")
        with self._metrics_lock:
            self._last_error = text
        _LOGGER.warning("H.264 RTSP publisher error: %s", text)

    def _note_restart(self) -> None:
        with self._metrics_lock:
            self._restarts += 1
