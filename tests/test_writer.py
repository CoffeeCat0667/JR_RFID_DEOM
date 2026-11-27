import time
import unittest

from app import protocol
from app.buffer import TagBuffer
import app.reader as reader_mod
from app.reader import Reader

OLD_EPC = "30751FEB705C5904E3D50D70"
NEW_EPC = "E2003412AABBCCDD"
NEW_EPC_LONG = "E2003412AABBCCDDEEFF0011"
OTHER_EPC = "112233445566778899AABBCC"
TID_HEX = "E280116060000205AABBCCDD"
TID = bytes.fromhex(TID_HEX)

VERSION_RESPONSE = bytes.fromhex("BB 01 03 00 0B 00 4D 31 30 30 20 56 31 2E 30 30 22 7E")
POLL_OK = protocol.build_frame(protocol.TYPE_RESPONSE, protocol.CMD_SINGLE_POLL, b"\x00")


class FakeSerial:
    last = None

    def __init__(self, *args, **kwargs):
        self.rx = bytearray()
        self.writes = []
        self.epc = bytearray.fromhex(OLD_EPC)
        self.pc = (len(self.epc) // 2) << 11
        self.tid = bytearray(TID)
        self.selected: str | None = None
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

    def _error(self, code: int) -> bytes:
        return protocol.build_frame(protocol.TYPE_RESPONSE, protocol.CMD_ERROR, bytes([code]))

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

        if command == protocol.CMD_SELECT_PARAMS:
            mask_len = param[5] // 8
            self.selected = param[7 : 7 + mask_len].hex().upper()
            self.rx.extend(
                protocol.build_frame(protocol.TYPE_RESPONSE, command, b"\x00")
            )
            return

        if command == protocol.CMD_WRITE:
            if self.write_error is not None:
                self.rx.extend(self._error(self.write_error))
                return
            if self.selected != bytes(self.epc).hex().upper():
                self.rx.extend(self._error(0x10))
                return
            if self.ignore_writes > 0:
                self.ignore_writes -= 1
            else:
                mem_bank = param[4]
                sa = (param[5] << 8) | param[6]
                dt = param[9:]
                if mem_bank == protocol.MEM_BANK_EPC and sa == protocol.EPC_SA_PC:
                    self.pc = int.from_bytes(dt[:2], "big")
                    self.epc = bytearray(dt[2:])
            self.rx.extend(self._response(command, bytes(self.epc), b"\x00"))
            return

        if command == protocol.CMD_READ:
            if self.suppress_reads:
                return
            if self.read_error is not None:
                self.rx.extend(self._error(self.read_error))
                return
            if self.selected != bytes(self.epc).hex().upper():
                self.rx.extend(self._error(0x09))
                return
            mem_bank = param[4]
            sa = (param[5] << 8) | param[6]
            dl = (param[7] << 8) | param[8]
            if mem_bank == protocol.MEM_BANK_TID:
                if dl * 2 > len(self.tid):
                    self.rx.extend(self._error(0xA3))
                    return
                tail = bytes(self.tid[: dl * 2])
            elif mem_bank == protocol.MEM_BANK_EPC and sa >= protocol.EPC_SA_DATA:
                base = (sa - protocol.EPC_SA_DATA) * 2
                tail = bytes(self.epc[base : base + dl * 2])
            else:
                tail = b"\x00" * (dl * 2)
            epc = (
                bytes.fromhex(self.read_epc_override)
                if self.read_epc_override
                else bytes(self.epc)
            )
            self.rx.extend(self._response(command, epc, tail))

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

    def test_success_writes_epc_once(self):
        new_epc = bytes.fromhex(NEW_EPC)
        self.reader._run_write_job(OLD_EPC, new_epc)

        self.assertEqual(self.states, [True, False])
        self.assertEqual(self.commands(), [0x0C, 0x49, 0x0C, 0x39])
        self.assertEqual(
            self.fake.writes[0].hex().upper(),
            "BB000C00130100000020600030751FEB705C5904E3D50D70AD7E",
        )
        self.assertEqual(self.fake.writes[1], protocol.build_write_epc(new_epc))
        self.assertEqual(
            self.fake.writes[2], protocol.build_select_params(bytes.fromhex(NEW_EPC))
        )
        self.assertEqual(self.fake.writes[3], protocol.build_read_epc(4))
        self.assertEqual(bytes(self.fake.epc).hex().upper(), NEW_EPC)
        self.assertEqual(self.fake.pc, 0x2000)
        result = self.results[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["expected"], NEW_EPC)
        self.assertEqual(result["readbacks"], [NEW_EPC])
        self.assertEqual(result["epc"], OLD_EPC)
        self.assertEqual(result["new_epc"], NEW_EPC)
        self.assertEqual(result["read_epc"], NEW_EPC)

    def test_pc_recomputed_for_12_byte_epc(self):
        self.reader._run_write_job(OLD_EPC, bytes.fromhex(NEW_EPC_LONG))

        self.assertTrue(self.results[-1]["ok"])
        self.assertEqual(bytes(self.fake.epc).hex().upper(), NEW_EPC_LONG)
        self.assertEqual(self.fake.pc, 0x3000)

    def test_write_error_is_not_retried(self):
        self.fake.write_error = 0x10
        self.reader._run_write_job(OLD_EPC, bytes.fromhex(NEW_EPC))

        self.assertEqual(self.commands(), [0x0C, 0x49])
        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["readbacks"], [])
        self.assertIn("写标签失败", result["message"])
        self.assertIn("0x10", result["message"])

    def test_read_timeout_fails_without_retry(self):
        self.fake.suppress_reads = True
        self.reader._run_write_job(OLD_EPC, bytes.fromhex(NEW_EPC))

        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["readbacks"], [])
        self.assertIn("无法确认", result["message"])
        self.assertEqual(self.commands(), [0x0C, 0x49, 0x0C, 0x39])

    def test_write_success_but_epc_not_applied_fails(self):
        self.fake.ignore_writes = 1
        self.reader._run_write_job(OLD_EPC, bytes.fromhex(NEW_EPC))

        self.assertEqual(self.commands(), [0x0C, 0x49, 0x0C, 0x39])
        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["readbacks"], [])
        self.assertIn("无法确认", result["message"])
        self.assertEqual(bytes(self.fake.epc).hex().upper(), OLD_EPC)

    def test_readback_epc_mismatch_fails(self):
        self.fake.read_epc_override = OTHER_EPC
        self.reader._run_write_job(OLD_EPC, bytes.fromhex(NEW_EPC))

        result = self.results[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["read_epc"], OTHER_EPC)
        self.assertIn("回读 EPC 与写入值不一致", result["message"])

    def test_second_submit_rejected_while_job_pending(self):
        self.assertTrue(self.reader.submit_epc_write(OLD_EPC, bytes.fromhex(NEW_EPC)))
        self.assertFalse(self.reader.submit_epc_write(OLD_EPC, bytes.fromhex(NEW_EPC)))


