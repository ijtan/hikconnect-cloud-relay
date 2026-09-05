"""Offline tests for the small VTM/H.264 protocol boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
import queue
from unittest.mock import patch
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
cloud = importlib.import_module(f"{PACKAGE_NAME}.cloud")


class ProtocolTests(unittest.TestCase):
    def test_rtp_payload_and_timestamp(self) -> None:
        packet = bytes.fromhex("8060000100000e1001020304") + b"payload"
        self.assertEqual(vtm.rtp_payload(packet), b"payload")
        self.assertEqual(vtm.rtp_timestamp(packet), 3600)

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


if __name__ == "__main__":
    unittest.main()
