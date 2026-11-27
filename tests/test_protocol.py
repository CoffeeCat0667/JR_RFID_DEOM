import threading
import time
import unittest

from app import protocol
from app.buffer import TagBuffer


VERSION_RESPONSE = bytes.fromhex("BB 01 03 00 0B 00 4D 31 30 30 20 56 31 2E 30 30 22 7E")
TAG_NOTIFICATION = bytes.fromhex(
    "BB 02 22 00 11 C9 34 00 30 75 1F EB 70 5C 59 04 E3 D5 0D 70 3A 76 EF 7E"
)
POLL_ERROR = bytes.fromhex("BB 01 FF 00 01 15 16 7E")


class ProtocolTest(unittest.TestCase):
    def test_command_frames_match_manual(self):
        self.assertEqual(protocol.HARDWARE_VERSION_CMD.hex().upper(), "BB0003000100047E")
        self.assertEqual(protocol.SINGLE_POLL_CMD.hex().upper(), "BB00220000227E")

    def test_parse_hardware_version(self):
        parser = protocol.FrameParser()
        frames = parser.feed(VERSION_RESPONSE)
        self.assertEqual(len(frames), 1)
        self.assertEqual(protocol.parse_hardware_version(frames[0]), "M100 V1.00")

    def test_parse_tag_notification(self):
        parser = protocol.FrameParser()
        tag = protocol.parse_tag(parser.feed(TAG_NOTIFICATION)[0])
        self.assertIsNotNone(tag)
        self.assertEqual(tag.epc, "30751FEB705C5904E3D50D70")
        self.assertEqual(tag.rssi, -55)
        self.assertFalse(tag.pc_mismatch)

    def test_parse_poll_error_without_epc(self):
        parser = protocol.FrameParser()
        error = protocol.parse_error(parser.feed(POLL_ERROR)[0])
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 0x15)
        self.assertIsNone(error.epc)

    def test_parse_poll_error_with_epc(self):
        param = b"\x15" + bytes.fromhex("3400 30751FEB705C5904E3D50D70")
        frame = protocol.build_frame(protocol.TYPE_RESPONSE, protocol.CMD_ERROR, param)
        error = protocol.parse_error(protocol.FrameParser().feed(frame)[0])
        self.assertEqual(error.code, 0x15)
        self.assertEqual(error.epc, "30751FEB705C5904E3D50D70")
        self.assertFalse(error.pc_mismatch)

    def test_split_frames(self):
        parser = protocol.FrameParser()
        self.assertEqual(parser.feed(TAG_NOTIFICATION[:6]), [])
        frames = parser.feed(TAG_NOTIFICATION[6:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, TAG_NOTIFICATION)

    def test_merged_frames_with_garbage_prefix(self):
        parser = protocol.FrameParser()
        frames = parser.feed(b"\x00\x11" + TAG_NOTIFICATION + VERSION_RESPONSE)
        self.assertEqual(len(frames), 2)
        self.assertEqual(protocol.parse_tag(frames[0]).epc, "30751FEB705C5904E3D50D70")
        self.assertEqual(protocol.parse_hardware_version(frames[1]), "M100 V1.00")

    def test_resync_after_bad_checksum(self):
        broken = bytearray(TAG_NOTIFICATION)
        broken[-2] ^= 0xFF
        parser = protocol.FrameParser()
        self.assertEqual(parser.feed(bytes(broken)), [])
        frames = parser.feed(TAG_NOTIFICATION)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, TAG_NOTIFICATION)

    def test_payload_containing_frame_markers(self):
        epc = bytes.fromhex("017EBB7EBB0102")
        param = b"\xC9" + bytes.fromhex("3400") + epc + b"\x3A\x76"
        frame = protocol.build_frame(protocol.TYPE_NOTIFICATION, protocol.CMD_SINGLE_POLL, param)
        parser = protocol.FrameParser()
        frames = parser.feed(frame + TAG_NOTIFICATION)
        self.assertEqual(len(frames), 2)
        self.assertEqual(protocol.parse_tag(frames[0]).epc, epc.hex().upper())
        self.assertEqual(protocol.parse_tag(frames[1]).epc, "30751FEB705C5904E3D50D70")

    def test_resync_after_oversized_length(self):
        parser = protocol.FrameParser()
        frames = parser.feed(bytes([0xBB, 0x01, 0x03, 0x0F, 0xFF]) + TAG_NOTIFICATION)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, TAG_NOTIFICATION)


OLD_EPC = "30751FEB705C5904E3D50D70"
NEW_EPC = "E2003412AABBCCDDEEFF0011"


def make_frame(frame_type: int, command: int, param: bytes = b"") -> protocol.Frame:
    return protocol.FrameParser().feed(protocol.build_frame(frame_type, command, param))[0]


