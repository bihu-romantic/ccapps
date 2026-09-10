"""
Modbus TCP 驱动
===============

面向 PLC / 智能仪表最常见的 Modbus TCP 协议，自实现 MBAP 报文封装与
PDU 编解码，不依赖第三方 Modbus 库，便于裁剪与移植到嵌入式网关。

测点地址写法（``SignalPoint.address``）::

    "3:40001:float32:big"   功能码区:寄存器地址:数据类型:字节序
    "4:30010:int16"          可省略字节序，默认大端
    "1:00005:bool"           线圈

功能码区约定：
* ``1`` 线圈（读 0x01 / 写 0x05）
* ``2`` 离散输入（只读 0x02）
* ``3`` 保持寄存器（读 0x03 / 写 0x06、0x10）
* ``4`` 输入寄存器（只读 0x04）

驱动会把地址连续、类型一致的测点自动合并成一次批量读，
单次请求最多 125 个寄存器（协议上限）。
"""

from __future__ import annotations

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

logger = get_logger("acquisition.modbus")

#: 单次读取的寄存器数上限（Modbus 协议规定）
MAX_REGISTERS_PER_READ = 125
MAX_COILS_PER_READ = 2000

#: 异常码释义
EXCEPTION_CODES: Dict[int, str] = {
    0x01: "非法功能码",
    0x02: "非法数据地址",
    0x03: "非法数据值",
    0x04: "从站设备故障",
    0x05: "确认（长耗时操作已受理）",
    0x06: "从站设备忙",
    0x08: "存储奇偶校验错误",
    0x0A: "网关路径不可用",
    0x0B: "网关目标设备无响应",
}

#: 数据类型占用的寄存器数
TYPE_REGISTER_COUNT: Dict[str, int] = {
    "bool": 1, "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2, "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
}


@dataclass(frozen=True)
class ModbusAddress:
    """解析后的 Modbus 地址。"""

    area: int
    register: int
    data_type: str
    byte_order: str = "big"
    #: 32 位量的字序：``high_first`` 表示高字在前（大多数 PLC 默认）
    word_order: str = "high_first"

    @property
    def register_count(self) -> int:
        return TYPE_REGISTER_COUNT.get(self.data_type, 1)

    @property
    def is_coil(self) -> bool:
        return self.area in (1, 2)

    @property
    def read_only(self) -> bool:
        return self.area in (2, 4)


def parse_address(address: str) -> ModbusAddress:
    """解析测点地址字符串。"""
    parts = [p.strip() for p in str(address).split(":") if p.strip() != ""]
    if len(parts) < 2:
        raise ProtocolError(f"Modbus 地址格式非法: {address!r}", {"address": address})
    try:
        area = int(parts[0])
        register = int(parts[1])
    except ValueError as exc:
        raise ProtocolError(f"Modbus 地址不是整数: {address!r}", {"address": address}) from exc
    if area not in (1, 2, 3, 4):
        raise ProtocolError(f"未知的 Modbus 功能码区: {area}", {"address": address})
    data_type = (parts[2].lower() if len(parts) > 2 else
                 ("bool" if area in (1, 2) else "int16"))
    if data_type not in TYPE_REGISTER_COUNT:
        raise ProtocolError(f"不支持的数据类型: {data_type}", {"address": address})
    byte_order = parts[3].lower() if len(parts) > 3 else "big"
    word_order = parts[4].lower() if len(parts) > 4 else "high_first"
    # 兼容 40001 起始的传统编址写法，折算为协议偏移
    if register >= 40001 and area == 3:
        register -= 40001
    elif register >= 30001 and area == 4:
        register -= 30001
    elif register >= 10001 and area == 2:
        register -= 10001
    return ModbusAddress(area=area, register=register, data_type=data_type,
                         byte_order=byte_order, word_order=word_order)


def decode_registers(words: Sequence[int], addr: ModbusAddress) -> float:
    """把寄存器字数组解码为数值。"""
    count = addr.register_count
    if len(words) < count:
        raise ProtocolError(
            f"寄存器数量不足：需要 {count}，实际 {len(words)}",
            {"data_type": addr.data_type},
        )
    chunk = list(words[:count])
    if count > 1 and addr.word_order == "low_first":
        chunk.reverse()
    raw = b"".join(struct.pack(">H", w & 0xFFFF) for w in chunk)
    if addr.byte_order == "little":
        raw = bytes(reversed(raw))
    fmt = {
        "int16": ">h", "uint16": ">H",
        "int32": ">i", "uint32": ">I", "float32": ">f",
        "int64": ">q", "uint64": ">Q", "float64": ">d",
        "bool": ">H",
    }[addr.data_type]
    value = struct.unpack(fmt, raw)[0]
    return float(value)


