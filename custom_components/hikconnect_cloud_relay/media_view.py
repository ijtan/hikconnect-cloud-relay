"""Unauthenticated, local media endpoints for HA stream consumers."""

from __future__ import annotations

import asyncio
import queue
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN


class HikvisionMediaView(HomeAssistantView):
    """Serve the relay to HA's stream integration and local FFmpeg clients."""

    url = "/api/hikconnect_cloud_relay/{entry_id}/{resource}"
    name = "api:hikconnect_cloud_relay"
    requires_auth = False

    async def get(self, request: web.Request, entry_id: str, resource: str) -> web.StreamResponse:
        hass = request.app["hass"]
        runtime = hass.data.get(DOMAIN, {}).get(entry_id)
        if not runtime:
            return web.json_response({"error": "entry not found"}, status=404)
        relay = runtime["relay"]

        if resource == "stats":
            return web.json_response(relay.stats())
        if resource == "health":
            payload = relay.stats()
            return web.json_response(
                payload, status=200 if payload["status"] == "streaming" else 503
            )
        if resource == "snapshot.jpg":
            try:
                frame = await hass.async_add_executor_job(relay.snapshot, 15.0)
            except queue.Empty:
                return web.json_response({"error": "no frame available"}, status=504)
            return web.Response(
                body=frame,
                content_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )
        if resource == "stream.ts":
            return await self._stream_mpegts(request, relay)
        if resource != "stream.mjpeg":
            return web.json_response({"error": "not found"}, status=404)

        client = relay.subscribe()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=hikvisionframe",
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        try:
            await response.prepare(request)
            while True:
                try:
                    frame = await hass.async_add_executor_job(client.get, 30.0)
                except queue.Empty:
                    if relay.status in {"stopped", "error"}:
                        break
                    continue
                await response.write(
                    b"--hikvisionframe\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    + frame
                    + b"\r\n"
                )
        except (asyncio.CancelledError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            relay.unsubscribe(client)
        return response

    async def _stream_mpegts(self, request: web.Request, relay: Any) -> web.StreamResponse:
        """Serve FFmpeg's source H.264 MPEG-TS output to local consumers."""

        client = relay.subscribe_mpegts()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "video/mp2t",
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        try:
            await response.prepare(request)
            while True:
                try:
                    chunk = await request.app["hass"].async_add_executor_job(client.get, 30.0)
                except queue.Empty:
                    if relay.status in {"stopped", "error"}:
                        break
                    continue
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            relay.unsubscribe_mpegts(client)
        return response
