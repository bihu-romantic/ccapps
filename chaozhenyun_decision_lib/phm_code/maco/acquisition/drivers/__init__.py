"""
协议驱动包
==========

导入本包即完成全部内置驱动的注册（注册通过装饰器在模块导入时生效）。
可用驱动名：``modbus_tcp`` / ``opcua`` / ``s7`` / ``mqtt`` / ``simulator``。
"""

from .base import DriverStats, ProtocolDriver, RawReading
from .modbus_tcp import ModbusTcpDriver
from .mqtt import LoopbackMqttTransport, MqttDriver, MqttTransport
from .opcua import InMemoryUaSession, OpcUaDriver, UaSession
from .s7 import S7Driver
from .simulator import SignalSource, SimulatorDriver

__all__ = [
    "ProtocolDriver", "RawReading", "DriverStats",
    "ModbusTcpDriver", "OpcUaDriver", "S7Driver", "MqttDriver", "SimulatorDriver",
    "UaSession", "InMemoryUaSession", "MqttTransport", "LoopbackMqttTransport",
    "SignalSource",
]
