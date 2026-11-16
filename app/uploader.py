"""HTTP 上传线程：从缓冲区整批取走 EPC 并 POST，失败重试后回填缓冲区。"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Callable

from app import protocol
from app.buffer import TagBuffer

HTTP_TIMEOUT = 10.0
ATTEMPTS = 3
RETRY_DELAYS = (0.5, 1.0, 2.0)
REQUEUE_PAUSE = 1.0
AGGREGATE_WINDOW = 2.0
DEFAULT_DEVICE_ID = "RFID-PLATE-001"

logger = logging.getLogger("rfid")


class Uploader(threading.Thread):
    def __init__(
        self,
        buffer: TagBuffer,
        url: str,
        device_id: str = DEFAULT_DEVICE_ID,
        on_log: Callable[[str], None] | None = None,
        get_user: Callable[[str], str] | None = None,
        timeout: float = HTTP_TIMEOUT,
        attempts: int = ATTEMPTS,
    ) -> None:
        super().__init__(name="http-uploader", daemon=True)
        self._buffer = buffer
        self._url = url
        self._device_id = device_id.strip() or DEFAULT_DEVICE_ID
        self.on_log = on_log
        self._get_user = get_user or (lambda epc: protocol.USER_DATA_ZERO)
        self._timeout = timeout
        self._attempts = attempts
        self._stop_event = threading.Event()

        self.success_batches = 0
        self.failed_batches = 0
        self.sent_tags = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while True:
            batch = self._buffer.take_batch(AGGREGATE_WINDOW)
            if batch is None:
                return
            payload = json.dumps(
                [{"epc": epc, "user": self._get_user(epc)} for epc, _ in batch],
                ensure_ascii=False,
            ).encode("utf-8")
            ok, retryable, error = self._post(payload)
            if ok:
                self.success_batches += 1
                self.sent_tags += len(batch)
                self._log(f"上传成功：{len(batch)} 条")
            else:
                self.failed_batches += 1
                if self._stop_event.is_set():
                    self._log(f"上传失败且正在停止，丢弃 {len(batch)} 条：{error}", error=True)
                    return
                if not retryable:
                    self._log(f"上传失败，丢弃 {len(batch)} 条：{error}", error=True)
                else:
                    self._buffer.requeue(batch)
                    self._log(f"上传失败，{len(batch)} 条已放回缓冲区：{error}", error=True)
                    self._stop_event.wait(REQUEUE_PAUSE)
            if self._stop_event.is_set():
                remaining = self._buffer.size()
                if remaining:
                    self._log(f"程序停止，缓冲区剩余 {remaining} 条未上传", error=True)
                return

    def _post(self, payload: bytes) -> tuple[bool, bool, str]:
        """返回 (是否成功, 是否可重试, 错误说明)。"""
        last_error = ""
        for attempt in range(self._attempts):
            if attempt and self._stop_event.is_set():
                return False, False, "已停止"
            retryable = True
            try:
                request = urllib.request.Request(
                    self._url,
                    data=payload,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Device-Id": self._device_id,
                    },
                )
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    status = response.status
                    body = response.read()
                if 200 <= status < 300:
                    ok, error = self._parse_response(body)
                    if ok:
                        return True, False, ""
                    return False, False, error
                retryable = status >= 500
                last_error = f"HTTP {status}"
            except urllib.error.HTTPError as exc:
                retryable = exc.code >= 500
                last_error = f"HTTP {exc.code}"
                exc.close()
            except Exception as exc:
                last_error = str(exc)
            if not retryable:
                return False, False, last_error
            if attempt < self._attempts - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                if self._stop_event.wait(delay):
                    return False, False, "已停止"
        return False, True, last_error

    @staticmethod
    def _parse_response(body: bytes) -> tuple[bool, str]:
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return False, "响应不是有效 JSON"
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            return False, "响应缺少布尔字段 ok"
        if data["ok"]:
            return True, ""
        return False, "服务端返回 ok=false"

    def _log(self, message: str, error: bool = False) -> None:
        if error:
            logger.error(message)
        else:
            logger.info(message)
        if self.on_log is None:
            return
        try:
            self.on_log(message)
        except Exception:
            logger.exception("日志回调执行失败")
