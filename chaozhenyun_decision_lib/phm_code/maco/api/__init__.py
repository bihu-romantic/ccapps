"""
接口层（api）
=============

基于标准库的轻量 REST 服务，把引擎的查询与操作能力暴露给上位系统、
监控大屏或运维人员，不引入任何 Web 框架依赖。
"""

from .server import ApiRouter, ApiServer, ApiService, build_router

__all__ = ["ApiServer", "ApiService", "ApiRouter", "build_router"]
