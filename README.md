# Hikvision Intercom

A clean Home Assistant custom integration for Hikvision intercoms that are
visible through a Hik-Connect account.

This project is intentionally separate from the reverse-engineering lab. The
first backend uses the Hik-Connect username/password login and the account's
cloud VTM video route. It does not require local access to the two-wire
network, and it does not bundle proprietary SDK files.

## Current status

The initial implementation provides:

- a Home Assistant config flow for Hik-Connect username/password;
- device discovery, followed by linked-channel discovery and selection;
- one stream-capable camera entity for the selected channel;
- continuous cloud H.264 relayed to an HA-hosted MJPEG endpoint;
- selectable VTM profile `1`, `2`, or experimental `3`;
- configurable MJPEG target FPS and JPEG quality;
- `/stream.mjpeg`, `/snapshot.jpg`, `/health`, and `/stats` endpoints for
  dashboards and other local FFmpeg consumers;
- source-picture, RTP, H.264 NAL, and JPEG counters.

The tested station delivers a continuous `640×480` cloud feed. Selector 3
has carried the largest observed H.264 payload, but is not a proof of 720p or
1080p. The camera's advertised higher-resolution profile and audio are kept as
future media-backend work rather than being represented inaccurately here.

## Installation

For development, copy this repository's `custom_components/hikvision_intercom`
directory into the Home Assistant `config/custom_components` directory and
restart Home Assistant. Once the repository is published, it can be added to
HACS as a custom repository and installed as an integration.

Go to **Settings → Devices & services → Add integration → Hikvision
Intercom**. Enter the Hik-Connect account that owns or can see the station,
choose the device, then choose the linked camera channel.

These are Hik-Connect account credentials. They are not the HPP Developer
Account API key/secret pair. HPP OpenAPI metadata and HPNetSDK/P2P media will
be separate backends when their supported interfaces are available.

## Stream options

Open the integration's options after setup:

| Option | Meaning |
|---|---|
| Cloud VTM selector | `1` main candidate, `2` alternate candidate, `3` experimental high-bitrate candidate |
| MJPEG target FPS | `0` preserves the source cadence; a positive value asks FFmpeg to rate-limit output |
| JPEG quality | `2` is best/largest; `31` is smallest |
| Relay hostname/IP | Hostname used in the camera's stream source URL, useful when another container must reach Home Assistant |

The integration registers an endpoint in the Home Assistant HTTP server:

```text
http://HOME_ASSISTANT:8123/api/hikvision_intercom/ENTRY_ID/stream.mjpeg
http://HOME_ASSISTANT:8123/api/hikvision_intercom/ENTRY_ID/snapshot.jpg
http://HOME_ASSISTANT:8123/api/hikvision_intercom/ENTRY_ID/stats
```

The stream endpoint is deliberately unauthenticated so Home Assistant's
FFmpeg-based stream consumer and external processes can open it. Keep
Home Assistant's HTTP port on a trusted network or place it behind suitable
network access controls. The URL contains an opaque config-entry ID but is not
a substitute for authentication.

Home Assistant's camera entity returns the MJPEG URL as its stream source, so
the normal camera dashboard and the `stream` integration can consume it. An
external process such as Frigate can use the same URL as an FFmpeg input, for
example:

```yaml
cameras:
  front_door:
    ffmpeg:
      inputs:
        - path: http://homeassistant:8123/api/hikvision_intercom/ENTRY_ID/stream.mjpeg
          input_args:
            - -f
            - mjpeg
          roles:
            - detect
            - record
```

Use a Home Assistant hostname reachable from the Frigate container and replace
`ENTRY_ID` with the config entry ID. The endpoint is MJPEG video only in this
release; no microphone or talkback path is started implicitly.

## Development boundaries

The code has no device writes, door-unlock commands, audio-call operations,
local-port scanning, SDK binaries, private account data, or captured media.
The media worker uses bounded reconnect backoff and keeps only small per-client
frame queues so a slow consumer cannot create unbounded latency.

The next backend boundary is native HPP HPNetSDK/P2P media. HPP API keys and
secrets are control-plane credentials and should not be entered as the
Hik-Connect account password in this flow.

## License

MIT. See [LICENSE](LICENSE).
