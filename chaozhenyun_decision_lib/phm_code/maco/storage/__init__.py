"""
存储层（storage）
=================

以文件为介质的追加式仓储，不依赖数据库，便于在边缘网关部署。
决策记录按日分片、时序归档按小时分片、知识资产单独导出。
"""

from .repository import FileRepository

__all__ = ["FileRepository"]
