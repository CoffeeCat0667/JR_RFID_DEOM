# RFID 采集上传与标签 EPC 写入逻辑说明

> 适用版本：`261115Ver1.0`（2026-11-26 修订）
> 适用对象：维护 / 对接 / 现场排查人员
> 覆盖范围：从 RFID 读写器采集 EPC → 缓冲聚合 → HTTP 上报服务器的完整链路；以及在“写入”页把标签 EPC 替换为新值的完整链路。
> 代码基线：`main` 分支（HTTP 上报为 **EPC 字符串数组**；采集时额外读取 **TID** 并在界面显示；写入页写 **EPC 区**，不读写 User 区）。

---

## 目录

1. [系统总览](#1-系统总览)
2. [目录与职责](#2-目录与职责)
3. [串口协议基础](#3-串口协议基础)
4. [采集链路：设备 → 缓冲区](#4-采集链路设备--缓冲区)
5. [上报链路：缓冲区 → HTTP 服务器](#5-上报链路缓冲区--http-服务器)
6. [写入链路：界面 → 标签 EPC 区](#6-写入链路界面--标签-epc-区)
7. [界面装配与线程模型](#7-界面装配与线程模型)
8. [配置与日志](#8-配置与日志)
9. [常量与错误码速查](#9-常量与错误码速查)
10. [完整时序](#10-完整时序)
11. [测试与验证](#11-测试与验证)
12. [已知边界与注意事项](#12-已知边界与注意事项)
13. [关键代码索引](#13-关键代码索引)

---

## 1. 系统总览

程序是一个 Windows 桌面工具（Python 3.14 + PySide6 + pyserial），通过串口连接 E310 系列 UHF 读写器：

- **采集**：每 100ms 发送一次单次轮询命令，解析标签通知帧，得到 EPC（大写十六进制）与 RSSI；首次识别到新 EPC 时额外读取 TID 并在界面显示。
- **聚合**：标签进入线程安全缓冲区，按 EPC 去重；连续 2 秒没有“新的唯一 EPC”后整批取出。
- **上报**：整批以 JSON 字符串数组 POST 到 MES 地址，带 `X-Device-Id`；只有 HTTP 2xx 且响应 `{"ok": true}` 视为成功。TID 不上报。
- **写入**：在“写入”页选中已识别的标签，输入 2~30 字节偶数长度的 HEX 作为**新 EPC**，写入标签 EPC 区；PC 按新长度自动计算，CRC 由标签自动更新；写后用新 EPC 重新 Select 并回读校验。

### 1.1 三线程模型

| 线程 | 类 | 职责 |
|---|---|---|
| Qt 主线程 | `MainWindow` / `Bridge` | 界面、配置、日志展示；通过 Qt Signal 接收工作线程回调 |
| 串口线程 | `app.reader.Reader`（`threading.Thread`） | 打开串口、握手、100ms 轮询、解析标签、执行 EPC 写入任务 |
| 上传线程 | `app.uploader.Uploader`（`threading.Thread`） | 从缓冲区取批、构造 JSON、POST、重试、回填 |

三者之间**不共享可变 UI 对象**：工作线程只通过回调 → `Bridge` Signal → 主线程槽函数更新界面；共享数据只有 `TagBuffer`（条件变量保护）和写入任务（互斥锁保护）。

### 1.2 数据流

```text
┌──────────────┐  串口帧    ┌─────────────────────┐
│ E310 读写器  │ ─────────► │ Reader 线程          │
│ (COMx)       │ ◄───────── │ 100ms 单次轮询       │
└──────────────┘  0x22 命令 └──────────┬──────────┘
                                       │ TagBuffer.add(epc, rssi)
                                       │ 首次识别新 EPC：Select + 读 TID
                                       ▼
                             ┌─────────────────────┐
                             │ TagBuffer            │
                             │ dict 去重 / 上限 5000 │
                             └──────────┬──────────┘
                                        │ take_batch(2.0s 静默)
                                        ▼
                             ┌─────────────────────┐
                             │ Uploader 线程        │
                             │ EPC 数组 + POST + 重试│
                             └──────────┬──────────┘
                                        │ HTTP
                                        ▼
                             ┌─────────────────────┐
                             │ MES / 验证服务器     │
                             └─────────────────────┘

写入链路（复用同一条串口，写入期间暂停轮询）：

界面“写入”页 ──submit_epc_write()──► Reader._write_job
                                          │
                                          ▼
             0x0C Select(旧 EPC) → 0x49 写 PC+新 EPC
             → 0x0C Select(新 EPC) → 0x39 读 EPC 区
                                          │
                                          ▼
                          结果 dict ──Signal──► 界面结果区 / 替换表格 EPC
```

---

## 2. 目录与职责

```text
app/
├── main.py                 程序入口：日志初始化、QApplication、MainWindow
├── config.py               配置读写（%LOCALAPPDATA%\JRRFID\config.json）与文件日志
├── protocol.py             串口协议：组帧、流式解帧、响应解析、EPC 长度校验
├── reader.py               串口读卡线程：握手、轮询、标签回调、EPC 写入任务
├── buffer.py               线程安全 EPC 缓冲区：去重、静默聚合、回填
├── uploader.py             HTTP 上报线程：取批、POST、重试、回填
└── ui/
    └── main_window.py      主窗口：采集页、写入页、Bridge 信号、启动/停止
tests/
├── test_protocol.py        组帧/解帧/解析/EPC 长度校验/聚合
├── test_uploader.py        真实本地 HTTP 服务器下的上报行为
└── test_writer.py          EPC 写入流程（假串口 + EPC 区存储模型）
verify/rfid_server.py       本地接收验证服务器（接受 EPC 字符串数组，本次未改动）
verify/rfid_server_TID.py    TID 版验证服务器（兼容 EPC 数组与 {"epc","tid"} 对象数组）
```

---

## 3. 串口协议基础

### 3.1 帧格式

所有命令与响应统一为：

```text
┌──────┬──────┬──────┬────────┬────────┬───────────────┬──────┬──────┐
│ 0xBB │ Type │ Cmd  │ PLH    │ PLL    │ Param(PL 字节) │  CK  │ 0x7E │
└──────┴──────┴──────┴────────┴────────┴───────────────┴──────┴──────┘
  帧头    1B     1B    高字节    低字节      PL = 参数长度     校验   帧尾
```

- 帧总长度 = `5 + PL + 2`。
- `CK = sum(Type, Cmd, PLH, PLL, Param...) & 0xFF`。
- `Type`：`0x00` 命令、`0x01` 响应、`0x02` 通知。
- 参数中允许出现 `0x7E` / `0xBB`，因此解析必须按 `PL` 长度切帧，不能搜索帧尾。

### 3.2 用到的命令

| 命令 | 代码 | 用途 | 参数结构 |
|---|---|---|---|
| 获取硬件版本 | `0x03` | 握手 | `00` |
| 单次轮询 | `0x22` | 采集标签 | 无参数 |
| 设置 Select 参数 | `0x0C` | 选中唯一标签 | `SelParam + Ptr + MaskLen + Truncate + Mask` |
| 读标签数据存储区 | `0x39` | 写入后回读 EPC 区 / 读取 TID | `AP(4) + MemBank + SA(2) + DL(2)` |
| 写标签数据存储区 | `0x49` | 写 PC + 新 EPC | `AP(4) + MemBank + SA(2) + DL(2) + DT` |
| 失败响应 | `0xFF` | 错误码 | `错误码 [+ PC + EPC]` |

### 3.3 EPC 区布局

EPC 存储区（`MemBank=0x01`）按 word 排列：

| 地址 | 内容 | 说明 |
|---|---|---|
| word 0 | CRC-16 | 标签维护；写入 EPC 后由标签自动重算，程序不写 |
| word 1 | PC | 高 5 位为 EPC 字数，其余为 UMI/XI 等标志位 |
| word 2 起 | EPC | 标签身份，长度由 PC 决定 |

本工具写 EPC 时从 **word 1** 开始写 `PC + 新 EPC`，一次命令同时更新长度与内容。

### 3.4 组帧与解帧（`app/protocol.py`）

```python
HEADER = 0xBB
END = 0x7E

TYPE_COMMAND = 0x00
TYPE_RESPONSE = 0x01
TYPE_NOTIFICATION = 0x02

CMD_INFO = 0x03
CMD_SINGLE_POLL = 0x22
CMD_SELECT_PARAMS = 0x0C
CMD_READ = 0x39
CMD_WRITE = 0x49
CMD_ERROR = 0xFF

MEM_BANK_EPC = 0x01
MEM_BANK_TID = 0x02
MAX_EPC_MASK_LEN = 31
MAX_WRITE_EPC_LEN = 30

# EPC 区布局：word0 为 CRC（标签在写入 EPC 后自动更新），word1 为 PC，word2 起为 EPC。
EPC_SA_PC = 0x0001
EPC_SA_DATA = 0x0002

# TID 长度因标签而异：依次尝试 8/6/4/2 word，取第一个读取成功的实际长度。
TID_READ_WORDS = (8, 6, 4, 2)

MAX_PARAM_LEN = 256   # 用于从垃圾字节中重新同步


def checksum(data: bytes) -> int:
    """从帧类型到最后一个参数字节的累加和，取最低字节。"""
    return sum(data) & 0xFF


def build_frame(frame_type: int, command: int, param: bytes = b"") -> bytes:
    length = len(param)
    body = bytes([frame_type, command, (length >> 8) & 0xFF, length & 0xFF]) + param
    return bytes([HEADER]) + body + bytes([checksum(body), END])
```

流式解析器：每次把串口读到的字节喂入，内部保留半帧；只有长度够、帧尾正确、校验通过才产出一帧：

```python
class FrameParser:
    """按 PL 长度切帧的流式解析器，不依赖帧尾搜索，载荷中的 0x7E/0xBB 不会误切。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(HEADER)
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]              # 丢弃帧头前的垃圾字节
            if len(self._buf) < 5:
                break
            length = (self._buf[3] << 8) | self._buf[4]
            if length > MAX_PARAM_LEN:              # 明显不是合法帧，前移 1 字节重同步
                del self._buf[0]
                continue
            total = 5 + length + 2
            if len(self._buf) < total:
                break                               # 半帧，等待后续字节
            raw = bytes(self._buf[:total])
            if raw[-1] != END or checksum(raw[1:5 + length]) != raw[5 + length]:
                del self._buf[0]                    # 校验失败，前移 1 字节重同步
                continue
            frames.append(Frame(raw[1], raw[2], raw[5:5 + length], raw))
            del self._buf[:total]
        return frames
```

### 3.5 关键解析函数

```python
def parse_hardware_version(frame: Frame) -> str | None:
    """0x03 响应：Param[0] == 0x00，其后为 ASCII 版本号。"""
    if frame.frame_type != TYPE_RESPONSE or frame.command != CMD_INFO:
        return None
    if len(frame.param) < 1 or frame.param[0] != 0x00:
        return None
    return frame.param[1:].decode("ascii", errors="replace").strip()


def parse_tag(frame: Frame) -> TagRead | None:
    """通知帧：RSSI(1) + PC(2) + EPC(PL-5) + CRC(2)。"""
    if frame.frame_type != TYPE_NOTIFICATION or frame.command != CMD_SINGLE_POLL:
        return None
    if len(frame.param) < 7:
        return None
    rssi = frame.param[0] - 256 if frame.param[0] > 127 else frame.param[0]
    pc = (frame.param[1] << 8) | frame.param[2]
    epc = frame.param[3:-2]                        # 去掉 PC 与 CRC
    if not epc:
        return None
    words = (pc >> 11) & 0x1F                      # PC 高 5 位 = EPC 字数
    return TagRead(epc.hex().upper(), rssi, words * 2 != len(epc))


def parse_error(frame: Frame) -> ErrorRead | None:
    """0xFF 响应：Param[0] 为错误码；部分错误还会携带 PC + EPC。"""
    if frame.frame_type != TYPE_RESPONSE or frame.command != CMD_ERROR:
        return None
    if not frame.param:
        return None
    code = frame.param[0]
    rest = frame.param[1:]
    if len(rest) < 3:
        return ErrorRead(code)
    pc = (rest[0] << 8) | rest[1]
    data = rest[2:]
    words = (pc >> 11) & 0x1F
    mismatch = words * 2 != len(data)
    if 0 < words * 2 < len(data):
        data = data[: words * 2]
        mismatch = False
    return ErrorRead(code, data.hex().upper() if data else None, mismatch)
```

```python
def parse_read_data(frame: Frame) -> tuple[str, bytes] | None:
    """0x39 响应：UL + PC + EPC + Data，返回 (EPC 十六进制, 数据)。"""
    if frame.frame_type != TYPE_RESPONSE or frame.command != CMD_READ:
        return None
    if len(frame.param) < 4:
        return None
    ul = frame.param[0]                            # 含 PC 与 EPC 的总字节数
    if ul < 2 or len(frame.param) <= 1 + ul:
        return None
    epc = frame.param[3:1 + ul]
    data = frame.param[1 + ul:]
    if not epc or not data:
        return None
    return epc.hex().upper(), data


def parse_select_success(frame: Frame) -> bool:
    """0x0C 响应：Param[0] == 0x00 表示成功。"""
    return (
        frame.frame_type == TYPE_RESPONSE
        and frame.command == CMD_SELECT_PARAMS
        and len(frame.param) >= 1
        and frame.param[0] == 0x00
    )


def parse_write_success(frame: Frame) -> bool:
    """0x49 响应：Param[-1] == 0x00 表示成功。"""
    return (
        frame.frame_type == TYPE_RESPONSE
        and frame.command == CMD_WRITE
        and len(frame.param) >= 1
        and frame.param[-1] == 0x00
    )
```

---

## 4. 采集链路：设备 → 缓冲区

### 4.1 打开串口

`Reader.run()` 先按界面选择的串口与波特率打开串口，清空收发缓冲：

```python
self._serial = serial.Serial(
    self.port,
    self.baudrate,
    timeout=SERIAL_TIMEOUT,        # 0.02s，避免读操作长时间阻塞
    write_timeout=1,
)
self._serial.reset_input_buffer()
self._serial.reset_output_buffer()
```

打开失败 → 回调 `on_handshake_failed`，界面弹窗并恢复选择。

### 4.2 握手

发送 `BB 00 03 00 01 00 04 7E` 获取硬件版本：

- 每次尝试等待 **1.5 秒**，最多 **3 次**。
- 超时只记日志并重试；3 次都失败则回调 `on_handshake_failed`。
- 收到 `0x03` 响应且 `Param[0]=0x00`，把 `Param[1:]` 按 ASCII 解析为版本号。

```python
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
```

握手成功后：

```python
self.version = version
self._emit(self.on_version, version)     # 界面创建并启动 Uploader
self._log(...)
self._poll_loop()
```

### 4.3 100ms 轮询循环

核心约束：

- 默认每 **100ms** 发送 `BB 00 22 00 00 22 7E`。
- 同一时刻只允许 **1 条轮询在途**；250ms 未回则跳过本轮。
- 用 `next_poll` 单调递增调度，避免因为处理耗时导致“补发风暴”。
- 空闲时优先执行待处理的 EPC 写入任务。

```python
def _poll_loop(self) -> None:
    next_poll = time.monotonic()
    outstanding: float | None = None
    while not self._stop_event.is_set():
        now = time.monotonic()

        # 先把串口里已到达的字节全部解析成帧
        for frame in self._read_frames():
            outstanding = self._handle_frame(frame, outstanding)

        # 没有在途轮询时，优先执行写入任务
        if outstanding is None:
            job = self._take_write_job()
            if job is not None:
                self._run_write_job(*job)
                next_poll = time.monotonic() + POLL_INTERVAL
                continue

        now = time.monotonic()
        if now < next_poll:
            continue

        # 轮询超时：只记一次超时，丢弃在途标记
        if outstanding is not None and now - outstanding > POLL_TIMEOUT:
            self.timeouts += 1
            outstanding = None
            self._log_throttled("poll-timeout", "轮询响应超时，继续下一轮")

        # 没有在途请求时才读 TID / 发下一条轮询，保证单条在途
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
        if next_poll <= now:                 # 处理耗时超过周期时不补发
            next_poll = now + POLL_INTERVAL
```

### 4.4 帧分发

```python
def _handle_frame(self, frame: protocol.Frame,
                  outstanding: float | None) -> float | None:
    # 1) 标签通知帧：优先解析
    tag = protocol.parse_tag(frame)
    if tag is not None:
        self._on_tag(tag)
        return outstanding                 # 通知帧不代表本次轮询结束

    # 2) 非响应帧直接忽略
    if frame.frame_type != protocol.TYPE_RESPONSE:
        return outstanding

    # 3) 0x22 响应：本轮轮询成功（空盘），清除在途标记
    if frame.command == protocol.CMD_SINGLE_POLL:
        return None

    # 4) 0xFF 错误响应
    if frame.command == protocol.CMD_ERROR:
        error = protocol.parse_error(frame)
        if error is None:
            return None
        if error.code == 0x15:             # 无标签 / CRC 错误，视为空盘
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
```

要点：

- `0x15` 是“本轮没读到标签”，只增加“空盘次数”，不算通信错误。
- 其它错误码也结束本轮，并计入 `poll_errors`。
- 即使错误帧携带 EPC，也按标签回调处理（部分固件会这样返回）。

### 4.5 单个标签处理

```python
def _on_tag(self, tag: protocol.TagRead) -> None:
    self.tags += 1
    if tag.pc_mismatch:
        self._log_throttled("pc-mismatch",
                            f"EPC 长度与 PC 不一致（{tag.epc}），已按帧长度处理")
    self._buffer.add(tag.epc, tag.rssi)
    self._queue_tid_read(tag.epc)
    self._emit(self.on_tag, tag.epc, tag.rssi)
```

- EPC 统一转成**大写无空格十六进制字符串**作为后续所有环节的键。
- RSSI 按有符号字节处理（>127 减 256）。
- `pc_mismatch` 只影响日志，不丢数据：始终以帧内实际 EPC 长度为准。
- 采集只额外读取 TID（见 4.6），不读写 User 区。
- `on_tag` 回调经 Qt Signal 送到主线程刷新表格。

### 4.6 TID 读取与缓存（首次识别）

TID 只用于界面显示，**不进入 HTTP 上报**。策略是“首次识别时读一次并按 EPC 缓存”：

- 轮询第一次识别到某个唯一 EPC 时，把它放入待读队列。
- 在没有在途轮询时，执行 `0x0C Select` + `0x39 读 TID`（`MemBank=0x02`、`SA=0`）。
- TID 长度因标签而异：依次尝试 `DL=8/6/4/2 word`，取第一个成功的实际长度，不补齐。
- 读取失败（超时、`0xA3` 存储区越界、回读 EPC 不一致）→ 该 EPC 缓存空字符串，界面显示 `—`。
- 同一次采集运行内同一 EPC 只读一次；停止采集后缓存随 `Reader` 丢弃。
- 写入页成功改写 EPC 后，把 TID 缓存从旧 EPC 移到新 EPC。

协议常量与组帧（`app/protocol.py`）：

```python
MEM_BANK_TID = 0x02
TID_READ_WORDS = (8, 6, 4, 2)


def build_read_tid(words: int, access_password: int = 0) -> bytes:
    """4.7 读标签数据存储区：TID 区从 SA=0 开始。"""
    if words < 1 or words > 32:
        raise ValueError("读取长度必须为 1~32 word")
    param = (
        access_password.to_bytes(4, "big")
        + bytes([MEM_BANK_TID])
        + b"\x00\x00"
        + words.to_bytes(2, "big")
    )
    return build_frame(TYPE_COMMAND, CMD_READ, param)
```

缓存与读取（`app/reader.py`）：

```python
self._tid_cache: dict[str, str] = {}
self._tid_pending: deque[str] = deque()
self._tid_queued: set[str] = set()
self._tid_lock = threading.Lock()


def get_tid(self, epc: str) -> str:
    """已读取的 TID（大写 HEX，实际长度）；未读到或读取失败时为空字符串。"""
    with self._tid_lock:
        return self._tid_cache.get(epc, "")


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
```

轮询循环中的调度（在无在途轮询、且没有待执行写入任务时执行一次）：

```python
if outstanding is None:
    pending_tid = self._take_tid_read()
    if pending_tid is not None:
        self._read_tid(pending_tid)
        next_poll = time.monotonic() + POLL_INTERVAL
        continue
    self._write(protocol.SINGLE_POLL_CMD)
```

要点：

- 读 TID 放在轮询超时处理之后、发送下一条轮询之前，避免因“通知帧不清除在途标记”而一直得不到执行。
- `0xA3` 表示请求长度超过标签 TID 区容量，程序自动缩短长度重试；其它错误或超时直接放弃。
- 一次 TID 读取通常需要 1~2 条 `0x39` 命令（12 字节 TID 先读 8 word 失败，再读 6 word 成功）。

### 4.7 缓冲区（`app/buffer.py`）

```python
class TagBuffer:
    def __init__(self, capacity: int = 5000) -> None:
        self._capacity = capacity
        self._items: dict[str, int | None] = {}
        self._dropped = 0
        self._stopped = False
        self._revision = 0
        self._cond = threading.Condition()

    def add(self, epc: str, rssi: int | None) -> None:
        with self._cond:
            if epc in self._items:
                self._items[epc] = rssi              # 已存在：只更新 RSSI
            else:
                if len(self._items) >= self._capacity:
                    self._items.pop(next(iter(self._items)))  # 淘汰最早插入
                    self._dropped += 1
                self._items[epc] = rssi
                self._revision += 1                  # 只有“新唯一 EPC”才递增
            self._cond.notify()

    def take_batch(self, quiet: float = 0.0) -> list[tuple[str, int | None]] | None:
        with self._cond:
            while not self._items and not self._stopped:
                self._cond.wait()                    # 阻塞等待第一批数据
            if not self._items:
                return None                          # 已停止且无数据
            if quiet > 0:
                seen = self._revision
                deadline = time.monotonic() + quiet
                while not self._stopped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                    if self._revision != seen:       # 来了新的唯一 EPC
                        seen = self._revision
                        deadline = time.monotonic() + quiet
            batch = list(self._items.items())
            self._items.clear()
            return batch

    def requeue(self, batch: list[tuple[str, int | None]]) -> None:
        with self._cond:
            items = dict(batch)
            items.update(self._items)                # 已经在缓冲区里的更新数据优先
            if len(items) > self._capacity:
                extra = len(items) - self._capacity
                for _ in range(extra):
                    items.pop(next(iter(items)))
                    self._dropped += 1
            self._items = items
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
```

关键设计：

| 设计点 | 说明 |
|---|---|
| 按 EPC 去重 | `dict` 键即 EPC，同一批只保留一条，RSSI 取最新 |
| 重复不延长静默窗口 | `_revision` 只在新增键时 +1；重复读同一 EPC 不会重置 2 秒计时 |
| 上限保护 | 默认 5000，超出时淘汰最早插入项并计入 `dropped` |
| 线程安全 | 所有操作都在 `Condition` 内完成 |
| 回填合并 | 上传失败放回时与现有数据合并，`requeue` 的数据覆盖同 EPC |

---

## 5. 上报链路：缓冲区 → HTTP 服务器

### 5.1 启动与停止

- `Uploader` 在**握手成功后**由 `MainWindow._on_version()` 创建并启动，保证地址与设备 ID 已确定。
- 停止顺序：`Reader.stop()` → `Buffer.stop()` → `Uploader.stop()` → `join`。
- 停止时缓冲区剩余数据只记录日志，不再尝试发送。

### 5.2 取批与静默聚合

```python
def run(self) -> None:
    while True:
        batch = self._buffer.take_batch(AGGREGATE_WINDOW)   # 2.0s
        if batch is None:
            return
        payload = json.dumps(
            [epc for epc, _ in batch], ensure_ascii=False
        ).encode("utf-8")
        ok, retryable, error = self._post(payload)
        ...
```

`take_batch(2.0)` 的语义：

1. 缓冲区已有数据 → 开始计时。
2. 2 秒内每出现一个**新的唯一 EPC**，计时重置。
3. 2 秒内只有重复 EPC（已经在批内）→ 不重置。
4. 计时到 → 整批取走并清空。

因此：持续放在检测板上的标签会约每 2 秒形成一批并重复上报，MES 侧需具备幂等能力；本版本不做“标签离开才上报”的存在检测。

### 5.3 当前请求体（EPC 字符串数组）

```json
["30751FEB705C5904E3D50D70", "E2003412AABBCCDDEEFF0011"]
```

- 大写十六进制，无 `0x`、无空格。
- 批内 EPC 不重复。
- 空缓冲区不发请求。
- 不读取、不上报 User 区数据。

### 5.4 HTTP 请求细节

```python
request = urllib.request.Request(
    self._url,
    data=payload,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-Device-Id": self._device_id,     # 界面“设备 ID”，默认 RFID-PLATE-001
    },
)
with urllib.request.urlopen(request, timeout=self._timeout) as response:  # 10s
    status = response.status
    body = response.read()
```

### 5.5 成功判定

```python
if 200 <= status < 300:
    ok, error = self._parse_response(body)
```

`_parse_response` 规则：

- 响应体必须是 JSON **对象**，且含布尔字段 `ok`。
- `{"ok": true}` → 成功。
- `{"ok": false}` → 业务失败（丢弃该批，不重试）。
- 非 JSON / 缺少布尔 `ok` → 格式错误（丢弃该批，不重试）。

### 5.6 重试与回填矩阵

```python
def _post(self, payload: bytes) -> tuple[bool, bool, str]:
    """返回 (是否成功, 是否可重试, 错误说明)。"""
```

| 场景 | 是否重试 | 最终处理 |
|---|---|---|
| 网络异常（连接失败、超时、DNS 等） | 是 | 3 次后仍失败 → 整批回填缓冲区 |
| HTTP 5xx | 是 | 同上 |
| HTTP 4xx | 否 | 丢弃该批，只记失败 |
| 2xx + `{"ok": false}` | 否 | 丢弃该批 |
| 2xx + 非 JSON / 缺布尔 `ok` | 否 | 丢弃该批 |
| 程序正在停止 | 否 | 丢弃该批并记录剩余条数 |

重试间隔固定为 **0.5s / 1s / 2s**；单次请求超时 **10s**。

```python
RETRY_DELAYS = (0.5, 1.0, 2.0)
...
if attempt < self._attempts - 1:
    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
    if self._stop_event.wait(delay):
        return False, False, "已停止"
```

失败后的主循环处理：

```python
if not retryable:
    self._log(f"上传失败，丢弃 {len(batch)} 条：{error}", error=True)
else:
    self._buffer.requeue(batch)                       # 整批放回
    self._log(f"上传失败，{len(batch)} 条已放回缓冲区：{error}", error=True)
    self._stop_event.wait(REQUEUE_PAUSE)              # 1s 后再取
```

### 5.7 统计与日志

| 字段 | 含义 |
|---|---|
| `success_batches` | 成功批次数（界面“上传成功”） |
| `failed_batches` | 失败批次数（界面“上传失败”） |
| `sent_tags` | 成功发送的标签条数 |

成功/失败/回填/丢弃都会写入界面日志与文件日志。

---

## 6. 写入链路：界面 → 标签 EPC 区

> 写入目标是标签的 **EPC 存储区**（`MemBank=0x01`）：从 word1 写入 `PC + 新 EPC`，标签 EPC 被替换。
> CRC（word0）由标签在 EPC 写入后自动更新，程序不写。**本版本不读写 User 区。**

### 6.1 约束

| 项 | 规则 |
|---|---|
| 新 EPC 长度 | 2 ~ 30 字节，且必须为偶数（同时受 Select 掩码与 `0x49` 单次 32 word 上限约束） |
| 写入地址 | 固定 `SA=0x0001`（PC 所在 word） |
| PC | `(EPC 字数 << 11)`，其余标志位按 0 处理 |
| CRC | 标签自动更新，不发送写 CRC |
| Access Password | 固定 `00000000`，按手册不额外发送 Access 指令 |
| 并发 | 同一时刻只允许一个写入任务；写入期间按钮与标签表禁用 |
| 轮询 | 写入 + 回读期间暂停 100ms 轮询 |
| 回读 | 写后用**新 EPC** 重新 Select，再读 EPC 区 `SA=2` |
| 回读不一致 / 超时 | 直接失败，**不自动重写**（旧 EPC 可能已不存在） |
| 写错误 | 直接失败，不重试 |
| 成功后界面 | 写入页表格中该行 EPC 替换为新值；该行 TID 列保持不变（TID 是芯片唯一标识，不随 EPC 改变） |

### 6.2 数据校验（`prepare_epc_write`）

```python
def parse_hex_text(text: str) -> bytes:
    tokens = text.split()
    if not tokens:
        raise ValueError("请输入要写入的 HEX 数据")
    data = bytearray()
    for token in tokens:
        if len(token) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in token):
            raise ValueError("每个字节必须是两位十六进制，例如 12 34 AB")
        data.append(int(token, 16))
    if len(data) < 2 or len(data) > 64:
        raise ValueError("写入数据必须为 2~64 字节")
    return bytes(data)


def prepare_epc_write(text: str) -> bytes:
    """校验要写入 EPC 区的 EPC：2~30 字节且按 word 对齐。"""
    data = parse_hex_text(text)
    if len(data) > MAX_WRITE_EPC_LEN:
        raise ValueError("EPC 长度必须为 2~30 字节")
    if len(data) % 2:
        raise ValueError("EPC 必须按 word（2 字节）对齐")
    return data
```

界面在输入变化时实时校验，不合法直接禁用“写入”按钮，不发送任何串口指令。

### 6.3 写入任务提交与互斥

```python
def submit_epc_write(self, epc: str, data: bytes) -> bool:
    if self._stop_event.is_set() or not self.version:
        return False
    with self._write_lock:
        if self._write_job is not None or self.writing:
            return False
        self._write_job = (epc, data)
    return True
```

- 只有握手成功后才允许提交。
- 已有待执行任务或正在写入时拒绝第二次提交。
- 主循环在**没有在途轮询**时取出任务，设置 `self.writing = True`。

### 6.4 写入 + 回读状态机

```python
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
```

命令序列：

```text
正常：
  0x0C Select(旧 EPC)
  0x49 写 EPC 区（SA=1，DL=1+N/2，DT=PC + 新 EPC）
  0x0C Select(新 EPC)
  0x39 读 EPC 区（SA=2，DL=N/2）
  → 回读 EPC == 新 EPC 且回读数据 == 新 EPC → 成功

异常：
  Select 旧 EPC 失败 / 写错误 → 失败，不重试
  写成功但用新 EPC 无法选中 / 回读超时 / 回读不一致 → 失败，不自动重写
```

### 6.5 命令组帧

**设置 Select 参数（0x0C）**

```python
def build_select_params(epc: bytes) -> bytes:
    """4.5 设置 Select 参数：按完整 EPC 选中唯一标签。"""
    if len(epc) < 2 or len(epc) > MAX_EPC_MASK_LEN or len(epc) % 2:
        raise ValueError("EPC 长度必须为 2~30 字节的偶数")
    param = (
        b"\x01"                 # Target=000, Action=000, MemBank=EPC
        + b"\x00\x00\x00\x20"   # Ptr=32 bit，跳过 CRC 与 PC
        + bytes([len(epc) * 8]) # Mask 长度（bit）
        + b"\x00"               # Truncate=0
        + epc
    )
    return build_frame(TYPE_COMMAND, CMD_SELECT_PARAMS, param)
```

**写 EPC 区（0x49）**

```python
def build_write_epc(epc: bytes, access_password: int = 0) -> bytes:
    """4.8 写标签数据存储区：从 EPC 区 word1 写入 PC + 新 EPC。"""
    if len(epc) < 2 or len(epc) > MAX_WRITE_EPC_LEN or len(epc) % 2:
        raise ValueError("EPC 长度必须为 2~30 字节的偶数")
    pc = (len(epc) // 2) << 11
    data = pc.to_bytes(2, "big") + epc
    param = (
        access_password.to_bytes(4, "big")
        + bytes([MEM_BANK_EPC])            # 0x01
        + EPC_SA_PC.to_bytes(2, "big")     # SA = 1
        + (len(data) // 2).to_bytes(2, "big")
        + data
    )
    return build_frame(TYPE_COMMAND, CMD_WRITE, param)
```

**读 EPC 区（0x39）**

```python
def build_read_epc(words: int, access_password: int = 0) -> bytes:
    """4.7 读标签数据存储区：从 EPC 区 word2 读取 EPC，用于回读校验。"""
    if words < 1 or words > 32:
        raise ValueError("读取长度必须为 1~32 word")
    param = (
        access_password.to_bytes(4, "big")
        + bytes([MEM_BANK_EPC])
        + EPC_SA_DATA.to_bytes(2, "big")   # SA = 2
        + words.to_bytes(2, "big")
    )
    return build_frame(TYPE_COMMAND, CMD_READ, param)
```

**实际帧示例**

把 EPC = `30751FEB705C5904E3D50D70` 的标签改为 `E2003412AABBCCDD`（8 字节，PC=0x2000）：

```text
Select 旧：BB 00 0C 00 13 01 00 00 00 20 60 00 30 75 1F EB 70 5C 59 04 E3 D5 0D 70 AD 7E
Write：   BB 00 49 00 13 00 00 00 00 01 00 01 00 05 20 00 E2 00 34 12 AA BB CC DD B9 7E
Select 新：BB 00 0C 00 0F 01 00 00 00 20 40 00 E2 00 34 12 AA BB CC DD B2 7E
Read：    BB 00 39 00 09 00 00 00 00 01 00 02 00 04 49 7E
```

改为 12 字节 EPC `E2003412AABBCCDDEEFF0011`（PC=0x3000）时，写帧为：

```text
BB 00 49 00 17 00 00 00 00 01 00 01 00 07 30 00 E2 00 34 12 AA BB CC DD EE FF 00 11 CD 7E
```

### 6.6 命令收发与超时

```python
COMMAND_TIMEOUT = 1.0

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
```

要点：

- 每条命令独立等待 1.0 秒；超时返回 `None`。
- 等待期间收到的**通知帧会被忽略**（写入期间本身也不关注采集）。
- `0xFF` 错误响应也会立即返回，由调用方解析错误码。

### 6.7 错误码说明

| 错误码 | 含义 | 常见原因 |
|---|---|---|
| `0x09` | 读标签失败 | 新 EPC 未被选中、标签已离开 |
| `0x10` | 写标签失败 | 旧 EPC 未选中、标签不在场 |
| `0x16` | 访问密码错误 | 标签设置了 Access Password |
| `0x17` | 指令代码错误 | 固件不支持该命令 |
| `0xB3` | 存储区越界 | 新 EPC 超过标签 EPC 容量 |
| `0xB4` | 存储区已锁定 | EPC 区被 Lock |

错误信息会带中文说明与十六进制错误码显示在写入结果区。

### 6.8 轮询暂停与恢复

- 写入任务只在 `outstanding is None` 时执行，即没有等待中的轮询响应。
- 写入完成后 `next_poll = now + 100ms`，避免一次性补发多条轮询。
- 写入期间 `self.writing = True`，界面“写入”按钮与标签表禁用。
- 写入与回读期间到达的标签通知帧会被忽略，不进入缓冲区。

### 6.9 写入成功后的界面与数据

- 写入页表格中该行 EPC 原地替换为新值；当前选择同步更新；TID 列保持不变。
- 写入成功后 TID 缓存从旧 EPC 移到新 EPC，后续不会为新 EPC 重复读 TID。
- 采集页“最近标签”与缓冲区中已存在的旧 EPC 不做历史回改；轮询到新 EPC 后会作为新标签出现。
- 如果旧 EPC 已经上报过，MES 侧会先后看到旧 EPC 与新 EPC，需要业务侧自行处理。

---

## 7. 界面装配与线程模型

### 7.1 Qt Signal 桥接

工作线程不能直接操作 QWidget，统一通过 `Bridge`：

```python
class Bridge(QObject):
    tag = Signal(str, object)
    tid = Signal(str, str)
    version = Signal(str)
    handshake_failed = Signal(str)
    runtime_error = Signal(str)
    log = Signal(str)
    write_state = Signal(bool)
    write_result = Signal(object)
```

`Reader` / `Uploader` 的回调在构造时接到 `Bridge` 的 `emit`：

```python
self._reader = Reader(
    port, baud, self._buffer,
    on_tag=self._bridge.tag.emit,
    on_tid=self._bridge.tid.emit,
    on_version=self._bridge.version.emit,
    on_handshake_failed=self._bridge.handshake_failed.emit,
    on_runtime_error=self._bridge.runtime_error.emit,
    on_log=self._bridge.log.emit,
    on_write_state=self._bridge.write_state.emit,
    on_write_result=self._bridge.write_result.emit,
)
```

### 7.2 启动流程

```python
def _on_start(self) -> None:
    if self._reader is not None:
        return
    port = self.port_combo.currentData()
    if not port:
        QMessageBox.warning(...)
        return
    url = self.url_edit.text().strip()
    error = self._validate_url(url)
    if error:
        QMessageBox.warning(...)
        return
    baud = self.baud_combo.currentData()

    self._config.update({"port": port, "baud": baud, "url": url,
                         "device_id": self.device_edit.text().strip()})
    config.save_config(self._config)

    self._buffer = TagBuffer()
    self._reader = Reader(port, baud, self._buffer, ...)
    self.tag_table.setRowCount(0)
    self.unique_table.setRowCount(0)
    self.write_result.clear()
    self._set_running(True)
    self.state_value.setText("读取硬件版本…")
    self._reader.start()
```

握手成功后自动创建上传线程：

```python
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
```

### 7.3 停止流程

```python
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
```

注意顺序：先停采集（否则还会产生新标签），再停缓冲区，最后停上传。上传线程若正在 10 秒超时的请求中，最长等待 8 秒后放弃 join。

### 7.4 标签表与唯一标签表

- 采集页“最近标签”：`EPC / TID / RSSI / 时间`，每次读到标签插入首行，最多保留 `MAX_TAG_ROWS = 50` 行。
- 写入页“识别到的标签（唯一）”：`EPC / TID / RSSI / 最后识别`，按 EPC 原地更新 RSSI 与最后识别时间，不在重复时新增行。
- 写入页选择某行后，“当前选择”显示 EPC，输入合法才启用“写入”按钮。
- TID 读取完成后通过 `on_tid` Signal 送到主线程，原地更新两个表格中对应 EPC 的 TID 列；未读到显示 `—`。

```python
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
```

### 7.5 写入按钮使能

```python
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
```

### 7.6 提交写入与结果展示

```python
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
```

结果区显示：结果、说明、原 EPC、新 EPC、回读 EPC、回读数据；成功后替换表格中的 EPC。

```python
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
```

---

## 8. 配置与日志

### 8.1 配置

- 路径：`%LOCALAPPDATA%\JRRFID\config.json`
- 字段：

```json
{
  "port": "COM3",
  "baud": 115200,
  "url": "http://192.168.1.100:8080/api/epc",
  "device_id": "RFID-PLATE-001"
}
```

- 启动时读取；点击“开始采集”与关闭窗口时保存。
- 配置读取做了类型校验，非法字段回退默认值；保存失败只记日志，不阻塞运行。

### 8.2 日志

- 路径：`%LOCALAPPDATA%\JRRFID\logs\app-YYYYMMDD.log`
- 记录：握手结果、轮询超时（节流 1 秒）、轮询错误、上传结果、写入提交与结果、串口异常、配置异常。
- 界面日志最多保留 2000 行（`MAX_LOG_LINES`）。

---

## 9. 常量与错误码速查

### 9.1 采集 / 通信

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `POLL_INTERVAL` | 0.1s | `reader.py` | 轮询周期 |
| `HANDSHAKE_TIMEOUT` | 1.5s | `reader.py` | 单次握手等待 |
| `HANDSHAKE_RETRIES` | 3 | `reader.py` | 握手次数 |
| `POLL_TIMEOUT` | 0.25s | `reader.py` | 轮询在途超时 |
| `SERIAL_TIMEOUT` | 0.02s | `reader.py` | 串口读超时 |
| `COMMAND_TIMEOUT` | 1.0s | `reader.py` | 写入相关命令等待 |
| `MAX_PARAM_LEN` | 256 | `protocol.py` | 解帧合法性上限 |

### 9.2 缓冲 / 上报

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `capacity` | 5000 | `buffer.py` | 缓冲区上限 |
| `AGGREGATE_WINDOW` | 2.0s | `uploader.py` | 静默聚合窗口 |
| `HTTP_TIMEOUT` | 10.0s | `uploader.py` | 单次请求超时 |
| `ATTEMPTS` | 3 | `uploader.py` | 最大尝试次数 |
| `RETRY_DELAYS` | 0.5/1/2s | `uploader.py` | 重试间隔 |
| `REQUEUE_PAUSE` | 1.0s | `uploader.py` | 回填后暂停 |
| `DEFAULT_DEVICE_ID` | `RFID-PLATE-001` | `uploader.py` | 设备 ID 默认值 |

### 9.3 写入 / TID

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `MEM_BANK_EPC` | `0x01` | `protocol.py` | EPC 存储区 |
| `MEM_BANK_TID` | `0x02` | `protocol.py` | TID 存储区 |
| `EPC_SA_PC` | `0x0001` | `protocol.py` | PC 所在 word |
| `EPC_SA_DATA` | `0x0002` | `protocol.py` | EPC 数据起始 word |
| `MAX_WRITE_EPC_LEN` | 30 字节 | `protocol.py` | 新 EPC 上限 |
| `MAX_EPC_MASK_LEN` | 31 字节 | `protocol.py` | Select 掩码字节上限 |
| `TID_READ_WORDS` | `(8, 6, 4, 2)` | `protocol.py` | TID 自适应读取长度 |

### 9.4 界面

| 常量 | 值 | 说明 |
|---|---|---|
| `BAUD_RATES` | 9600 ~ 921600 | 可选波特率，默认 115200 |
| `MAX_TAG_ROWS` | 50 | 采集页最近标签行数 |
| `MAX_LOG_LINES` | 2000 | 界面日志行数 |
| `STATS_INTERVAL_MS` | 500 | 统计刷新周期 |

### 9.5 错误码

| 码 | 名称 | 说明 |
|---|---|---|
| `0x09` | 读标签失败 | 新 EPC 未被选中或标签已离开 |
| `0x10` | 写标签失败 | 旧 EPC 未被选中或标签不在场 |
| `0x15` | 轮询失败 | 无标签返回或 CRC 错误（空盘） |
| `0x16` | 访问密码错误 | 标签有 Access Password |
| `0x17` | 指令代码错误 | 固件不支持 |
| `0x20` | 跳频搜索超时 | 信道搜索超时 |
| `0xA3` | 存储区越界（读） | 读取长度超过 TID 区容量 |
| `0xB3` | 存储区越界 | 新 EPC 超过标签容量 |
| `0xB4` | 存储区锁定 | EPC 区被 Lock |

---

## 10. 完整时序

### 10.1 采集 → 上报

```text
用户点击“开始采集”
  │
  ├─ 校验串口/地址/波特率，保存配置
  ├─ 创建 TagBuffer、Reader，启动串口线程
  │
Reader 线程
  ├─ 打开串口（失败 → 弹窗）
  ├─ 0x03 读取硬件版本（1.5s × 3）
  ├─ 回调 on_version → 主线程创建并启动 Uploader
  └─ 每 100ms：
       ├─ 发 0x22 轮询
       ├─ 收到通知帧 → parse_tag → buffer.add → on_tag → 界面
       ├─ 收到 0x22 响应 → 本轮结束
       ├─ 收到 0x15 → 空盘 +1
       ├─ 首次识别新 EPC → 空闲时 Select + 读 TID（8/6/4/2 word）
       └─ 250ms 无响应 → 超时 +1，继续

Uploader 线程
  ├─ take_batch(2s)：新唯一 EPC 重置计时
  ├─ 构造 ["EPC1", "EPC2", ...]
  ├─ POST（Content-Type、X-Device-Id）
  ├─ 2xx + ok:true → 成功计数
  ├─ ok:false / 4xx / 非法 JSON → 丢弃 + 失败计数
  └─ 网络异常 / 5xx → 重试 3 次 → 仍失败回填 + 1s 后继续
```

### 10.2 写入标签 EPC

```text
用户选中标签 + 输入合法新 EPC
  │
  ├─ 界面实时校验（2~30 字节、偶数）
  └─ 点击“写入” → submit_epc_write(旧 EPC, 新 EPC)

Reader 轮询循环
  ├─ 等待当前轮询响应结束 / 250ms 超时
  ├─ 取出写入任务，writing = True，通知界面禁用
  ├─ 0x0C Select(旧 EPC)
  ├─ 0x49 写 EPC 区（SA=1，DL=1+N/2，DT=PC+新 EPC）
  ├─ 0x0C Select(新 EPC)
  ├─ 0x39 读 EPC 区（SA=2，DL=N/2）
  ├─ 回读 EPC 与数据都等于新 EPC？
  │    是 → 成功，界面替换该行 EPC
  │    否 → 失败，不重写
  ├─ writing = False，通知界面恢复
  └─ 100ms 后恢复轮询
```

---

## 11. 测试与验证

### 11.1 单元测试（43 个，全部通过）

```bat
python -m unittest discover -s tests -v
```

覆盖内容：

| 测试文件 | 覆盖点 |
|---|---|
| `tests/test_protocol.py` | 手册样例帧解析、拆包/粘包、垃圾字节、校验错误重同步、载荷含 `0x7E/0xBB`、错误帧携带 EPC、EPC 区写/读组帧、TID 读组帧、EPC 长度校验、2 秒静默聚合 |
| `tests/test_uploader.py` | 真实本地 HTTP 服务器：EPC 字符串数组、去重、`X-Device-Id`、`ok:false`/400 丢弃、5xx 重试、非法响应丢弃、失败回填 |
| `tests/test_writer.py` | 假串口 + EPC/TID 存储模型：EPC 写入成功、PC 重算、写错误、回读超时、回读不一致、写入期间暂停轮询、TID 自适应读取、TID 读取失败缓存空值 |

### 11.2 本地接收验证服务器

```bat
python verify\rfid_server.py 8000
```

- 页面：`http://127.0.0.1:8000/`
- 上报地址：`http://<本机IP>:8000/api/epc`
- 接受 `["EPC1", "EPC2"]` 字符串数组，与本工具一致。

### 11.3 TID 版验证服务器

```bat
python verify\rfid_server_TID.py 8000
```

- 兼容 `["EPC1", "EPC2"]` 和 `[{"epc": "...", "tid": "..."}]` 两种请求体。
- 页面增加 TID 列；当前工具只上报 EPC，TID 列显示 `—`。
- 原 `verify\rfid_server.py` 未改动。

### 11.4 界面冒烟

项目通过 offscreen 界面冒烟：假串口下完成采集、显示标签、写入 EPC 区、回读校验、配置持久化。

---

## 12. 已知边界与注意事项

1. **持续放置会重复上报**：标签一直在检测板上时约每 2 秒形成一批重复上报，MES 需按 EPC 幂等处理；本版不做“离开再上报”。
2. **写 EPC 不可撤销**：程序不做备份、不做撤销；界面仅文字提示。
3. **写 EPC 会改变标签身份**：成功后表格 EPC 替换；已上报的旧 EPC 与缓冲区中的旧记录不会回改，MES 可能先后收到新旧两个 EPC。
4. **PC 标志位按 0 处理**：程序只按新 EPC 字数计算 PC 高 5 位；若原标签 UMI/XI 等标志位为 1，会被清除。
5. **新 EPC 长度受标签容量限制**：超过标签 EPC 容量返回 `0xB3`；EPC 区被锁返回 `0xB4`。
6. **新 EPC 上限 30 字节**：受 Select 掩码字段（1 字节 bit 长度）与工具校验约束；EPC Gen2 理论上限为 62 字节，但本工具无法 Select 超过 30 字节的 EPC。
7. **CRC 由标签更新**：依据 EPC Gen2 6.3.2.1.2.1，标签在 EPC 区被写入后自动重算 StoredCRC；程序不写 CRC。
8. **回读使用新 EPC**：写后旧 EPC 已不存在，必须重新 Select 新 EPC；若回读失败，写入可能已经生效，程序只报“无法确认”。
9. **回读失败不自动重写**：避免在旧 EPC 不存在时反复写或误写其它标签。
10. **写入期间暂停采集**：Select/写/Select/读最多 4 条命令，按每条 1 秒超时估算最坏约 4 秒，期间不采集新标签。
11. **等待命令响应会忽略通知帧**：`_wait_response` 只关心目标命令的响应，写入期间到达的标签通知不会进入缓冲区。
12. **Access Password 固定 0**：有密码的标签会写失败（`0x16`）。
13. **缓冲区满时淘汰最早插入项**：极端高并发下会丢弃旧数据并累计 `dropped`，界面不显示该计数。
14. **单文件 exe**：PyInstaller onefile，运行时会有常驻子进程；强制结束需结束所有同名进程。
15. **HTTP 协议**：当前请求体是 EPC 字符串数组，与 `docs/RFID检测板对接协议.md` 一致；若 MES 改为对象格式，需要同步修改 `Uploader.run()`。
16. **TID 仅本地显示**：TID 不进入 HTTP 上报；如需上报，需同步修改 `Uploader` 与验证服务器。
17. **TID 读取有额外开销**：每个新 EPC 首次识别时多 1~2 条 `0x39` 命令（12 字节 TID 为 8 word 失败 + 6 word 成功），期间暂停一次轮询。
18. **TID 读取失败不重试**：超时、无 TID 区或回读 EPC 不一致时缓存空值，界面显示 `—`，后续不再重读。
19. **TID 长度按实际返回**：依次尝试 8/6/4/2 word，不补齐；超过 8 word（16 字节）的 TID 会被截断为 16 字节。

---

## 13. 关键代码索引

| 功能 | 文件 | 位置 |
|---|---|---|
| 帧格式 / 校验 / 组帧 | `app/protocol.py` | `checksum`、`build_frame` |
| 流式解帧 | `app/protocol.py` | `FrameParser.feed` |
| 标签解析 | `app/protocol.py` | `parse_tag` |
| 错误解析 | `app/protocol.py` | `parse_error` |
| 读响应解析 | `app/protocol.py` | `parse_read_data` |
| 写/Select 成功判定 | `app/protocol.py` | `parse_write_success`、`parse_select_success` |
| Select / 写 EPC / 读 EPC 组帧 | `app/protocol.py` | `build_select_params`、`build_write_epc`、`build_read_epc` |
| EPC 长度校验 | `app/protocol.py` | `parse_hex_text`、`prepare_epc_write` |
| 串口打开 / 握手 | `app/reader.py` | `Reader.run`、`_handshake` |
| 轮询循环 | `app/reader.py` | `_poll_loop` |
| 帧分发 | `app/reader.py` | `_handle_frame` |
| 标签入缓冲 | `app/reader.py` | `_on_tag` |
| TID 缓存与读取 | `app/reader.py` | `get_tid`、`_queue_tid_read`、`_read_tid`、`_set_tid` |
| TID 读组帧 | `app/protocol.py` | `build_read_tid` |
| 写入任务提交 | `app/reader.py` | `submit_epc_write` |
| 写入状态机 | `app/reader.py` | `_run_write_job` |
| 写入命令封装 | `app/reader.py` | `_select_mask`、`_write_epc`、`_read_epc` |
| 命令等待 | `app/reader.py` | `_command`、`_wait_response` |
| 缓冲区 | `app/buffer.py` | `TagBuffer` |
| 取批 / 上报 / 重试 | `app/uploader.py` | `Uploader.run`、`_post`、`_parse_response` |
| 界面装配 | `app/ui/main_window.py` | `_on_start`、`_on_version`、`_stop`、`_on_write` |
| 写入成功后替换 EPC 行 | `app/ui/main_window.py` | `_replace_epc_row` |
| TID 界面更新 | `app/ui/main_window.py` | `_on_tag`、`_on_tid`、`_record_unique_tag` |
| 配置 / 日志 | `app/config.py` | `load_config`、`save_config`、`setup_logging` |
| 本地验证服务器 | `verify/rfid_server.py` | `Handler.do_POST`（仅 EPC 字符串数组） |
| TID 版验证服务器 | `verify/rfid_server_TID.py` | `Handler.do_POST`（兼容 EPC 数组与对象数组） |

---

> 本文档与仓库代码同步维护；若修改上报格式、命令时序或常量，请同时更新本文件。
