"""串口读卡线程：握手获取硬件版本，然后按 100ms 周期执行单次轮询。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

import serial

from app import protocol
from app.buffer import TagBuffer

POLL_INTERVAL = 0.1
HANDSHAKE_TIMEOUT = 1.5
HANDSHAKE_RETRIES = 3
POLL_TIMEOUT = 0.25
SERIAL_TIMEOUT = 0.02
COMMAND_TIMEOUT = 1.0

logger = logging.getLogger("rfid")


class Reader(threading.Thread):
    def __init__(
        self,
        port: str,
        baudrate: int,
        buffer: TagBuffer,
        on_tag: Callable[[str, int | None], None] | None = None,
        on_tid: Callable[[str, str], None] | None = None,
        on_version: Callable[[str], None] | None = None,
        on_handshake_failed: Callable[[str], None] | None = None,
        on_runtime_error: Callable[[str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_write_state: Callable[[bool], None] | None = None,
        on_write_result: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(name="serial-reader", daemon=True)
        self.port = port
        self.baudrate = baudrate
        self._buffer = buffer
        self.on_tag = on_tag
        self.on_tid = on_tid
        self.on_version = on_version
        self.on_handshake_failed = on_handshake_failed
        self.on_runtime_error = on_runtime_error
        self.on_log = on_log
        self.on_write_state = on_write_state
        self.on_write_result = on_write_result

        self.version = ""
        self.polls = 0
        self.empty_polls = 0
        self.tags = 0
        self.poll_errors = 0
        self.timeouts = 0
        self.writing = False

        self._stop_event = threading.Event()
        self._parser = protocol.FrameParser()
        self._serial: serial.Serial | None = None
        self._last_log: dict[str, float] = {}
        self._write_job: tuple[str, bytes] | None = None
        self._write_lock = threading.Lock()
        self._tid_cache: dict[str, str] = {}
        self._tid_pending: deque[str] = deque()
        self._tid_queued: set[str] = set()
        self._tid_lock = threading.Lock()

    def stop(self) -> None:
        self._stop_event.set()

    def submit_epc_write(self, epc: str, data: bytes) -> bool:
        if self._stop_event.is_set() or not self.version:
            return False
        with self._write_lock:
            if self._write_job is not None or self.writing:
                return False
            self._write_job = (epc, data)
        return True

    def get_tid(self, epc: str) -> str:
        """已读取的 TID（大写 HEX，实际长度）；未读到或读取失败时为空字符串。"""
        with self._tid_lock:
            return self._tid_cache.get(epc, "")

    def run(self) -> None:
        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=SERIAL_TIMEOUT,
                write_timeout=1,
            )
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception as exc:
            self._handshake_failed(f"打开串口 {self.port} 失败：{exc}")
            return

        try:
            version, error = self._handshake()
            if self._stop_event.is_set():
                return
            if version is None:
                self._handshake_failed(error or "读取硬件版本超时")
                return
            self.version = version
            self._emit(self.on_version, version)
            self._log(f"硬件版本：{version}，开始轮询（间隔 {int(POLL_INTERVAL * 1000)}ms）")
            self._poll_loop()
        except Exception as exc:
            self._log(f"串口通信异常：{exc}", error=True)
            self._emit(self.on_runtime_error, f"串口通信异常：{exc}")
        finally:
            self._close()
            self._log("串口线程结束")

    def _handshake(self) -> tuple[str | None, str]:
        last_error = ""
        for attempt in range(1, HANDSHAKE_RETRIES + 1):
            if self._stop_event.is_set():
                return None, ""
            try:
                self._write(protocol.HARDWARE_VERSION_CMD)
            except Exception as exc:
                return None, f"发送硬件版本指令失败：{exc}"
            deadline = time.monotonic() + HANDSHAKE_TIMEOUT
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    return None, ""
                try:
                    frames = self._read_frames()
                except Exception as exc:
                    return None, f"读取串口数据失败：{exc}"
                for frame in frames:
                    version = protocol.parse_hardware_version(frame)
                    if version:
                        return version, ""
            last_error = f"读取硬件版本超时（第 {attempt}/{HANDSHAKE_RETRIES} 次）"
            self._log(last_error, error=True)
        return None, last_error

    def _poll_loop(self) -> None:
        next_poll = time.monotonic()
        outstanding: float | None = None
        while not self._stop_event.is_set():
            now = time.monotonic()
            for frame in self._read_frames():
                outstanding = self._handle_frame(frame, outstanding)

            if outstanding is None:
                job = self._take_write_job()
                if job is not None:
                    self._run_write_job(*job)
                    next_poll = time.monotonic() + POLL_INTERVAL
                    continue

            now = time.monotonic()
            if now < next_poll:
                continue
            if outstanding is not None and now - outstanding > POLL_TIMEOUT:
                self.timeouts += 1
                outstanding = None
                self._log_throttled("poll-timeout", "轮询响应超时，继续下一轮")
            if outstanding is None:
                pending_tid = self._take_tid_read()
                if pending_tid is not None:
                    self._read_tid(pending_tid)
                    next_poll = time.monotonic() + POLL_INTERVAL
                    continue
                self._write(protocol.SINGLE_POLL_CMD)
                self.polls += 1
                outstanding = now
            next_poll += POLL_INTERVAL
            if next_poll <= now:
                next_poll = now + POLL_INTERVAL

    def _handle_frame(self, frame: protocol.Frame, outstanding: float | None) -> float | None:
        tag = protocol.parse_tag(frame)
        if tag is not None:
            self._on_tag(tag)
            return outstanding
        if frame.frame_type != protocol.TYPE_RESPONSE:
            return outstanding
        if frame.command == protocol.CMD_SINGLE_POLL:
            return None
        if frame.command == protocol.CMD_ERROR:
            error = protocol.parse_error(frame)
            if error is None:
                return None
            if error.code == 0x15:
                self.empty_polls += 1
                if error.epc:
                    self._on_tag(protocol.TagRead(error.epc, None, error.pc_mismatch))
                return None
            self.poll_errors += 1
            self._log(f"轮询返回错误：{protocol.error_name(error.code)}")
            if error.epc:
                self._on_tag(protocol.TagRead(error.epc, None, error.pc_mismatch))
            return None
        return outstanding

    def _take_write_job(self) -> tuple[str, bytes] | None:
        with self._write_lock:
            job = self._write_job
            self._write_job = None
            if job is not None:
                self.writing = True
        return job

    def _queue_tid_read(self, epc: str) -> None:
        with self._tid_lock:
            if epc in self._tid_cache or epc in self._tid_queued:
                return
            self._tid_queued.add(epc)
            self._tid_pending.append(epc)

    def _take_tid_read(self) -> str | None:
        with self._tid_lock:
            while self._tid_pending:
                epc = self._tid_pending.popleft()
                self._tid_queued.discard(epc)
                if epc not in self._tid_cache:
                    return epc
        return None

    def _set_tid(self, epc: str, data: bytes) -> None:
        with self._tid_lock:
            self._tid_cache[epc] = data.hex().upper()

    def _read_tid(self, epc: str) -> None:
        select_ok, message = self._select_mask(epc)
        if select_ok:
            for words in protocol.TID_READ_WORDS:
                read_ok, actual, read_epc, message = self._read_tid_data(words)
                if read_ok and read_epc == epc:
                    self._set_tid(epc, actual)
                    self._log(f"TID：{epc} → {actual.hex().upper()}")
                    return
                if read_ok:
                    message = "回读 EPC 与所选不一致"
                    break
                if "0xA3" not in message:
                    break
        self._set_tid(epc, b"")
        self._log(f"读取标签 TID 失败，界面按空值处理：{epc}（{message}）")

    def _run_write_job(self, epc: str, data: bytes) -> None:
        self._emit(self.on_write_state, True)
        expected = data.hex().upper()
        readbacks: list[str] = []
        read_epc: str | None = None
        ok = False
        message = "写入失败"
        try:
            select_ok, message = self._select_mask(epc)
            if select_ok:
                write_ok, message = self._write_epc(data)
                if write_ok:
                    # EPC 已被改写，必须用新 EPC 重新 Select 才能回读
                    select_new_ok, select_message = self._select_mask(expected)
                    if not select_new_ok:
                        message = f"写入已返回成功，但无法用新 EPC 选中标签（{select_message}）"
                    else:
                        read_ok, actual, read_epc, message = self._read_epc(len(data) // 2)
                        if read_ok:
                            readbacks.append(actual.hex().upper())
                            if read_epc == expected and actual == data:
                                ok = True
                                message = "写入成功，回读一致"
                            elif read_epc != expected:
                                message = "回读 EPC 与写入值不一致"
                            else:
                                message = "回读数据与写入值不一致"
                        else:
                            message += "，无法确认写入结果"
        except ValueError as exc:
            message = f"写入参数无效：{exc}"
        finally:
            with self._write_lock:
                self.writing = False
            if ok:
                with self._tid_lock:
                    tid = self._tid_cache.pop(epc, None)
                    if tid:
                        self._tid_cache[expected] = tid
            self._emit(self.on_write_state, False)
            self._emit(
                self.on_write_result,
                {
                    "ok": ok,
                    "message": message,
                    "expected": expected,
                    "actual": readbacks[-1] if readbacks else None,
                    "readbacks": readbacks,
                    "epc": epc,
                    "new_epc": expected,
                    "read_epc": read_epc,
                },
            )

    def _select_mask(self, epc: str) -> tuple[bool, str]:
        frame = self._command(
            protocol.build_select_params(bytes.fromhex(epc)), protocol.CMD_SELECT_PARAMS
        )
        if frame is None:
            return False, "设置 Select 参数超时"
        if frame.command == protocol.CMD_ERROR:
            return False, self._error_message(frame)
        if not protocol.parse_select_success(frame):
            return False, "设置 Select 参数失败"
        return True, ""

    def _write_epc(self, epc: bytes) -> tuple[bool, str]:
        frame = self._command(protocol.build_write_epc(epc), protocol.CMD_WRITE)
        if frame is None:
            return False, "写标签超时"
        if frame.command == protocol.CMD_ERROR:
            return False, self._error_message(frame)
        if not protocol.parse_write_success(frame):
            return False, "写入返回失败"
        return True, ""

    def _read_epc(self, words: int) -> tuple[bool, bytes, str | None, str]:
        frame = self._command(protocol.build_read_epc(words), protocol.CMD_READ)
        if frame is None:
            return False, b"", None, "回读超时"
        if frame.command == protocol.CMD_ERROR:
            return False, b"", None, self._error_message(frame)
        result = protocol.parse_read_data(frame)
        if result is None:
            return False, b"", None, "回读响应无效"
        read_epc, data = result
        return True, data, read_epc, ""

    def _read_tid_data(self, words: int) -> tuple[bool, bytes, str | None, str]:
        frame = self._command(protocol.build_read_tid(words), protocol.CMD_READ)
        if frame is None:
            return False, b"", None, "回读超时"
        if frame.command == protocol.CMD_ERROR:
            return False, b"", None, self._error_message(frame)
        result = protocol.parse_read_data(frame)
        if result is None:
            return False, b"", None, "回读响应无效"
        read_epc, data = result
        return True, data, read_epc, ""

    def _error_message(self, frame: protocol.Frame) -> str:
        error = protocol.parse_error(frame)
        if error is None:
            return "标签返回错误"
        return f"标签返回错误：{protocol.error_name(error.code)}（0x{error.code:02X}）"

    def _command(self, frame: bytes, command: int) -> protocol.Frame | None:
        self._write(frame)
        return self._wait_response(command)

    def _wait_response(self, command: int) -> protocol.Frame | None:
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            for frame in self._read_frames():
                if frame.frame_type != protocol.TYPE_RESPONSE:
                    continue
                if frame.command == command or frame.command == protocol.CMD_ERROR:
                    return frame
        return None

    def _on_tag(self, tag: protocol.TagRead) -> None:
        self.tags += 1
        if tag.pc_mismatch:
            self._log_throttled("pc-mismatch", f"EPC 长度与 PC 不一致（{tag.epc}），已按帧长度处理")
        self._buffer.add(tag.epc, tag.rssi)
        self._queue_tid_read(tag.epc)
        self._emit(self.on_tag, tag.epc, tag.rssi)

    def _read_frames(self) -> list[protocol.Frame]:
        if self._serial is None:
            return []
        data = self._serial.read(self._serial.in_waiting or 1)
        if not data:
            return []
        return self._parser.feed(data)

    def _write(self, data: bytes) -> None:
        if self._serial is None:
            raise RuntimeError("串口未打开")
        self._serial.write(data)

    def _close(self) -> None:
        if self._serial is None:
            return
        try:
            self._serial.close()
        except Exception as exc:
            logger.warning("关闭串口失败：%s", exc)
        self._serial = None

    def _handshake_failed(self, message: str) -> None:
        self._log(message, error=True)
        self._emit(self.on_handshake_failed, message)

    def _emit(self, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("回调执行失败")

    def _log(self, message: str, error: bool = False) -> None:
        if error:
            logger.error(message)
        else:
            logger.info(message)
        self._emit(self.on_log, message)

    def _log_throttled(self, key: str, message: str) -> None:
        now = time.monotonic()
        if now - self._last_log.get(key, 0.0) < 1.0:
            return
        self._last_log[key] = now
        self._log(message)