class WriteProtocolTest(unittest.TestCase):
    def test_build_select_params(self):
        frame = protocol.build_select_params(bytes.fromhex(OLD_EPC))
        self.assertEqual(
            frame.hex().upper(),
            "BB000C00130100000020600030751FEB705C5904E3D50D70AD7E",
        )

    def test_build_write_epc(self):
        frame = protocol.build_write_epc(bytes.fromhex(NEW_EPC))
        self.assertEqual(
            frame.hex().upper(),
            "BB004900170000000001000100073000E2003412AABBCCDDEEFF0011CD7E",
        )

    def test_build_read_epc(self):
        frame = protocol.build_read_epc(6)
        self.assertEqual(frame.hex().upper(), "BB003900090000000001000200064B7E")

    def test_build_read_tid(self):
        frame = protocol.build_read_tid(6)
        self.assertEqual(frame.hex().upper(), "BB003900090000000002000000064A7E")

    def test_parse_select_success(self):
        frame = make_frame(protocol.TYPE_RESPONSE, protocol.CMD_SELECT_PARAMS, b"\x00")
        self.assertTrue(protocol.parse_select_success(frame))
        failed = make_frame(
            protocol.TYPE_RESPONSE, protocol.CMD_SELECT_PARAMS, b"\x01"
        )
        self.assertFalse(protocol.parse_select_success(failed))

    def test_parse_write_success(self):
        epc = bytes.fromhex(NEW_EPC)
        param = bytes([2 + len(epc), 0x34, 0x00]) + epc + b"\x00"
        frame = make_frame(protocol.TYPE_RESPONSE, protocol.CMD_WRITE, param)
        self.assertTrue(protocol.parse_write_success(frame))
        param_failed = param[:-1] + b"\x01"
        frame_failed = make_frame(
            protocol.TYPE_RESPONSE, protocol.CMD_WRITE, param_failed
        )
        self.assertFalse(protocol.parse_write_success(frame_failed))

    def test_parse_read_data(self):
        epc = bytes.fromhex(NEW_EPC)
        param = bytes([2 + len(epc), 0x34, 0x00]) + epc + epc
        frame = make_frame(protocol.TYPE_RESPONSE, protocol.CMD_READ, param)
        read_epc, data = protocol.parse_read_data(frame)
        self.assertEqual(read_epc, NEW_EPC)
        self.assertEqual(data, epc)

    def test_parse_hex_text(self):
        self.assertEqual(
            protocol.parse_hex_text("E2 00 34 12"), bytes.fromhex("E2003412")
        )
        for text in ("", "E2 0", "E2 0G", "E2 00 3"):
            with self.assertRaises(ValueError):
                protocol.parse_hex_text(text)

    def test_prepare_epc_write(self):
        self.assertEqual(
            protocol.prepare_epc_write("E2 00 34 12 AA BB CC DD EE FF 00 11"),
            bytes.fromhex(NEW_EPC),
        )
        self.assertEqual(
            protocol.prepare_epc_write("E2 00 34 12"), bytes.fromhex("E2003412")
        )
        self.assertEqual(len(protocol.prepare_epc_write("00 " * 30)), 30)
        with self.assertRaises(ValueError):
            protocol.prepare_epc_write("E2")
        with self.assertRaises(ValueError):
            protocol.prepare_epc_write("E2 00 34")
        with self.assertRaises(ValueError):
            protocol.prepare_epc_write("00 " * 32)
        with self.assertRaises(ValueError):
            protocol.prepare_epc_write("00 " * 66)

    def test_write_error_names(self):
        for code in (0x10, 0x16, 0xA3, 0xB3, 0xB4):
            self.assertNotIn("未知错误码", protocol.error_name(code))


class TagBufferTest(unittest.TestCase):
    def test_batch_dedup_keeps_latest_rssi(self):
        buffer = TagBuffer()
        buffer.add("AA", -50)
        buffer.add("AA", -40)
        buffer.add("BB", None)
        self.assertEqual(buffer.size(), 2)
        batch = buffer.take_batch()
        self.assertEqual(batch, [("AA", -40), ("BB", None)])
        self.assertEqual(buffer.size(), 0)

    def test_requeue_merges_with_new_data(self):
        buffer = TagBuffer()
        buffer.add("AA", -50)
        batch = buffer.take_batch()
        buffer.add("BB", -60)
        buffer.requeue(batch)
        self.assertEqual(buffer.take_batch(), [("AA", -50), ("BB", -60)])

    def test_capacity_drops_oldest(self):
        buffer = TagBuffer(capacity=2)
        buffer.add("AA", -50)
        buffer.add("BB", -60)
        buffer.add("CC", -70)
        self.assertEqual(buffer.size(), 2)
        self.assertEqual(buffer.dropped, 1)
        self.assertEqual(buffer.take_batch(), [("BB", -60), ("CC", -70)])

    def test_stopped_empty_buffer_returns_none(self):
        buffer = TagBuffer()
        buffer.stop()
        self.assertIsNone(buffer.take_batch())

    def test_take_batch_waits_for_quiet_window(self):
        buffer = TagBuffer()
        buffer.add("AA", -50)
        start = time.monotonic()
        timer = threading.Timer(0.1, buffer.add, args=("BB", -60))
        timer.start()
        batch = buffer.take_batch(0.2)
        elapsed = time.monotonic() - start
        timer.join()
        self.assertEqual(batch, [("AA", -50), ("BB", -60)])
        self.assertGreaterEqual(elapsed, 0.25)

    def test_repeated_epc_does_not_extend_quiet_window(self):
        buffer = TagBuffer()
        buffer.add("AA", -50)
        start = time.monotonic()
        timer = threading.Timer(0.1, buffer.add, args=("AA", -40))
        timer.start()
        batch = buffer.take_batch(0.2)
        elapsed = time.monotonic() - start
        timer.join()
        self.assertEqual(len(batch), 1)
        self.assertLess(elapsed, 0.35)


if __name__ == "__main__":
    unittest.main()