def encode_value(value: float, addr: ModbusAddress) -> List[int]:
    """把数值编码为寄存器字数组。"""
    fmt = {
        "int16": ">h", "uint16": ">H",
        "int32": ">i", "uint32": ">I", "float32": ">f",
        "int64": ">q", "uint64": ">Q", "float64": ">d",
        "bool": ">H",
    }[addr.data_type]
    if addr.data_type in ("int16", "uint16", "int32", "uint32",
                          "int64", "uint64", "bool"):
        payload = struct.pack(fmt, int(round(value)))
    else:
        payload = struct.pack(fmt, float(value))
    if addr.byte_order == "little":
        payload = bytes(reversed(payload))
    words = [struct.unpack(">H", payload[i:i + 2])[0] for i in range(0, len(payload), 2)]
    if len(words) > 1 and addr.word_order == "low_first":
        words.reverse()
    return words


@dataclass
class _ReadBlock:
    """一次批量读的合并块。"""

    area: int
    start: int
    count: int
    points: List[Tuple[SignalPoint, ModbusAddress]]


@DRIVER_REGISTRY.decorator("modbus_tcp")
class ModbusTcpDriver(ProtocolDriver):
    """Modbus TCP 客户端驱动。

    :param options: 支持 ``host`` / ``port`` / ``unit_id`` / ``timeout_ms``
        / ``max_gap``（合并读时允许跨过的最大空洞寄存器数）
    """

    protocol = "modbus_tcp"

    def __init__(self, name: str, options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, options)
        self._host: str = str(self.options.get("host", "127.0.0.1"))
        self._port: int = int(self.options.get("port", 502))
        self._unit_id: int = int(self.options.get("unit_id", 1))
        self._max_gap: int = int(self.options.get("max_gap", 8))
        self._sock: Optional[socket.socket] = None
        self._tid: int = 0
        self._io_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接
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
                f"连接 Modbus 从站失败: {exc}",
                {"host": self._host, "port": self._port},
            ) from exc

    def _do_disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ------------------------------------------------------------------
    # 报文收发
    # ------------------------------------------------------------------
    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) % 0xFFFF
        return self._tid or 1

    def _transact(self, pdu: bytes) -> bytes:
        """发送 PDU 并接收响应 PDU（去掉 MBAP 头）。"""
        if self._sock is None:
            raise ConnectionFailure("Modbus 连接未建立", {"driver": self.name})
        tid = self._next_tid()
        # MBAP 头：事务号(2) 协议号(2) 长度(2) 单元号(1)
        header = struct.pack(">HHHB", tid, 0, len(pdu) + 1, self._unit_id)
        with self._io_lock:
            try:
                self._sock.sendall(header + pdu)
                resp_header = self._recv_exact(7)
                r_tid, proto, length, unit = struct.unpack(">HHHB", resp_header)
                if proto != 0:
                    raise ProtocolError(f"非 Modbus 协议标识: {proto}", {"tid": r_tid})
                if r_tid != tid:
                    raise ProtocolError(
                        f"事务号不匹配：期望 {tid}，收到 {r_tid}", {"driver": self.name}
                    )
                body_len = max(length - 1, 0)
                body = self._recv_exact(body_len)
            except socket.timeout as exc:
                raise TimeoutExpired(
                    f"Modbus 请求超时 {self._timeout_ms}ms",
                    {"host": self._host, "port": self._port},
                ) from exc
            except OSError as exc:
                raise ConnectionFailure(f"Modbus 通信中断: {exc}",
                                        {"host": self._host}) from exc
        if not body:
            raise ProtocolError("Modbus 响应为空", {"driver": self.name})
        function = body[0]
        if function & 0x80:
            code = body[1] if len(body) > 1 else 0
            raise ProtocolError(
                f"从站返回异常码 0x{code:02X}（{EXCEPTION_CODES.get(code, '未定义')}）",
                {"function": function & 0x7F, "exception_code": code},
            )
        return body

    def _recv_exact(self, size: int) -> bytes:
        """精确接收 ``size`` 字节，处理 TCP 粘包/拆包。"""
        if size <= 0:
            return b""
        assert self._sock is not None
        chunks: List[bytes] = []
        remaining = size
        while remaining > 0:
            piece = self._sock.recv(remaining)
            if not piece:
                raise ConnectionFailure("Modbus 连接被对端关闭", {"driver": self.name})
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _do_read(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        parsed: List[Tuple[SignalPoint, ModbusAddress]] = []
        failures: List[RawReading] = []
        for p in points:
            try:
                parsed.append((p, parse_address(p.address)))
            except ProtocolError as exc:
                failures.append(RawReading(p.point_id, None, ok=False, error=str(exc)))
        blocks = self._merge_blocks(parsed)
        results: Dict[str, RawReading] = {}
        for block in blocks:
            words = self._read_block(block)
            for point, addr in block.points:
                offset = addr.register - block.start
                try:
                    if addr.is_coil:
                        value = float(words[offset])
                    else:
                        value = decode_registers(words[offset:offset + addr.register_count], addr)
                    results[point.point_id] = RawReading(point.point_id, value)
                except (IndexError, ProtocolError) as exc:
                    results[point.point_id] = RawReading(
                        point.point_id, None, ok=False, error=str(exc)
                    )
        out = [results.get(p.point_id,
                           RawReading(p.point_id, None, ok=False, error="未返回数据"))
               for p, _ in parsed]
        return out + failures

    def _merge_blocks(
        self, parsed: Sequence[Tuple[SignalPoint, ModbusAddress]]
    ) -> List[_ReadBlock]:
        """把地址相邻的测点合并成尽量少的读请求。"""
        blocks: List[_ReadBlock] = []
        by_area: Dict[int, List[Tuple[SignalPoint, ModbusAddress]]] = {}
        for point, addr in parsed:
            by_area.setdefault(addr.area, []).append((point, addr))
        for area, items in by_area.items():
            items.sort(key=lambda it: it[1].register)
            limit = MAX_COILS_PER_READ if area in (1, 2) else MAX_REGISTERS_PER_READ
            current: Optional[_ReadBlock] = None
            for point, addr in items:
                end = addr.register + addr.register_count
                if current is None:
                    current = _ReadBlock(area, addr.register, addr.register_count,
                                         [(point, addr)])
                    continue
                block_end = current.start + current.count
                gap = addr.register - block_end
                new_count = max(block_end, end) - current.start
                if gap <= self._max_gap and new_count <= limit:
                    current.count = new_count
                    current.points.append((point, addr))
                else:
                    blocks.append(current)
                    current = _ReadBlock(area, addr.register, addr.register_count,
                                         [(point, addr)])
            if current is not None:
                blocks.append(current)
        return blocks

    def _read_block(self, block: _ReadBlock) -> List[int]:
        """执行一次批量读，返回按寄存器/线圈展开的数组。"""
        function = {1: 0x01, 2: 0x02, 3: 0x03, 4: 0x04}[block.area]
        pdu = struct.pack(">BHH", function, block.start, block.count)
        body = self._transact(pdu)
        byte_count = body[1] if len(body) > 1 else 0
        payload = body[2:2 + byte_count]
        if block.area in (1, 2):
            bits: List[int] = []
            for byte in payload:
                for bit in range(8):
                    bits.append((byte >> bit) & 1)
            return bits[:block.count]
        if len(payload) < block.count * 2:
            raise ProtocolError(
                f"寄存器响应长度不足：期望 {block.count * 2}，实际 {len(payload)}",
                {"start": block.start, "count": block.count},
            )
        return [struct.unpack(">H", payload[i:i + 2])[0]
                for i in range(0, block.count * 2, 2)]

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def _do_write(self, point: SignalPoint, raw_value: float) -> bool:
        addr = parse_address(point.address)
        if addr.read_only:
            raise ProtocolError(
                f"测点 {point.point_id} 位于只读区（功能码区 {addr.area}）",
                {"point_id": point.point_id},
            )
        if addr.is_coil:
            state = 0xFF00 if raw_value else 0x0000
            pdu = struct.pack(">BHH", 0x05, addr.register, state)
            body = self._transact(pdu)
            return len(body) >= 5
        words = encode_value(raw_value, addr)
        if len(words) == 1:
            pdu = struct.pack(">BHH", 0x06, addr.register, words[0] & 0xFFFF)
            body = self._transact(pdu)
            return len(body) >= 5
        # 多寄存器写（0x10）
        payload = b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
        pdu = struct.pack(">BHHB", 0x10, addr.register, len(words), len(payload)) + payload
        body = self._transact(pdu)
        return len(body) >= 5

    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """连接描述，供接口层展示（不含任何凭据）。"""
        return {
            "protocol": self.protocol,
            "host": self._host,
            "port": self._port,
            "unit_id": self._unit_id,
            "max_gap": self._max_gap,
            "connected": self.connected,
        }


__all__ = [
    "ModbusTcpDriver",
    "ModbusAddress",
    "parse_address",
    "decode_registers",
    "encode_value",
    "EXCEPTION_CODES",
]
