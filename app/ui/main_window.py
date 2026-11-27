"""主窗口：串口/波特率/目标地址选择，采集状态与日志展示。"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

import serial.tools.list_ports
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app import config, protocol
from app.buffer import TagBuffer
from app.reader import Reader
from app.uploader import Uploader

BAUD_RATES = [9600, 19200, 28800, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 115200
MAX_TAG_ROWS = 50
MAX_LOG_LINES = 2000
STATS_INTERVAL_MS = 500


class Bridge(QObject):
    tag = Signal(str, object)
    tid = Signal(str, str)
    version = Signal(str)
    handshake_failed = Signal(str)
    runtime_error = Signal(str)
    log = Signal(str)
    write_state = Signal(bool)
    write_result = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RFID EPC 采集上传工具")
        self.resize(940, 780)

        self._config = config.load_config()
        self._buffer: TagBuffer | None = None
        self._reader: Reader | None = None
        self._uploader: Uploader | None = None
        self._stopping = False
        self._running = False
        self._write_busy = False

        self._bridge = Bridge(self)
        self._bridge.tag.connect(self._on_tag)
        self._bridge.tid.connect(self._on_tid)
        self._bridge.version.connect(self._on_version)
        self._bridge.handshake_failed.connect(self._on_handshake_failed)
        self._bridge.runtime_error.connect(self._on_runtime_error)
        self._bridge.log.connect(self._show_worker_log)
        self._bridge.write_state.connect(self._on_write_state)
        self._bridge.write_result.connect(self._on_write_result)

        self._build_ui()
        self._refresh_ports()
        self._apply_config()

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start()

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_collect_page(), "采集")
        self.tabs.addTab(self._build_write_page(), "写入")
        self.setCentralWidget(self.tabs)

    def _build_collect_page(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        settings = QGroupBox("连接设置")
        form = QFormLayout(settings)

        self.port_combo = QComboBox()
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._on_refresh_ports)
        port_row = QWidget()
        port_layout = QHBoxLayout(port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        port_layout.addWidget(self.port_combo, 1)
        port_layout.addWidget(self.refresh_btn)
        form.addRow("串口设备", port_row)

        self.baud_combo = QComboBox()
        for baud in BAUD_RATES:
            self.baud_combo.addItem(str(baud), baud)
        form.addRow("波特率", self.baud_combo)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://127.0.0.1:8000/api/epc")
        form.addRow("HTTP 目标地址", self.url_edit)

        self.device_edit = QLineEdit()
        self.device_edit.setPlaceholderText("RFID-PLATE-001")
        form.addRow("设备 ID", self.device_edit)
        layout.addWidget(settings)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("开始采集")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        status = QGroupBox("运行状态")
        grid = QGridLayout(status)
        grid.addWidget(QLabel("状态："), 0, 0)
        self.state_value = QLabel("空闲")
        self.state_value.setMinimumWidth(80)
        grid.addWidget(self.state_value, 0, 1)
        grid.addWidget(QLabel("硬件版本："), 0, 2)
        self.version_value = QLabel("—")
        grid.addWidget(self.version_value, 0, 3)

        stats = [
            ("轮询次数", "poll_value"),
            ("空盘次数", "empty_value"),
            ("读到标签", "tag_value"),
            ("上传成功", "success_value"),
            ("上传失败", "failure_value"),
            ("缓冲条数", "buffer_value"),
        ]
        for index, (text, attr) in enumerate(stats):
            row = 1 + index // 3
            column = (index % 3) * 2
            grid.addWidget(QLabel(f"{text}："), row, column)
            value = QLabel("0")
            setattr(self, attr, value)
            grid.addWidget(value, row, column + 1)
        grid.setColumnStretch(5, 1)
        layout.addWidget(status)

        tags = QGroupBox("最近标签")
        tags_layout = QVBoxLayout(tags)
        self.tag_table = QTableWidget(0, 4)
        self.tag_table.setHorizontalHeaderLabels(["EPC", "TID", "RSSI", "时间"])
        self.tag_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tag_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tag_table.verticalHeader().setVisible(False)
        header = self.tag_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tag_table.setMaximumHeight(190)
        tags_layout.addWidget(self.tag_table)
        layout.addWidget(tags)

        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 1)

        return root

    def _build_write_page(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        tip = QLabel("需先在“采集”页开始采集。选中标签后输入新的 EPC（2~30 字节 HEX），写入标签 EPC 区，写入成功后标签 EPC 会被替换。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        tags = QGroupBox("识别到的标签（唯一）")
        tags_layout = QVBoxLayout(tags)
        self.unique_table = QTableWidget(0, 4)
        self.unique_table.setHorizontalHeaderLabels(["EPC", "TID", "RSSI", "最后识别"])
        self.unique_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.unique_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.unique_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.unique_table.verticalHeader().setVisible(False)
        header = self.unique_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.unique_table.itemSelectionChanged.connect(self._update_write_button)
        tags_layout.addWidget(self.unique_table)
        layout.addWidget(tags, 1)

        self.write_selection_label = QLabel("未选择标签")
        self.write_selection_label.setWordWrap(True)
        layout.addWidget(self.write_selection_label)

        form = QFormLayout()
        self.write_epc_edit = QLineEdit()
        self.write_epc_edit.setPlaceholderText("E2 00 34 12 AA BB CC DD")
        self.write_epc_edit.textChanged.connect(self._update_write_button)
        form.addRow("新 EPC", self.write_epc_edit)
        layout.addLayout(form)

        hints = QLabel(
            "写入标签 EPC 区（MemBank=0x01，从 word1 写入 PC + 新 EPC），标签 EPC 会被替换。\n"
            "每个字节两位十六进制、空格分隔，长度 2~30 字节且为偶数。\n"
            "PC 按新 EPC 长度自动计算，CRC 由标签自动更新。\n"
            "EPC 区容量不足或长度超过标签支持范围返回 0xB3，已锁定返回 0xB4。\n"
            "Access Password 固定 00000000（不发送 Access 指令）。"
        )
        hints.setWordWrap(True)
        layout.addWidget(hints)

        self.write_hint = QLabel("请先选择要写入的标签")
        self.write_hint.setWordWrap(True)
        layout.addWidget(self.write_hint)

        self.write_btn = QPushButton("写入")
        self.write_btn.setEnabled(False)
        self.write_btn.clicked.connect(self._on_write)
        layout.addWidget(self.write_btn)

        result_group = QGroupBox("写入结果")
        result_layout = QVBoxLayout(result_group)
        self.write_result = QPlainTextEdit()
        self.write_result.setReadOnly(True)
        self.write_result.setMaximumHeight(120)
        result_layout.addWidget(self.write_result)
        layout.addWidget(result_group)

        self._update_write_button()
        return root

    def _apply_config(self) -> None:
        url = self._config.get("url", "")
        if url:
            self.url_edit.setText(url)
        self.device_edit.setText(self._config.get("device_id", ""))
        index = self.baud_combo.findData(self._config.get("baud", DEFAULT_BAUD))
        self.baud_combo.setCurrentIndex(index if index >= 0 else self.baud_combo.findData(DEFAULT_BAUD))

    def _refresh_ports(self, preferred: str | None = None) -> None:
        if preferred is None:
            preferred = self.port_combo.currentData() or self._config.get("port", "")
        self.port_combo.clear()
        ports = sorted(serial.tools.list_ports.comports(), key=lambda item: item.device)
        for info in ports:
            label = info.device
            if info.description and info.description != "n/a":
                label = f"{info.device} - {info.description}"
            self.port_combo.addItem(label, info.device)
        if not ports:
            self.port_combo.addItem("未找到串口", None)
            return
        index = self.port_combo.findData(preferred)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)

    def _on_refresh_ports(self) -> None:
        self._refresh_ports()
        self._append_log("已刷新串口列表")

    def _on_start(self) -> None:
        if self._reader is not None:
            return
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "未选择串口", "没有可用串口，请连接设备后点击“刷新”。")
            return
        url = self.url_edit.text().strip()
        error = self._validate_url(url)
        if error:
            QMessageBox.warning(self, "地址无效", error)
            self.url_edit.setFocus()
            return
        baud = self.baud_combo.currentData()
        if baud is None:
            QMessageBox.warning(self, "未选择波特率", "请选择波特率。")
            return

        self._config.update(
            {"port": port, "baud": baud, "url": url, "device_id": self.device_edit.text().strip()}
        )
        config.save_config(self._config)

        self._buffer = TagBuffer()
        self._reader = Reader(
            port,
            baud,
            self._buffer,
            on_tag=self._bridge.tag.emit,
            on_tid=self._bridge.tid.emit,
            on_version=self._bridge.version.emit,
            on_handshake_failed=self._bridge.handshake_failed.emit,
            on_runtime_error=self._bridge.runtime_error.emit,
            on_log=self._bridge.log.emit,
            on_write_state=self._bridge.write_state.emit,
            on_write_result=self._bridge.write_result.emit,
        )
        self.tag_table.setRowCount(0)
        self.unique_table.setRowCount(0)
        self.write_result.clear()
        self._set_running(True)
        self.state_value.setText("读取硬件版本…")
        self._append_log(f"打开串口 {port} @ {baud}，读取硬件版本…")
        self._reader.start()

    def _on_stop(self) -> None:
        self._stop()

    def _stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        reader, uploader, buffer = self._reader, self._uploader, self._buffer
        self._reader = self._uploader = self._buffer = None
        if reader is not None:
            reader.stop()
            reader.join(timeout=3)
        if buffer is not None:
            buffer.stop()
        if uploader is not None:
            uploader.stop()
            uploader.join(timeout=8)
            if uploader.is_alive():
                self._append_log("上传线程未在 8 秒内退出，程序退出时将被终止")
        self._set_running(False)
        self.state_value.setText("空闲")
        self._append_log("采集已停止")
        self._stopping = False

    def _set_running(self, running: bool) -> None:
        self._running = running
        for widget in (
            self.port_combo,
            self.refresh_btn,
            self.baud_combo,
            self.url_edit,
            self.device_edit,
        ):
            widget.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.unique_table.setEnabled(running)
        self.write_epc_edit.setEnabled(running)
        if not running:
            self._write_busy = False
        self._update_write_button()

    def _on_version(self, version: str) -> None:
        self.version_value.setText(version or "—")
        self.state_value.setText("采集中")
        self._append_log(f"硬件版本：{version}")
        if self._uploader is not None or self._buffer is None:
            return
        self._uploader = Uploader(
            self._buffer,
            self.url_edit.text().strip(),
            device_id=self.device_edit.text().strip(),
            on_log=self._bridge.log.emit,
        )
        self._uploader.start()

    def _on_tag(self, epc: str, rssi: object) -> None:
        tid = self._reader.get_tid(epc) if self._reader is not None else ""
        self.tag_table.insertRow(0)
        self.tag_table.setItem(0, 0, QTableWidgetItem(epc))
        self.tag_table.setItem(0, 1, QTableWidgetItem(tid or "—"))
        rssi_text = f"{rssi} dBm" if rssi is not None else "—"
        self.tag_table.setItem(0, 2, QTableWidgetItem(rssi_text))
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.tag_table.setItem(0, 3, QTableWidgetItem(stamp))
        while self.tag_table.rowCount() > MAX_TAG_ROWS:
            self.tag_table.removeRow(self.tag_table.rowCount() - 1)
        self._record_unique_tag(epc, tid, rssi_text, stamp)

    def _on_tid(self, epc: str, tid: str) -> None:
        text = tid or "—"
        for row in range(self.tag_table.rowCount()):
            item = self.tag_table.item(row, 0)
            if item is not None and item.text() == epc:
                self.tag_table.item(row, 1).setText(text)
        for row in range(self.unique_table.rowCount()):
            item = self.unique_table.item(row, 0)
            if item is not None and item.text() == epc:
                self.unique_table.item(row, 1).setText(text)
                return

    def _record_unique_tag(self, epc: str, tid: str, rssi_text: str, stamp: str) -> None:
        for row in range(self.unique_table.rowCount()):
            if self.unique_table.item(row, 0).text() == epc:
                self.unique_table.item(row, 1).setText(tid or "—")
                self.unique_table.item(row, 2).setText(rssi_text)
                self.unique_table.item(row, 3).setText(stamp)
                if self._is_selected_row(row):
                    self._update_write_button()
                return
        row = self.unique_table.rowCount()
        self.unique_table.insertRow(row)
        self.unique_table.setItem(row, 0, QTableWidgetItem(epc))
        self.unique_table.setItem(row, 1, QTableWidgetItem(tid or "—"))
        self.unique_table.setItem(row, 2, QTableWidgetItem(rssi_text))
        self.unique_table.setItem(row, 3, QTableWidgetItem(stamp))

    def _is_selected_row(self, row: int) -> bool:
        return any(index.row() == row for index in self.unique_table.selectedIndexes())

    def _selected_epc(self) -> str | None:
        rows = self.unique_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.unique_table.item(rows[0].row(), 0)
        return item.text() if item else None

    def _update_write_button(self) -> None:
        epc = self._selected_epc()
        if epc is None:
            self.write_selection_label.setText("未选择标签")
            self.write_hint.setText("请先在上表选择要写入的标签")
            ok = False
        else:
            self.write_selection_label.setText(f"当前选择：{epc}")
            try:
                data = protocol.prepare_epc_write(self.write_epc_edit.text())
            except ValueError as exc:
                self.write_hint.setText(str(exc))
                ok = False
            else:
                self.write_hint.setText(
                    f"输入合法：{len(data)} 字节，写入后 EPC 将变为 {data.hex().upper()}"
                )
                ok = True
        self.write_btn.setEnabled(ok and self._running and not self._write_busy)

    def _on_write_state(self, running: bool) -> None:
        self._write_busy = running
        self.unique_table.setEnabled(self._running and not running)
        self.write_epc_edit.setEnabled(self._running and not running)
        self._update_write_button()

    def _on_write(self) -> None:
        epc = self._selected_epc()
        if epc is None or self._reader is None or self._write_busy:
            return
        try:
            data = protocol.prepare_epc_write(self.write_epc_edit.text())
        except ValueError as exc:
            self.write_result.setPlainText(f"输入无效：{exc}")
            return
        if not self._reader.submit_epc_write(epc, data):
            self.write_result.setPlainText("写入任务未提交：当前已有写入任务或采集未就绪")
            return
        self._write_busy = True
        self.write_result.setPlainText(f"已提交：{epc} → {data.hex().upper()}，等待结果…")
        self._update_write_button()
        self._append_log(f"提交写入 EPC：{epc} → {data.hex().upper()}")

    def _replace_epc_row(self, old_epc: str, new_epc: str) -> None:
        for row in range(self.unique_table.rowCount()):
            item = self.unique_table.item(row, 0)
            if item is not None and item.text() == old_epc:
                item.setText(new_epc)
                if self._is_selected_row(row):
                    self.write_selection_label.setText(f"当前选择：{new_epc}")
                return

    def _on_write_result(self, result: dict) -> None:
        self._write_busy = False
        lines = [
            f"结果：{'成功' if result['ok'] else '失败'}",
            f"说明：{result['message']}",
            f"原 EPC：{result['epc']}",
            f"新 EPC：{result['new_epc']}",
        ]
        if result["read_epc"]:
            lines.append(f"回读 EPC：{result['read_epc']}")
        if result["readbacks"]:
            lines.append("回读数据：" + " / ".join(result["readbacks"]))
        self.write_result.setPlainText("\n".join(lines))
        if result["ok"]:
            self._replace_epc_row(result["epc"], result["new_epc"])
        self._append_log(f"写入{'成功' if result['ok'] else '失败'}：{result['message']}")
        self._update_write_button()

    def _on_handshake_failed(self, message: str) -> None:
        self._show_worker_log(f"错误：{message}")
        QMessageBox.critical(
            self,
            "读取硬件版本失败",
            f"{message}\n\n请检查串口、波特率与接线后重新选择。",
        )
        self._stop()
        self._refresh_ports()
        self.port_combo.setFocus()

    def _on_runtime_error(self, message: str) -> None:
        self._show_worker_log(f"错误：{message}")
        QMessageBox.critical(self, "采集已停止", message)
        self._stop()
        self._refresh_ports()

    def _refresh_stats(self) -> None:
        reader = self._reader
        uploader = self._uploader
        buffer = self._buffer
        self.poll_value.setText(str(reader.polls) if reader else "0")
        self.empty_value.setText(str(reader.empty_polls) if reader else "0")
        self.tag_value.setText(str(reader.tags) if reader else "0")
        self.success_value.setText(str(uploader.success_batches) if uploader else "0")
        self.failure_value.setText(str(uploader.failed_batches) if uploader else "0")
        self.buffer_value.setText(str(buffer.size()) if buffer else "0")

    def _validate_url(self, url: str) -> str:
        if not url:
            return "请填写 HTTP 目标地址。"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return "地址必须以 http:// 或 https:// 开头。"
        if not parts.netloc:
            return "地址缺少主机名。"
        return ""

    def _show_worker_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {message}")

    def _append_log(self, message: str) -> None:
        config.logger.info(message)
        self._show_worker_log(message)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._stop()
        self._config.update(
            {
                "port": self.port_combo.currentData() or "",
                "baud": self.baud_combo.currentData() or DEFAULT_BAUD,
                "url": self.url_edit.text().strip(),
                "device_id": self.device_edit.text().strip(),
            }
        )
        config.save_config(self._config)
        event.accept()
