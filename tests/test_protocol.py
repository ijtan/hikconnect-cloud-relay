"""Offline tests for the small VTM/H.264 protocol boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
import queue
from unittest.mock import Mock, patch
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hikconnect_cloud_relay_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(ROOT / "custom_components" / "hikconnect_cloud_relay")]
sys.modules[PACKAGE_NAME] = package

vtm = importlib.import_module(f"{PACKAGE_NAME}.vtm")
relay = importlib.import_module(f"{PACKAGE_NAME}.relay")
rtsp = importlib.import_module(f"{PACKAGE_NAME}.rtsp")
cloud = importlib.import_module(f"{PACKAGE_NAME}.cloud")


class ProtocolTests(unittest.TestCase):
    def test_rtp_payload_and_timestamp(self) -> None:
        packet = bytes.fromhex("8060000100000e1001020304") + b"payload"
        self.assertEqual(vtm.rtp_payload(packet), b"payload")
        self.assertEqual(vtm.rtp_timestamp(packet), 3600)

    def test_vtm_keepalive_runs_independently_of_packet_reads(self) -> None:
        client = vtm.VtmStreamClient("ysproto://stream.example.test:8554/live")
        client.stream_session = "session"
        stop_event = Mock()
        stop_event.wait.side_effect = [False, True]
        with patch.object(client, "_send") as send:
            client._keepalive_loop(stop_event)
        send.assert_called_once_with(
            vtm._keepalive_request("session"), vtm.MESSAGE, vtm.KEEPALIVE_REQ
        )

    def test_h264_single_and_stap_a(self) -> None:
        depacketizer = relay.H264Depacketizer()
        self.assertEqual(list(depacketizer.feed(b"\x65abc")), [b"\x65abc"])
        stap = b"\x78\x00\x02\x67\x01\x00\x02\x68\x02"
        self.assertEqual(list(depacketizer.feed(stap)), [b"\x67\x01", b"\x68\x02"])

    def test_h264_fu_a(self) -> None:
        depacketizer = relay.H264Depacketizer()
        self.assertEqual(list(depacketizer.feed(b"\x7c\x85first")), [])
        self.assertEqual(list(depacketizer.feed(b"\x7c\x45last")), [b"\x65firstlast"])

    def test_linked_filter_does_not_use_shared_vtm_host(self) -> None:
        inactive = cloud.HikChannel(
            serial="station",
            channel=10,
            name="station",
            signal_status=0,
            related_ipc=False,
            stream_biz_url="biz=1",
            vtm_host="shared.example",
            vtm_port=8554,
            raw={},
        )
        active = cloud.HikChannel(
            serial="station",
            channel=1,
            name="OUTDOOR STATION",
            signal_status=1,
            related_ipc=True,
            stream_biz_url="biz=1",
            vtm_host="shared.example",
            vtm_port=8554,
            raw={},
        )
        self.assertFalse(inactive.linked)
        self.assertTrue(active.linked)

    def test_chunk_buffer_drops_oldest_data_for_slow_clients(self) -> None:
        buffer = relay.ChunkBuffer(max_chunks=2)
        client = buffer.subscribe()
        try:
            buffer.publish(b"one")
            buffer.publish(b"two")
            buffer.publish(b"three")
            self.assertEqual(client.get_nowait(), b"two")
            self.assertEqual(client.get_nowait(), b"three")
            with self.assertRaises(queue.Empty):
                client.get_nowait()
        finally:
            buffer.unsubscribe(client)

    def test_ffmpeg_mpegts_output_repeats_h264_headers(self) -> None:
        cloud_relay = relay.CloudRelay(
            username="user",
            password="password",
            api_host="https://api.example.test",
            serial="station",
            channel=1,
            stream_type=1,
            fps=0,
            jpeg_quality=5,
        )
        process = types.SimpleNamespace(
            stdin=None,
            stdout=None,
            poll=lambda: 0,
        )
        with patch.object(relay.shutil, "which", return_value="ffmpeg"), patch.object(
            relay.subprocess, "Popen", return_value=process
        ) as popen:
            _, output = cloud_relay._start_ffmpeg()
            try:
                command = popen.call_args.args[0]
                self.assertIn("repeat-headers=1", command)
                self.assertIn("expr:gte(t,n_forced*2)", command)
                self.assertIn("50", command)
                self.assertIn("+resend_headers+pat_pmt_at_frames", command)
            finally:
                output.close()

    def test_rtsp_copy_command_does_not_encode(self) -> None:
        command = rtsp.build_rtsp_copy_command(
            "/usr/bin/ffmpeg", "rtsp://127.0.0.1:8554/hikconnect/test"
        )
        self.assertIn("-c:v", command)
        self.assertIn("copy", command)
        self.assertIn("-rtsp_transport", command)
        self.assertIn("tcp", command)
        self.assertIn("-xerror", command)
        self.assertIn("-rw_timeout", command)
        self.assertIn("10000000", command)
        self.assertNotIn("libx264", command)
        self.assertNotIn("-vf", command)
        self.assertNotIn("mpegts", command)

    def test_rtsp_url_validation_rejects_credentials(self) -> None:
        self.assertEqual(
            rtsp.validate_rtsp_publish_url(" rtsp://127.0.0.1:8554/hikconnect/test "),
            "rtsp://127.0.0.1:8554/hikconnect/test",
        )
        with self.assertRaises(ValueError):
            rtsp.validate_rtsp_publish_url("rtsp://user:pass@127.0.0.1:8554/test")

    def test_rtsp_path_is_derived_from_device_identity(self) -> None:
        self.assertEqual(
            rtsp.default_rtsp_publish_url("test-device", 1),
            "rtsp://127.0.0.1:8554/hikconnect/test-device_1",
        )
        self.assertEqual(
            rtsp.rtsp_reader_url("192.168.4.52", "test-device", 1),
            "rtsp://192.168.4.52:8554/hikconnect/test-device_1",
        )
        self.assertTrue(rtsp.uses_managed_local_server("rtsp://127.0.0.1:8554/test"))
        self.assertFalse(rtsp.uses_managed_local_server("rtsp://192.168.4.52:8554/test"))

    def test_rtsp_mode_disables_legacy_transcode(self) -> None:
        cloud_relay = relay.CloudRelay(
            username="user",
            password="password",
            api_host="https://api.example.test",
            serial="station",
            channel=1,
            stream_type=1,
            fps=0,
            jpeg_quality=5,
            output_mode="rtsp",
            rtsp_publish_url="rtsp://127.0.0.1:8554/hikconnect/station_1",
        )
        self.assertFalse(cloud_relay.legacy_outputs_enabled)
        self.assertTrue(cloud_relay.rtsp_enabled)

    def test_rtsp_health_requires_publisher_ready(self) -> None:
        cloud_relay = relay.CloudRelay(
            username="user",
            password="password",
            api_host="https://api.example.test",
            serial="station",
            channel=1,
            stream_type=1,
            fps=0,
            jpeg_quality=5,
            output_mode="rtsp",
            rtsp_publish_url="rtsp://127.0.0.1:8554/hikconnect/station_1",
        )
        cloud_relay._set_state("streaming")
        self.assertFalse(cloud_relay.healthy)
        assert cloud_relay._rtsp is not None
        cloud_relay._rtsp._set_status("streaming")
        self.assertTrue(cloud_relay.healthy)

    def test_copy_gate_waits_for_parameter_sets_and_idr(self) -> None:
        gate = rtsp.H264CopyGate()
        sps, pps, idr, pframe = b"\x67sps", b"\x68pps", b"\x65idr", b"\x41p"
        self.assertEqual(gate.feed([pframe]), [])
        self.assertEqual(gate.feed([sps, pps, pframe]), [])
        self.assertEqual(gate.feed([idr]), [sps, pps, idr])
        self.assertTrue(gate.ready)
        self.assertEqual(gate.feed([pframe]), [pframe])
        gate.reset(clear_parameter_sets=False)
        self.assertEqual(gate.feed([pframe]), [])
        self.assertEqual(gate.feed([idr]), [sps, pps, idr])

    def test_rtsp_queue_overflow_requires_resync(self) -> None:
        publisher = rtsp.RtspCopyPublisher(
            "rtsp://127.0.0.1:8554/hikconnect/test", max_queue_bytes=8
        )
        publisher.submit([b"\x67sps", b"\x68pps", b"\x65idr"])
        stats = publisher.stats()
        self.assertEqual(stats["dropped_chunks"], 1)
        self.assertEqual(stats["queued_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
