"""Download and supervise the local MediaMTX RTSP server."""

from __future__ import annotations

from io import BytesIO
import logging
import os
import platform
from pathlib import Path
import stat
import subprocess
import tarfile
import threading
import time
import zipfile

import requests

_LOGGER = logging.getLogger(__name__)

MEDIAMTX_VERSION = "v1.19.3"
_RELEASES_BASE = (
    "https://github.com/bluenviron/mediamtx/releases/download/"
    f"{MEDIAMTX_VERSION}"
)


def _os_name() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "windows"
    return "linux"


def _arch_name() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("armv7") or machine == "armv7l":
        return "armv7"
    if machine.startswith("armv6") or machine == "armv6l":
        return "armv6"
    if machine in ("i386", "i686", "x86"):
        return "386"
    raise RuntimeError(f"Unsupported architecture for MediaMTX: {machine}")


def _asset_name() -> str:
    os_name = _os_name()
    ext = "zip" if os_name == "windows" else "tar.gz"
    return f"mediamtx_{MEDIAMTX_VERSION}_{os_name}_{_arch_name()}.{ext}"


def _write_member(destination: Path, data: bytes) -> Path:
    destination.write_bytes(data)
    if _os_name() != "windows":
        destination.chmod(
            destination.stat().st_mode
            | stat.S_IEXEC
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    return destination


def ensure_mediamtx(storage_dir: Path) -> Path:
    """Return a cached MediaMTX executable, downloading it if necessary."""

    storage_dir.mkdir(parents=True, exist_ok=True)
    executable_name = "mediamtx.exe" if _os_name() == "windows" else "mediamtx"
    executable = storage_dir / executable_name
    if executable.exists():
        return executable

    asset = _asset_name()
    response = requests.get(f"{_RELEASES_BASE}/{asset}", timeout=60)
    response.raise_for_status()
    archive = BytesIO(response.content)

    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as archive_file:
            member = next(
                (
                    name
                    for name in archive_file.namelist()
                    if Path(name).name == executable_name
                ),
                None,
            )
            if member is None:
                raise RuntimeError(
                    f"MediaMTX archive did not contain {executable_name}"
                )
            return _write_member(executable, archive_file.read(member))

    with tarfile.open(fileobj=archive, mode="r:gz") as archive_file:
        member = next(
            (
                item
                for item in archive_file.getmembers()
                if Path(item.name).name == executable_name
            ),
            None,
        )
        if member is None:
            raise RuntimeError(
                f"MediaMTX archive did not contain {executable_name}"
            )
        extracted = archive_file.extractfile(member)
        if extracted is None:
            raise RuntimeError(
                f"MediaMTX archive member {executable_name} is unreadable"
            )
        return _write_member(executable, extracted.read())


class MediaMtxServer:
    """Keep a local MediaMTX publisher/reader boundary available."""

    _CHECK_INTERVAL = 2.0

    def __init__(self, executable: Path, rtsp_port: int) -> None:
        self._executable = executable
        self._rtsp_port = rtsp_port
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "stopped"
        self._restarts = 0

    @property
    def status(self) -> str:
        with self._process_lock:
            return self._status

    @property
    def restarts(self) -> int:
        with self._process_lock:
            return self._restarts

    def start(self) -> None:
        """Start MediaMTX and its process supervisor once."""

        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._stop_event.clear()
            self._launch_locked()
            self._thread = threading.Thread(
                target=self._monitor,
                name="hikvision-mediamtx-supervisor",
                daemon=True,
            )
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """Stop MediaMTX and its supervisor."""

        self._stop_event.set()
        with self._process_lock:
            process = self._process
            thread = self._thread
        self._terminate(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._process_lock:
            self._process = None
            self._thread = None
            self._status = "stopped"

    def _launch_locked(self) -> None:
        process = subprocess.Popen(
            [str(self._executable)],
            env={
                **os.environ,
                "MTX_RTSPADDRESS": f":{self._rtsp_port}",
                "MTX_RTSPTRANSPORTS": "tcp",
                "MTX_RTMP": "no",
                "MTX_HLS": "no",
                "MTX_WEBRTC": "no",
                "MTX_SRT": "no",
                "MTX_MOQ": "no",
                "MTX_API": "no",
                "MTX_METRICS": "no",
                "MTX_PLAYBACK": "no",
                "MTX_LOGLEVEL": "warn",
                "MTX_PATHS_ALL_SOURCE": "publisher",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._process = process
        self._status = "starting"
        _LOGGER.info(
            "MediaMTX started (pid %s), RTSP on port %s",
            process.pid,
            self._rtsp_port,
        )
        time.sleep(1.0)
        if process.poll() is not None:
            self._process = None
            self._status = "error"
            raise RuntimeError(
                f"MediaMTX exited during startup with code {process.returncode}"
            )
        self._status = "running"

    def _monitor(self) -> None:
        while not self._stop_event.wait(self._CHECK_INTERVAL):
            with self._process_lock:
                process = self._process
                exited = process is None or process.poll() is not None
                if not exited:
                    continue
                if self._stop_event.is_set():
                    return
                return_code = process.returncode if process is not None else "unknown"
                self._status = "restarting"
                try:
                    self._launch_locked()
                except Exception as exc:  # noqa: BLE001 - keep the stream supervisor alive
                    self._status = "error"
                    _LOGGER.warning(
                        "MediaMTX exited (%s); restart failed: %s",
                        return_code,
                        exc,
                    )
                    continue
                self._restarts += 1
                _LOGGER.warning("MediaMTX exited (%s); restarted", return_code)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
