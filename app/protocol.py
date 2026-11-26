"""E310 系列读写器串口协议：组帧、流式解帧、响应解析。"""

from __future__ import annotations

from dataclasses import dataclass

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

MEM_BANK_USER = 0x03
MAX_EPC_MASK_LEN = 31

# 上报固定携带的 User 区长度（字节）与对应读取字数。
USER_DATA_LEN = 16
USER_DATA_WORDS = USER_DATA_LEN // 2
USER_DATA_ZERO = "00" * USER_DATA_LEN

# 协议中所有已定义帧的参数长度都远小于该值，用于从垃圾字节中重新同步。
MAX_PARAM_LEN = 256


def checksum(data: bytes) -> int:
    """从帧类型到最后一个参数字节的累加和，取最低字节。"""
    return sum(data) & 0xFF


def build_frame(frame_type: int, command: int, param: bytes = b"") -> bytes:
    length = len(param)
    body = bytes([frame_type, command, (length >> 8) & 0xFF, length & 0xFF]) + param
    return bytes([HEADER]) + body + bytes([checksum(body), END])


def hex_bytes(text: str) -> bytes:
    return bytes.fromhex(text)


# 获取硬件版本：BB 00 03 00 01 00 04 7E
HARDWARE_VERSION_CMD = build_frame(TYPE_COMMAND, CMD_INFO, b"\x00")
# 单次轮询：BB 00 22 00 00 22 7E
SINGLE_POLL_CMD = build_frame(TYPE_COMMAND, CMD_SINGLE_POLL)


def build_select_params(epc: bytes) -> bytes:
    """4.5 设置 Select 参数：按完整 EPC 选中唯一标签。"""
    if len(epc) < 2 or len(epc) > MAX_EPC_MASK_LEN or len(epc) % 2:
        raise ValueError("EPC 长度必须为 2~30 字节的偶数")
    param = (
        b"\x01"  # Target=000, Action=000, MemBank=EPC
        + b"\x00\x00\x00\x20"  # Ptr=32 bit，跳过 CRC 与 PC
        + bytes([len(epc) * 8])
        + b"\x00"
        + epc
    )
    return build_frame(TYPE_COMMAND, CMD_SELECT_PARAMS, param)


