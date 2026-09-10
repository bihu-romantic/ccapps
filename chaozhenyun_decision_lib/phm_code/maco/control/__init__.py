"""
控制层（control）
=================

对应系统功能四：**与 PLC / SCADA / DCS 的闭环联动**。

* :mod:`~maco.control.adapters` —— 三类控制系统的写通道适配器
* :mod:`~maco.control.safety` —— 写前七道安全检查与互锁
* :mod:`~maco.control.executor` —— 指令执行、回读校验、整体回滚与闭环状态机
"""

from .adapters import (
    ControlAdapter,
    DcsAdapter,
    PlcAdapter,
    ScadaAdapter,
    WriteOutcome,
    build_adapter,
)
from .executor import ClosedLoopController, CommandExecutor, LoopContext
from .safety import Interlock, SafetyGate, SafetyVerdict, default_interlocks

__all__ = [
    "ControlAdapter", "PlcAdapter", "ScadaAdapter", "DcsAdapter",
    "WriteOutcome", "build_adapter", "SafetyGate", "SafetyVerdict",
    "Interlock", "default_interlocks", "CommandExecutor",
    "ClosedLoopController", "LoopContext",
]