class WriterLoopTest(unittest.TestCase):
    def setUp(self):
        self._saved_timeout = reader_mod.COMMAND_TIMEOUT
        self._saved_tid_timeout = reader_mod.TID_COMMAND_TIMEOUT
        self._saved_tid_retry = reader_mod.TID_RETRY_INTERVAL
        self._saved_serial = reader_mod.serial.Serial
        reader_mod.COMMAND_TIMEOUT = 0.2
        reader_mod.TID_COMMAND_TIMEOUT = 0.1
        reader_mod.TID_RETRY_INTERVAL = 0.3
        reader_mod.serial.Serial = FakeSerial

    def tearDown(self):
        reader_mod.COMMAND_TIMEOUT = self._saved_timeout
        reader_mod.TID_COMMAND_TIMEOUT = self._saved_tid_timeout
        reader_mod.TID_RETRY_INTERVAL = self._saved_tid_retry
        reader_mod.serial.Serial = self._saved_serial

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _read_banks(self, fake: FakeSerial) -> list[int]:
        banks = []
        for frame in fake.writes:
            if frame[2] != protocol.CMD_READ:
                continue
            length = (frame[3] << 8) | frame[4]
            banks.append(frame[5 : 5 + length][4])
        return banks

    def test_first_sighting_reads_tid_once(self):
        reader = Reader("COM1", 115200, TagBuffer())
        reader.start()
        self.assertTrue(self.wait_for(lambda: bool(reader.version)))
        fake = FakeSerial.last
        fake.poll_tag = OLD_EPC

        self.assertTrue(self.wait_for(lambda: reader.get_tid(OLD_EPC) == TID_HEX))
        banks = self._read_banks(fake)
        self.assertTrue(banks)
        self.assertTrue(all(bank == protocol.MEM_BANK_TID for bank in banks))
        self.assertEqual(len(banks), 2)  # 8 word 返回 0xA3，6 word 成功
        self.assertNotIn(0x03, banks)

        time.sleep(0.3)
        self.assertEqual(len(self._read_banks(fake)), 2)

        reader.stop()
        reader.join(timeout=3)

    def test_tid_read_failure_retries_until_success(self):
        events: list[tuple[str, str]] = []
        reader = Reader(
            "COM1",
            115200,
            TagBuffer(),
            on_tid=lambda epc, tid: events.append((epc, tid)),
        )
        reader.start()
        self.assertTrue(self.wait_for(lambda: bool(reader.version)))
        fake = FakeSerial.last
        fake.tid = bytearray()
        fake.poll_tag = OLD_EPC

        # 第一次失败：8/6/4/2 word 全部越界
        self.assertTrue(
            self.wait_for(
                lambda: len(self._read_banks(fake)) == len(protocol.TID_READ_WORDS)
            )
        )
        self.assertEqual(reader.get_tid(OLD_EPC), "")

        # 3 秒（测试中缩短为 0.3 秒）后自动重试
        self.assertTrue(
            self.wait_for(
                lambda: len(self._read_banks(fake)) == 2 * len(protocol.TID_READ_WORDS)
            )
        )
        self.assertEqual(reader.get_tid(OLD_EPC), "")

        # 标签拿近后，下一次重试成功
        fake.tid = bytearray(TID)
        self.assertTrue(self.wait_for(lambda: reader.get_tid(OLD_EPC) == TID_HEX))
        self.assertEqual(events, [(OLD_EPC, TID_HEX)])

        # 成功之后不再重复读
        reads = len(self._read_banks(fake))
        time.sleep(0.5)
        self.assertEqual(len(self._read_banks(fake)), reads)

        reader.stop()
        reader.join(timeout=3)

    def test_polling_pauses_during_write_and_resumes(self):
        results: list[dict] = []
        reader = Reader("COM1", 115200, TagBuffer(), on_write_result=results.append)
        reader.start()
        self.assertTrue(self.wait_for(lambda: bool(reader.version)))
        self.assertTrue(reader.submit_epc_write(OLD_EPC, bytes.fromhex(NEW_EPC)))
        self.assertTrue(self.wait_for(lambda: bool(results)))

        fake = FakeSerial.last
        commands = [frame[2] for frame in fake.writes]
        first_select = commands.index(protocol.CMD_SELECT_PARAMS)
        last_read = len(commands) - 1 - commands[::-1].index(protocol.CMD_READ)
        self.assertNotIn(
            protocol.CMD_SINGLE_POLL, commands[first_select : last_read + 1]
        )
        self.assertTrue(results[-1]["ok"])
        self.assertEqual(bytes(fake.epc).hex().upper(), NEW_EPC)

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
