"""
数据接入层（acquisition）
=========================

对应系统功能一：**设备实时数据接入**。

层内组成：

* :mod:`~maco.acquisition.drivers` —— Modbus TCP / OPC UA / S7 / MQTT / 仿真驱动
* :mod:`~maco.acquisition.buffer` —— 时序环形缓冲，为特征提取与效果评估供数
* :mod:`~maco.acquisition.cleansing` —— 五道数据清洗
* :mod:`~maco.acquisition.collector` —— 单设备采集器与多设备接入网关

对上游只暴露两样东西：``SampleFrame`` 数据帧与 ``TimeSeriesBuffer`` 缓冲，
下游任何模块都不直接接触协议细节。
"""

from .buffer import SeriesWindow, TimeSeriesBuffer
from .cleansing import CleansingReport, DataCleanser
from .collector import AcquisitionGateway, Collector
from .drivers import ProtocolDriver, RawReading

__all__ = [
    "TimeSeriesBuffer", "SeriesWindow", "DataCleanser", "CleansingReport",
    "Collector", "AcquisitionGateway", "ProtocolDriver", "RawReading",
]