def build_write_user(data: bytes, access_password: int = 0) -> bytes:
    """4.8 写标签数据存储区：User 区从 SA=0 开始。"""
    if len(data) < 2 or len(data) > 64 or len(data) % 2:
        raise ValueError("写入数据必须为 2~64 字节且按 word 对齐")
    param = (
        access_password.to_bytes(4, "big")
        + bytes([MEM_BANK_USER])
        + b"\x00\x00"
        + (len(data) // 2).to_bytes(2, "big")
        + data
    )
    return build_frame(TYPE_COMMAND, CMD_WRITE, param)


def build_read_user(words: int, access_password: int = 0) -> bytes:
    """4.7 读标签数据存储区：User 区从 SA=0 开始。"""
    if words < 1 or words > 32:
        raise ValueError("读取长度必须为 1~32 word")
    param = (
        access_password.to_bytes(4, "big")
        + bytes([MEM_BANK_USER])
        + b"\x00\x00"
        + words.to_bytes(2, "big")
    )
    return build_frame(TYPE_COMMAND, CMD_READ, param)


@dataclass(frozen=True)
class Frame:
    frame_type: int
    command: int
    param: bytes
    raw: bytes


@dataclass(frozen=True)
class TagRead:
    epc: str
    rssi: int | None
    pc_mismatch: bool = False


@dataclass(frozen=True)
class ErrorRead:
    code: int
    epc: str | None = None
    pc_mismatch: bool = False


class FrameParser:
    """按 PL 长度切帧的流式解析器，不依赖帧尾搜索，载荷中的 0x7E/0xBB 不会误切。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(HEADER)
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 5:
                break
            length = (self._buf[3] << 8) | self._buf[4]
            if length > MAX_PARAM_LEN:
                del self._buf[0]
                continue
            total = 5 + length + 2
            if len(self._buf) < total:
                break
            raw = bytes(self._buf[:total])
            if raw[-1] != END or checksum(raw[1 : 5 + length]) != raw[5 + length]:
                del self._buf[0]
                continue
            frames.append(Frame(raw[1], raw[2], raw[5 : 5 + length], raw))
            del self._buf[:total]
        return frames


def parse_hardware_version(frame: Frame) -> str | None:
    """解析 4.1.2 响应帧，成功返回 ASCII 版本号，否则返回 None。"""
    if frame.frame_type != TYPE_RESPONSE or frame.command != CMD_INFO:
        return None
    if len(frame.param) < 1 or frame.param[0] != 0x00:
        return None
    return frame.param[1:].decode("ascii", errors="replace").strip()


def parse_tag(frame: Frame) -> TagRead | None:
    """解析 4.2.2 通知帧：RSSI(1) + PC(2) + EPC(PL-5) + CRC(2)。"""
    if frame.frame_type != TYPE_NOTIFICATION or frame.command != CMD_SINGLE_POLL:
        return None
    if len(frame.param) < 7:
        return None
    rssi = frame.param[0] - 256 if frame.param[0] > 127 else frame.param[0]
    pc = (frame.param[1] << 8) | frame.param[2]
    epc = frame.param[3:-2]
    if not epc:
        return None
    words = (pc >> 11) & 0x1F
    return TagRead(epc.hex().upper(), rssi, words * 2 != len(epc))


def parse_error(frame: Frame) -> ErrorRead | None:
    """解析 0xFF 失败响应帧，参数可能为“错误码 + PC + EPC”。"""
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


def parse_select_success(frame: Frame) -> bool:
    """0x0C 响应：参数第 1 字节为 0x00 表示设置成功。"""
    return (
        frame.frame_type == TYPE_RESPONSE
        and frame.command == CMD_SELECT_PARAMS
        and len(frame.param) >= 1
        and frame.param[0] == 0x00
    )


def parse_write_success(frame: Frame) -> bool:
    """0x49 响应：参数最后 1 字节为 0x00 表示写入成功。"""
    return (
        frame.frame_type == TYPE_RESPONSE
        and frame.command == CMD_WRITE
        and len(frame.param) >= 1
        and frame.param[-1] == 0x00
    )


def parse_read_data(frame: Frame) -> tuple[str, bytes] | None:
    """0x39 响应：UL + PC + EPC + Data，返回 (EPC 十六进制, 数据)。"""
    if frame.frame_type != TYPE_RESPONSE or frame.command != CMD_READ:
        return None
    if len(frame.param) < 4:
        return None
    ul = frame.param[0]
    if ul < 2 or len(frame.param) <= 1 + ul:
        return None
    epc = frame.param[3 : 1 + ul]
    data = frame.param[1 + ul :]
    if not epc or not data:
        return None
    return epc.hex().upper(), data


def parse_hex_text(text: str) -> bytes:
    """解析空格分隔的两位 HEX 字节，失败抛出带中文原因的 ValueError。"""
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


def prepare_data_write(text: str) -> bytes:
    """校验写入 User 区的数据：2~64 字节且按 word 对齐。"""
    data = parse_hex_text(text)
    if len(data) % 2:
        raise ValueError("写入数据必须按 word（2 字节）对齐")
    return data


ERROR_NAMES = {
    0x09: "读标签失败（无标签返回或 CRC 错误）",
    0x10: "写标签失败（标签不在场或 EPC 不匹配）",
    0x15: "轮询失败（无标签返回或 CRC 错误）",
    0x16: "访问密码错误",
    0x17: "命令帧中指令代码错误",
    0x20: "跳频搜索信道超时",
    0xB3: "存储区越界（Memory Overrun）",
    0xB4: "存储区已锁定（Memory Locked）",
}


def error_name(code: int) -> str:
    return ERROR_NAMES.get(code, f"未知错误码 0x{code:02X}")
