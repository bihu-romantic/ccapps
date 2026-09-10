"""
西门子 S7 驱动
==============

面向 S7-300/400/1200/1500 系列 PLC 的 S7comm 协议驱动。报文在 ISO-on-TCP
（RFC1006，TPKT + COTP）之上承载 S7 PDU，本模块自行完成三层封装：

    TPKT(4B) → COTP(3B~) → S7 Header(10B) → 参数区 + 数据区

测点地址写法（``SignalPoint.address``）沿用西门子习惯写法::

    "DB100.DBD0:real"     DB 块双字，浮点
    "DB100.DBW10:int"     DB 块字，整型
    "DB100.DBX4.2:bool"   DB 块位
    "MD20:real"           存储器双字
    "IW0:int"             输入字
    "QW4:int"             输出字

驱动同样把地址连续的测点合并成一次读请求，减少 PLC 通信负载。
"""

from __future__ import annotations

import re
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...core.errors import ConnectionFailure, ProtocolError, TimeoutExpired
from ...core.logging_setup import get_logger
from ...core.registry import DRIVER_REGISTRY
from ...core.types import SignalPoint
from .base import ProtocolDriver, RawReading

logger = get_logger("acquisition.s7")

#: 区域标识
AREA_DB = 0x84
AREA_MERKER = 0x83
AREA_INPUT = 0x81
AREA_OUTPUT = 0x82

#: 数据类型 -> 字节宽度
S7_TYPE_WIDTH: Dict[str, int] = {
    "bool": 1, "byte": 1, "int": 2, "word": 2, "dint": 4,
    "dword": 4, "real": 4, "lreal": 8,
}

_ADDR_DB_RE = re.compile(
    r"^DB(?P<db>\d+)\.DB(?P<kind>[XBWD])(?P<offset>\d+)(?:\.(?P<bit>[0-7]))?"
    r"(?::(?P<dtype>\w+))?$", re.I
)
_ADDR_MEM_RE = re.compile(
    r"^(?P<area>[MIQ])(?P<kind>[XBWD])(?P<offset>\d+)(?:\.(?P<bit>[0-7]))?"
    r"(?::(?P<dtype>\w+))?$", re.I
)

#: S7 返回码释义
S7_RETURN_CODES: Dict[int, str] = {
    0x00: "保留",
    0x01: "硬件错误",
    0x03: "对象访问被拒绝",
    0x05: "地址越界",
    0x06: "数据类型不支持",
    0x07: "数据类型不一致",
    0x0A: "对象不存在",
    0xFF: "成功",
}


@dataclass(frozen=True)
class S7Address:
    """解析后的 S7 地址。"""

    area: int
    db_number: int
    byte_offset: int
    bit_offset: int
    data_type: str

    @property
    def width(self) -> int:
        return S7_TYPE_WIDTH.get(self.data_type, 2)

    @property
    def is_bit(self) -> bool:
        return self.data_type == "bool"


def parse_s7_address(address: str) -> S7Address:
    """解析 S7 地址字符串。"""
    text = str(address).strip()
    kind_to_type = {"X": "bool", "B": "byte", "W": "int", "D": "dint"}

    m = _ADDR_DB_RE.match(text)
    if m:
        kind = m.group("kind").upper()
        dtype = (m.group("dtype") or kind_to_type[kind]).lower()
        if dtype not in S7_TYPE_WIDTH:
            raise ProtocolError(f"不支持的 S7 数据类型: {dtype}", {"address": address})
        return S7Address(
            area=AREA_DB,
            db_number=int(m.group("db")),
            byte_offset=int(m.group("offset")),
            bit_offset=int(m.group("bit") or 0),
            data_type=dtype,
        )

    m = _ADDR_MEM_RE.match(text)
    if m:
        area = {"M": AREA_MERKER, "I": AREA_INPUT, "Q": AREA_OUTPUT}[m.group("area").upper()]
        kind = m.group("kind").upper()
        dtype = (m.group("dtype") or kind_to_type[kind]).lower()
        if dtype not in S7_TYPE_WIDTH:
            raise ProtocolError(f"不支持的 S7 数据类型: {dtype}", {"address": address})
        return S7Address(
            area=area,
            db_number=0,
            byte_offset=int(m.group("offset")),
            bit_offset=int(m.group("bit") or 0),
            data_type=dtype,
        )

    raise ProtocolError(f"S7 地址格式非法: {address!r}", {"address": address})


