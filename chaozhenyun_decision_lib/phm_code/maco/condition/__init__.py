"""
工况层（condition）
===================

"多工况自适应" 的实现基础：先判断设备当前处于哪个工况、是否稳态，
后续的健康评估与参数寻优都在该工况的上下文中进行。
"""

from .recognizer import (
    DEFAULT_SIGNATURE_KEYS,
    ConditionLibrary,
    ConditionProfile,
    ConditionRecognizer,
)

__all__ = ["ConditionRecognizer", "ConditionLibrary", "ConditionProfile",
           "DEFAULT_SIGNATURE_KEYS"]
