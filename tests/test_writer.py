import time
import unittest

from app import protocol
from app.buffer import TagBuffer
import app.reader as reader_mod
from app.reader import Reader

OLD_EPC = "30751FEB705C5904E3D50D70"
DATA_HEX = "E2003412AABBCCDD"
DATA = bytes.fromhex(DATA_HEX)
OTHER_EPC = "E2003412AABBCCDDEEFF0011"

VERSION_RESPONSE = bytes.fromhex("BB 01 03 00 0B 00 4D 31 30 30 20 56 31 2E 30 30 22 7E")
POLL_OK = protocol.build_frame(protocol.TYPE_RESPONSE, protocol.CMD_SINGLE_POLL, b"\x00")


class FakeSerial:
    last = None

    def __init__(self, *args, **kwargs):
        self.rx = bytearray()
        self.writes = []
        self.pc = 0x3000
        self.epc = bytearray.fromhex(OLD_EPC)
        self.user = bytearray(64)
        self.read_epc_override: str | None = None
        self.ignore_writes = 0
        self.write_error: int | None = None
        self.read_error: int | None = None
        self.suppress_reads = False
        self.poll_tag: str | None = None
        FakeSerial.last = self

    def reset_input_buffer(self):
        self.rx.clear()

    def reset_output_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, size=1):
        if self.rx:
            data = bytes(self.rx[:size])
            del self.rx[:size]
            return data
        time.sleep(0.002)
        return b""

    def _response(self, command: int, epc: bytes, tail: bytes) -> bytes:
        return protocol.build_frame(
            protocol.TYPE_RESPONSE,
            command,
            bytes([2 + len(epc)]) + self.pc.to_bytes(2, "big") + epc + tail,
        )

    def write(self, data):
        self.writes.append(data)
        if data == protocol.HARDWARE_VERSION_CMD:
            self.rx.extend(VERSION_RESPONSE)
            return
        if data == protocol.SINGLE_POLL_CMD:
            if self.poll_tag:
                epc = bytes.fromhex(self.poll_tag)
                self.rx.extend(
                    protocol.build_frame(
                        protocol.TYPE_NOTIFICATION,
                        protocol.CMD_SINGLE_POLL,
                        bytes([0xD2])
                        + ((len(epc) // 2) << 11).to_bytes(2, "big")
                        + epc
                        + b"\x00\x00",
                    )
                )
            else:
                self.rx.extend(POLL_OK)
            return
        command = data[2]
        length = (data[3] << 8) | data[4]
        param = data[5 : 5 + length]
        mem_bank = param[4]
        if command == protocol.CMD_SELECT_PARAMS:
            self.rx.extend(
                protocol.build_frame(protocol.TYPE_RESPONSE, command, b"\x00")
            )
        elif command == protocol.CMD_WRITE:
            if self.write_error is not None:
                self.rx.extend(
                    protocol.build_frame(
                        protocol.TYPE_RESPONSE,
                        protocol.CMD_ERROR,
                        bytes([self.write_error]),
                    )
                )
                return
            if self.ignore_writes > 0:
                self.ignore_writes -= 1
            elif mem_bank == protocol.MEM_BANK_USER:
                sa = (param[5] << 8) | param[6]
                dt = param[9:]
                base = sa * 2
                self.user[base : base + len(dt)] = dt
            self.rx.extend(self._response(command, bytes(self.epc), b"\x00"))
        elif command == protocol.CMD_READ:
            if self.suppress_reads:
                return
            if self.read_error is not None:
                self.rx.extend(
                    protocol.build_frame(
                        protocol.TYPE_RESPONSE,
                        protocol.CMD_ERROR,
                        bytes([self.read_error]),
                    )
                )
                return
            sa = (param[5] << 8) | param[6]
            dl = (param[7] << 8) | param[8]
            epc = (
                bytes.fromhex(self.read_epc_override)
                if self.read_epc_override
                else bytes(self.epc)
            )
            base = sa * 2
            self.rx.extend(
                self._response(command, epc, bytes(self.user[base : base + dl * 2]))
            )

    def close(self):
        pass


class WriterJobTest(unittest.TestCase):
    def setUp(self):
        self._saved_timeout = reader_mod.COMMAND_TIMEOUT
        reader_mod.COMMAND_TIMEOUT = 0.2
        self.fake = FakeSerial()
        self.states: list[bool] = []
        self.results: list[dict] = []
        self.reader = Reader(
            "COM1",
            115200,
            TagBuffer(),
            on_write_state=self.states.append,
            on_write_result=self.results.append,
        )
        self.reader.version = "M100 V1.00"
        self.reader._serial = self.fake

    def tearDown(self):
        reader_mod.COMMAND_TIMEOUT = self._saved_timeout

    def commands(self) -> list[int]:
        return [frame[2] for frame in self.fake.writes]

    def test_success_writes_user_memory_once(self):
        self.reader._run_write_job(OLD_EPC, DATA)

        self.assertEqual(self.states, [True, False])
        self.assertEqual(self.commands(), [0x0C, 0x49, 0x39])
        self.assertEqual(
            self.fake.writes[0].hex().upper(),
            "BB000C00130100000020600030751FEB705C5904E3D50D70AD7E",
        )
        self.assertEqual(
            self.fake.writes[1].hex().upper(),
            "BB00490011000000000300000004E2003412AABBCCDD977E",
        )
        self.assertEqual(bytes(self.fake.user[: len(DATA)]), DATA)
        self.assertEqual(bytes(self.fake.epc).hex().upper(), OLD_EPC)
        self.assertEqual(self.fake.pc, 0x3000)
        result = self.results[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["expected"], DATA_HEX)
        self.assertEqual(result["readbacks"], [DATA_HEX])
        self.assertEqual(result["epc"], OLD_EPC)
        self.assertEqual(self.reader.get_user(OLD_EPC), DATA_HEX + "00" * 8)

    def test_write_cache_truncates_to_16_bytes(self):
        data = bytes.fromhex("11" * 20)
        self.reader._run_write_job(OLD_EPC, data)

        self.assertTrue(self.results[-1]["ok"])
        self.assertEqual(self.reader.get_user(OLD_EPC), "11" * 16)

    def test_readback_mismatch_rewrites_once(self):
        self.fake.ignore_writes = 1
        self.reader._run_write_job(OLD_EPC, DATA)

        self.assertEqual(self.commands(), [0x0C, 0x49, 0x39, 0x49, 0x39])
        result = self.results[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["readbacks"], ["00" * len(DATA), DATA_HEX])
        self.assertEqual(bytes(self.fake.user[: len(DATA)]), DATA)

    def test_readback_mismatch_twice_fails(self):
        self.fake.ignore_writes = 2
        self.reader._run_write_job(OLD_EPC, DATA)

        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertIn("仍不一致", result["message"])

    def test_write_error_is_not_retried(self):
        self.fake.write_error = 0x10
        self.reader._run_write_job(OLD_EPC, DATA)

        self.assertEqual(self.commands(), [0x0C, 0x49])
        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertIn("写标签失败", result["message"])
        self.assertIn("0x10", result["message"])

    def test_read_timeout_fails_without_rewrite(self):
        self.fake.suppress_reads = True
        self.reader._run_write_job(OLD_EPC, DATA)

        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["readbacks"], [])
        self.assertIn("无法确认", result["message"])
        self.assertEqual(self.commands(), [0x0C, 0x49, 0x39])

    def test_readback_epc_mismatch_fails_without_rewrite(self):
        self.fake.read_epc_override = OTHER_EPC
        self.reader._run_write_job(OLD_EPC, DATA)

        self.assertEqual(self.commands(), [0x0C, 0x49, 0x39])
        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertIn("EPC 与所选不一致", result["message"])

    def test_second_submit_rejected_while_job_pending(self):
        self.assertTrue(self.reader.submit_epc_write(OLD_EPC, DATA))
        self.assertFalse(self.reader.submit_epc_write(OLD_EPC, DATA))


class WriterLoopTest(unittest.TestCase):
    def setUp(self):
        self._saved_timeout = reader_mod.COMMAND_TIMEOUT
        self._saved_serial = reader_mod.serial.Serial
        reader_mod.COMMAND_TIMEOUT = 0.2
        reader_mod.serial.Serial = FakeSerial

    def tearDown(self):
        reader_mod.COMMAND_TIMEOUT = self._saved_timeout
        reader_mod.serial.Serial = self._saved_serial

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_first_sighting_reads_user_into_cache(self):
        user_hex = "E2003412AABBCCDDEEFF001122334455"
        reader = Reader("COM1", 115200, TagBuffer())
        reader.start()
        self.assertTrue(self.wait_for(lambda: bool(reader.version)))
        fake = FakeSerial.last
        fake.user[:16] = bytes.fromhex(user_hex)
        fake.poll_tag = OLD_EPC

        self.assertTrue(self.wait_for(lambda: reader.get_user(OLD_EPC) == user_hex))
        commands = [frame[2] for frame in fake.writes]
        self.assertIn(protocol.CMD_SELECT_PARAMS, commands)
        self.assertIn(protocol.CMD_READ, commands)
        read_count = commands.count(protocol.CMD_READ)
        time.sleep(0.3)
        commands = [frame[2] for frame in fake.writes]
        self.assertEqual(commands.count(protocol.CMD_READ), read_count)

        reader.stop()
        reader.join(timeout=3)

    def test_user_read_error_caches_zeros_once(self):
        reader = Reader("COM1", 115200, TagBuffer())
        reader.start()
        self.assertTrue(self.wait_for(lambda: bool(reader.version)))
        fake = FakeSerial.last
        fake.poll_tag = OLD_EPC
        fake.read_error = 0xB3

        self.assertTrue(
            self.wait_for(
                lambda: any(frame[2] == protocol.CMD_READ for frame in fake.writes)
            )
        )
        time.sleep(0.3)
        read_count = sum(1 for frame in fake.writes if frame[2] == protocol.CMD_READ)
        self.assertEqual(read_count, 1)
        self.assertEqual(reader.get_user(OLD_EPC), "00" * 16)

        reader.stop()
        reader.join(timeout=3)

    def test_polling_pauses_during_write_and_resumes(self):
        results: list[dict] = []
        reader = Reader("COM1", 115200, TagBuffer(), on_write_result=results.append)
        reader.start()
        self.assertTrue(self.wait_for(lambda: bool(reader.version)))
        self.assertTrue(reader.submit_epc_write(OLD_EPC, DATA))
        self.assertTrue(self.wait_for(lambda: bool(results)))

        fake = FakeSerial.last
        commands = [frame[2] for frame in fake.writes]
        first_select = commands.index(protocol.CMD_SELECT_PARAMS)
        last_read = len(commands) - 1 - commands[::-1].index(protocol.CMD_READ)
        self.assertNotIn(
            protocol.CMD_SINGLE_POLL, commands[first_select : last_read + 1]
        )
        self.assertTrue(results[-1]["ok"])
        self.assertEqual(bytes(fake.epc).hex().upper(), OLD_EPC)

        polls_before = len(commands)
        self.assertTrue(
            self.wait_for(
                lambda: any(
                    frame[2] == protocol.CMD_SINGLE_POLL
                    for frame in fake.writes[polls_before:]
                )
            )
        )

        reader.stop()
        reader.join(timeout=3)
        self.assertFalse(reader.is_alive())


if __name__ == "__main__":
    unittest.main()