def decode_s7(payload: bytes, addr: S7Address) -> float:
    """把 S7 数据区字节解码为数值（S7 一律大端）。"""
    width = addr.width
    if len(payload) < width:
        raise ProtocolError(
            f"S7 数据长度不足：需要 {width}，实际 {len(payload)}",
            {"data_type": addr.data_type},
        )
    chunk = payload[:width]
    if addr.data_type == "bool":
        return float((chunk[0] >> addr.bit_offset) & 1)
    fmt = {"byte": ">B", "int": ">h", "word": ">H",
           "dint": ">i", "dword": ">I", "real": ">f", "lreal": ">d"}[addr.data_type]
    return float(struct.unpack(fmt, chunk)[0])


def encode_s7(value: float, addr: S7Address) -> bytes:
    """把数值编码为 S7 数据区字节。"""
    if addr.data_type == "bool":
        return bytes([1 if value else 0])
    if addr.data_type in ("real", "lreal"):
        fmt = ">f" if addr.data_type == "real" else ">d"
        return struct.pack(fmt, float(value))
    fmt = {"byte": ">B", "int": ">h", "word": ">H",
           "dint": ">i", "dword": ">I"}[addr.data_type]
    return struct.pack(fmt, int(round(value)))


@dataclass
class _S7Block:
    """合并后的一次读请求。"""

    area: int
    db_number: int
    start: int
    length: int
    points: List[Tuple[SignalPoint, S7Address]]


