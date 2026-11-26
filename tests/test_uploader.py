import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app.uploader as uploader_mod
from app.buffer import TagBuffer
from app.uploader import Uploader


class EchoHandler(BaseHTTPRequestHandler):
    received: list[tuple[str, str, str, bytes]] = []
    status_code = 200
    response_body = b'{"ok": true}'

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received.append(
            (
                self.headers.get("Content-Type", ""),
                self.headers.get("X-Device-Id", ""),
                self.path,
                body,
            )
        )
        self.send_response(type(self).status_code)
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, *args):
        pass


class UploaderTest(unittest.TestCase):
    def setUp(self):
        EchoHandler.received = []
        EchoHandler.status_code = 200
        EchoHandler.response_body = b'{"ok": true}'
        self._saved_window = uploader_mod.AGGREGATE_WINDOW
        uploader_mod.AGGREGATE_WINDOW = 0.1
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/epc"
        self.logs: list[str] = []

    def tearDown(self):
        uploader_mod.AGGREGATE_WINDOW = self._saved_window
        self.server.shutdown()
        self.server.server_close()

    def wait_until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        return predicate()

    def stop(self, buffer: TagBuffer, uploader: Uploader) -> None:
        buffer.stop()
        uploader.stop()
        uploader.join(timeout=3)

    def test_posts_deduplicated_epc(self):
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        buffer.add("AA3412", -45)
        buffer.add("BB7788", -60)
        uploader = Uploader(buffer, self.url, on_log=self.logs.append)
        uploader.start()

        self.assertTrue(self.wait_until(lambda: bool(EchoHandler.received)))
        self.stop(buffer, uploader)

        self.assertEqual(len(EchoHandler.received), 1)
        content_type, device_id, path, body = EchoHandler.received[0]
        self.assertEqual(path, "/api/epc")
        self.assertEqual(content_type, "application/json")
        self.assertEqual(device_id, "RFID-PLATE-001")
        self.assertEqual(json.loads(body), ["AA3412", "BB7788"])
        self.assertEqual(uploader.success_batches, 1)
        self.assertEqual(uploader.sent_tags, 2)

    def test_custom_device_id_header(self):
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        uploader = Uploader(buffer, self.url, device_id="RFID-PLATE-007")
        uploader.start()

        self.assertTrue(self.wait_until(lambda: bool(EchoHandler.received)))
        self.stop(buffer, uploader)

        self.assertEqual(EchoHandler.received[0][1], "RFID-PLATE-007")

    def test_ok_false_discards_batch_without_retry(self):
        EchoHandler.response_body = b'{"ok": false}'
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        buffer.add("BB7788", -60)
        uploader = Uploader(buffer, self.url)
        uploader.start()

        self.assertTrue(self.wait_until(lambda: uploader.failed_batches == 1))
        time.sleep(0.3)
        self.stop(buffer, uploader)

        self.assertEqual(len(EchoHandler.received), 1)
        self.assertEqual(buffer.size(), 0)
        self.assertEqual(uploader.failed_batches, 1)

    def test_bad_request_discards_batch_without_retry(self):
        EchoHandler.status_code = 400
        EchoHandler.response_body = b'{"ok": false}'
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        uploader = Uploader(buffer, self.url)
        uploader.start()

        self.assertTrue(self.wait_until(lambda: uploader.failed_batches == 1))
        time.sleep(0.3)
        self.stop(buffer, uploader)

        self.assertEqual(len(EchoHandler.received), 1)
        self.assertEqual(buffer.size(), 0)

    def test_server_error_is_retried_then_requeued(self):
        EchoHandler.status_code = 500
        EchoHandler.response_body = b'{"ok": false}'
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        buffer.add("BB7788", -60)
        uploader = Uploader(buffer, self.url, attempts=2)
        uploader.start()

        self.assertTrue(
            self.wait_until(lambda: uploader.failed_batches == 1)
        )
        self.assertEqual(buffer.size(), 2)
        self.stop(buffer, uploader)

        self.assertEqual(len(EchoHandler.received), 2)
        self.assertEqual(buffer.size(), 2)
        self.assertEqual(uploader.failed_batches, 1)

    def test_invalid_response_discards_batch(self):
        EchoHandler.response_body = b"ok"
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        uploader = Uploader(buffer, self.url)
        uploader.start()

        self.assertTrue(self.wait_until(lambda: uploader.failed_batches == 1))
        time.sleep(0.3)
        self.stop(buffer, uploader)

        self.assertEqual(len(EchoHandler.received), 1)
        self.assertEqual(buffer.size(), 0)

    def test_failure_requeues_batch(self):
        buffer = TagBuffer()
        buffer.add("AA3412", -50)
        buffer.add("BB7788", -60)
        uploader = Uploader(buffer, "http://127.0.0.1:1/api/epc", attempts=1, timeout=0.5)
        uploader.start()

        self.assertTrue(
            self.wait_until(lambda: uploader.failed_batches == 1 and buffer.size() == 2)
        )
        self.stop(buffer, uploader)

        self.assertEqual(buffer.size(), 2)
        self.assertEqual(uploader.failed_batches, 1)


if __name__ == "__main__":
    unittest.main()
