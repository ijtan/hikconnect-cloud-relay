# Hik-Connect Cloud Relay

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ijtan&repository=hikconnect-cloud-relay&category=integration)

A Home Assistant custom integration that turns live video from a Hik-Connect
intercom into a Home Assistant camera and a stream that other local services
can consume.

It is intended for installations where the Hik-Connect app can show live video
but local RTSP, ISAPI, or SDK access is unavailable or impractical. The
integration signs in with the Hik-Connect account, discovers the available
devices and linked channels, opens the cloud video route, and relays the video
to Home Assistant.

This integration has been tested on the Hikvision **DS-KIS703Y-P** kit:
the **DS-KV8103Y-IMPE2** door station and **DS-KH6320Y-WTPE2** indoor
station. It works with the linked Hik-Connect channel on that installation;
other Hikvision intercom kits may work too, but compatibility is not assumed.

## What it offers

- A setup wizard for Hik-Connect username and password.
- Device discovery followed by linked-channel selection.
- A normal Home Assistant camera entity.
- A continuous cloud video relay without port forwarding or local device admin
  access.
- A Home Assistant-hosted MJPEG stream for dashboards and browsers.
- An FFmpeg-generated MPEG-TS/H.264 stream for Frigate and other consumers.
- Configurable cloud stream selector, output FPS, JPEG quality, and relay host.
- Snapshot, health, and statistics endpoints for troubleshooting.
- Automatic reconnect with bounded backoff when the cloud session ends.

This is currently a video integration. Microphone audio, two-way talk, door
controls, and local-device protocols are not included in this release.

## Why it exists

Some Hikvision intercoms expose a linked camera channel through Hik-Connect,
but do not provide a usable local RTSP, ISAPI, or SDK stream. That means the
camera can work in the Hik-Connect app while producing no video in Home
Assistant.

This integration focuses on that gap: it signs in with the Hik-Connect account,
finds the linked channel, and keeps the cloud video relay running as a normal
Home Assistant camera and local stream.

## Installation

### HACS (Recommended)

1. Select the **Open in HACS** button at the top of this page.
2. If it is not listed yet, add this repository manually in HACS as a custom
   **Integration** repository.
3. Install **Hik-Connect Cloud Relay**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and choose
   **Hik-Connect Cloud Relay**.

The repository must be publicly reachable by Home Assistant/HACS for the
button and custom-repository installation to work.

<details>
<summary>Manual installation</summary>

1. Copy the `custom_components/hikconnect_cloud_relay` directory into the
   `config/custom_components` directory of Home Assistant.
2. Restart Home Assistant.

</details>

### Upgrading from an earlier test build

Version 0.1.1 uses the unique Home Assistant domain `hikconnect_cloud_relay`
instead of the earlier `hikvision_intercom` domain. If you installed an
earlier test build, remove its `config/custom_components/hikvision_intercom`
directory and old config entry before installing this version; the domain
change is not migrated automatically.

During setup, enter the Hik-Connect account that can see the intercom. These
are the same account credentials used by the Hik-Connect app. A local device
administrator password is not required.

## Stream output

After setup, open the integration's options to choose:

| Option | Meaning |
| --- | --- |
| Cloud stream selector | `1` main candidate, `2` alternate candidate, `3` experimental candidate |
| MJPEG target FPS | `0` keeps the source cadence; a positive value limits output |
| JPEG quality | `2` is best/largest; `31` is smallest |
| Relay host | Hostname or IP used by external consumers to reach Home Assistant |

The camera entity returns its stream source to Home Assistant. The relay also
provides these local endpoints:

```text
http://HOME_ASSISTANT:8123/api/hikconnect_cloud_relay/ENTRY_ID/stream.mjpeg
http://HOME_ASSISTANT:8123/api/hikconnect_cloud_relay/ENTRY_ID/stream.ts
http://HOME_ASSISTANT:8123/api/hikconnect_cloud_relay/ENTRY_ID/snapshot.jpg
http://HOME_ASSISTANT:8123/api/hikconnect_cloud_relay/ENTRY_ID/health
http://HOME_ASSISTANT:8123/api/hikconnect_cloud_relay/ENTRY_ID/stats
```

## Frigate setup

<details>
<summary>Use the relay in Frigate</summary>

A Frigate container can consume the MPEG-TS/H.264 endpoint with FFmpeg:

```yaml
cameras:
  front_door:
    ffmpeg:
      inputs:
        - path: http://homeassistant:8123/api/hikconnect_cloud_relay/ENTRY_ID/stream.ts
          input_args:
            - -f
            - mpegts
          roles:
            - detect
            - record
```

Use a hostname reachable from the consumer container and replace `ENTRY_ID`
with the Home Assistant config-entry ID. The MPEG-TS endpoint is FFmpeg-
transcoded H.264 and currently contains no audio track. H.264 parameter sets
are repeated at keyframes so consumers that join an already-running relay can
begin decoding without waiting for the relay or Home Assistant to restart.
The MJPEG endpoint remains available if a consumer needs it instead.

</details>

## Current limitations

- There is no native go2rtc integration or WebRTC/RTSP output yet. The
  integration currently exposes HTTP MJPEG and MPEG-TS endpoints instead.
- This release is video-only. It does not expose a passive audio listener or
  implement Hik-Connect two-way audio/call control.
- The tested intercom delivered a continuous `640×480` cloud feed. The camera
  advertises higher-resolution profiles, but this relay does not claim 720p or
  1080p until the cloud path has been verified at that resolution.
- Video depends on the Hik-Connect cloud service and internet access.
- The media endpoint is intentionally unauthenticated so HA's stream consumer
  and services such as Frigate can read it. Keep the Home Assistant HTTP port
  on a trusted network and do not expose this endpoint directly to the
  internet.
- Hik-Connect is an unofficial, undocumented account/API surface and may
  change without notice.
- The relay host needs an FFmpeg executable. Home Assistant OS and the Home
  Assistant Container include FFmpeg; other installations need to provide it.

## Security and privacy

Home Assistant stores the Hik-Connect credentials in the integration's config
entry. The relay's health and statistics endpoints expose stream state and
counters only. Video is relayed through Hik-Connect's cloud service before it
reaches Home Assistant. Anyone who can reach the unauthenticated media URL can
view the selected stream, so network isolation or a suitable reverse proxy is
required for untrusted networks.

## Project status

This is an unofficial community integration. It is not affiliated with or
endorsed by Hikvision or Hik-Connect. Issues and hardware reports are welcome,
especially reports that include the device family, selected stream profile,
and redacted relay statistics.

## License

MIT. See [LICENSE](LICENSE).