@DRIVER_REGISTRY.decorator("s7")
class S7Driver(ProtocolDriver):
    """西门子 S7 PLC 驱动。

    :param options: ``host`` / ``port``(默认102) / ``rack`` / ``slot``
        / ``pdu_size``（协商后的最大 PDU，默认 480）
    """

    protocol = "s7"

    def __init__(self, name: str, options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, options)
        self._host = str(self.options.get("host", "127.0.0.1"))
        self._port = int(self.options.get("port", 102))
        self._rack = int(self.options.get("rack", 0))
        self._slot = int(self.options.get("slot", 1))
        self._pdu_size = int(self.options.get("pdu_size", 480))
        self._sock: Optional[socket.socket] = None
        self._pdu_ref = 0
        self._io_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接：TCP → COTP 连接请求 → S7 通信建立
    # ------------------------------------------------------------------
    def _do_connect(self) -> None:
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout_ms / 1000.0
            )
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = sock
        except OSError as exc:
            raise ConnectionFailure(
                f"连接 S7 PLC 失败: {exc}", {"host": self._host, "port": self._port}
            ) from exc
        self._cotp_connect()
        self._setup_communication()

    def _do_disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _cotp_connect(self) -> None:
        """发送 COTP 连接请求（CR TPDU）。"""
        # 源/目的 TSAP：目的 TSAP 由 rack/slot 计算
        dst_tsap = (self._rack << 5) | self._slot
        cotp = bytes([
            0x11,              # 长度
            0xE0,              # CR TPDU
            0x00, 0x00,        # 目的引用
            0x00, 0x01,        # 源引用
            0x00,              # 类别
            0xC0, 0x01, 0x0A,  # TPDU 大小参数 = 1024
            0xC1, 0x02, 0x01, 0x00,             # 源 TSAP
            0xC2, 0x02, 0x01, dst_tsap & 0xFF,  # 目的 TSAP
        ])
        response = self._send_tpkt(cotp)
        if len(response) < 2 or response[1] != 0xD0:
            raise ProtocolError("COTP 连接被拒绝", {"host": self._host})

    def _setup_communication(self) -> None:
        """S7 通信参数协商（Job 0xF0）。"""
        params = struct.pack(">BBHHH", 0xF0, 0x00, 0x0001, 0x0001, self._pdu_size)
        header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000,
                             self._next_ref(), len(params), 0)
        body = self._s7_transact(header + params)
        if len(body) >= 20:
            negotiated = struct.unpack(">H", body[18:20])[0]
            if 240 <= negotiated <= 4096:
                self._pdu_size = negotiated
        logger.debug("S7 协商 PDU 大小 = %d", self._pdu_size)

    def _next_ref(self) -> int:
        self._pdu_ref = (self._pdu_ref + 1) % 0xFFFF
        return self._pdu_ref or 1

    # ------------------------------------------------------------------
    # 报文收发
    # ------------------------------------------------------------------
    def _send_tpkt(self, cotp_payload: bytes) -> bytes:
        """封装 TPKT 头并收发。返回 COTP 部分。"""
        if self._sock is None:
            raise ConnectionFailure("S7 连接未建立", {"driver": self.name})
        total = 4 + len(cotp_payload)
        packet = struct.pack(">BBH", 0x03, 0x00, total) + cotp_payload
        with self._io_lock:
            try:
                self._sock.sendall(packet)
                head = self._recv_exact(4)
                _, _, length = struct.unpack(">BBH", head)
                if length < 4:
                    raise ProtocolError("TPKT 长度非法", {"length": length})
                return self._recv_exact(length - 4)
            except socket.timeout as exc:
                raise TimeoutExpired(f"S7 请求超时 {self._timeout_ms}ms",
                                     {"host": self._host}) from exc
            except OSError as exc:
                raise ConnectionFailure(f"S7 通信中断: {exc}", {"host": self._host}) from exc

    def _s7_transact(self, s7_payload: bytes) -> bytes:
        """在 COTP DT TPDU 中承载 S7 报文。返回 S7 部分。"""
        cotp = bytes([0x02, 0xF0, 0x80]) + s7_payload
        response = self._send_tpkt(cotp)
        if len(response) < 3:
            raise ProtocolError("COTP 响应过短", {"driver": self.name})
        body = response[3:]
        if len(body) < 10 or body[0] != 0x32:
            raise ProtocolError("S7 报文头非法", {"driver": self.name})
        rosctr = body[1]
        if rosctr == 0x03:  # Ack_Data，含错误类/错误码
            err_class = body[10]
            err_code = body[11]
            if err_class != 0:
                raise ProtocolError(
                    f"S7 返回错误 class=0x{err_class:02X} code=0x{err_code:02X}",
                    {"driver": self.name},
                )
        return body

    def _recv_exact(self, size: int) -> bytes:
        if size <= 0:
            return b""
        assert self._sock is not None
        chunks: List[bytes] = []
        remaining = size
        while remaining > 0:
            piece = self._sock.recv(remaining)
            if not piece:
                raise ConnectionFailure("S7 连接被对端关闭", {"driver": self.name})
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _do_read(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        parsed: List[Tuple[SignalPoint, S7Address]] = []
        out: List[RawReading] = []
        for p in points:
            try:
                parsed.append((p, parse_s7_address(p.address)))
            except ProtocolError as exc:
                out.append(RawReading(p.point_id, None, ok=False, error=str(exc)))
        for block in self._merge_blocks(parsed):
            data = self._read_block(block)
            for point, addr in block.points:
                offset = addr.byte_offset - block.start
                try:
                    value = decode_s7(data[offset:offset + addr.width], addr)
                    out.append(RawReading(point.point_id, value))
                except (IndexError, ProtocolError) as exc:
                    out.append(RawReading(point.point_id, None, ok=False, error=str(exc)))
        return out

    def _merge_blocks(
        self, parsed: Sequence[Tuple[SignalPoint, S7Address]]
    ) -> List[_S7Block]:
        """按 (区域, DB号) 分组，合并地址相邻的测点。"""
        groups: Dict[Tuple[int, int], List[Tuple[SignalPoint, S7Address]]] = {}
        for point, addr in parsed:
            groups.setdefault((addr.area, addr.db_number), []).append((point, addr))
        blocks: List[_S7Block] = []
        # 单次读的数据上限：PDU 减去协议开销
        max_len = max(self._pdu_size - 32, 32)
        for (area, db), items in groups.items():
            items.sort(key=lambda it: it[1].byte_offset)
            current: Optional[_S7Block] = None
            for point, addr in items:
                end = addr.byte_offset + addr.width
                if current is None:
                    current = _S7Block(area, db, addr.byte_offset, addr.width,
                                       [(point, addr)])
                    continue
                new_len = max(current.start + current.length, end) - current.start
                if new_len <= max_len:
                    current.length = new_len
                    current.points.append((point, addr))
                else:
                    blocks.append(current)
                    current = _S7Block(area, db, addr.byte_offset, addr.width,
                                       [(point, addr)])
            if current is not None:
                blocks.append(current)
        return blocks

    def _read_block(self, block: _S7Block) -> bytes:
        """执行一次 S7 变量读（Job 0x04）。"""
        # 参数区：功能码 + 项目数 + 项目规格
        item = struct.pack(
            ">BBBBHHBBB",
            0x12,               # 变量规格
            0x0A,               # 本项长度
            0x10,               # 语法 ID: S7ANY
            0x02,               # 传输类型: BYTE
            block.length,       # 长度
            block.db_number,    # DB 号
            block.area,         # 区域
            (block.start >> 13) & 0xFF,
            (block.start >> 5) & 0xFF,
        )
        item += bytes([(block.start << 3) & 0xFF])
        params = struct.pack(">BB", 0x04, 0x01) + item
        header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000,
                             self._next_ref(), len(params), 0)
        body = self._s7_transact(header + params)
        # 数据区从 S7 头(12B for Ack_Data) + 参数区 之后开始
        param_len = struct.unpack(">H", body[6:8])[0]
        data_start = 12 + param_len
        if len(body) < data_start + 4:
            raise ProtocolError("S7 读响应数据区过短", {"db": block.db_number})
        return_code = body[data_start]
        if return_code != 0xFF:
            raise ProtocolError(
                f"S7 读返回码 0x{return_code:02X}"
                f"（{S7_RETURN_CODES.get(return_code, '未定义')}）",
                {"db": block.db_number, "start": block.start},
            )
        data_len_bits = struct.unpack(">H", body[data_start + 2:data_start + 4])[0]
        data_len = data_len_bits // 8
        payload = body[data_start + 4:data_start + 4 + data_len]
        if len(payload) < block.length:
            raise ProtocolError(
                f"S7 读数据不足：期望 {block.length}，实际 {len(payload)}",
                {"db": block.db_number},
            )
        return payload

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def _do_write(self, point: SignalPoint, raw_value: float) -> bool:
        addr = parse_s7_address(point.address)
        payload = encode_s7(raw_value, addr)
        transport = 0x03 if addr.is_bit else 0x04  # BIT / BYTE
        bit_addr = (addr.byte_offset << 3) | (addr.bit_offset if addr.is_bit else 0)
        item = struct.pack(
            ">BBBBHHBBBB",
            0x12, 0x0A, 0x10,
            0x02 if not addr.is_bit else 0x01,
            len(payload) if not addr.is_bit else 1,
            addr.db_number,
            addr.area,
            (bit_addr >> 16) & 0xFF,
            (bit_addr >> 8) & 0xFF,
            bit_addr & 0xFF,
        )
        data = struct.pack(">BBH", 0x00, transport,
                           len(payload) * (8 if not addr.is_bit else 1)) + payload
        if len(data) % 2:
            data += b"\x00"
        params = struct.pack(">BB", 0x05, 0x01) + item
        header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000,
                             self._next_ref(), len(params), len(data))
        body = self._s7_transact(header + params + data)
        param_len = struct.unpack(">H", body[6:8])[0]
        data_start = 12 + param_len
        if len(body) <= data_start:
            raise ProtocolError("S7 写响应缺少返回码", {"point_id": point.point_id})
        return_code = body[data_start]
        if return_code != 0xFF:
            raise ProtocolError(
                f"S7 写返回码 0x{return_code:02X}"
                f"（{S7_RETURN_CODES.get(return_code, '未定义')}）",
                {"point_id": point.point_id},
            )
        return True

    def describe(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "host": self._host,
            "port": self._port,
            "rack": self._rack,
            "slot": self._slot,
            "pdu_size": self._pdu_size,
            "connected": self.connected,
        }


__all__ = ["S7Driver", "S7Address", "parse_s7_address", "decode_s7", "encode_s7"]
