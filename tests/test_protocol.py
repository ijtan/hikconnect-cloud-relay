"""Offline tests for the small VTM/H.264 protocol boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hikvision_intercom_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(ROOT / "custom_components" / "hikvision_intercom")]
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


if __name__ == "__main__":
    unittest.main()
