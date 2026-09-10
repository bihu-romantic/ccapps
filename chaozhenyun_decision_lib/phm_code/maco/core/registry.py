"""
组件与设备注册模块
==================

系统的可扩展性依赖两张注册表：

* **组件注册表**（:class:`ComponentRegistry`）——按名称注册驱动、算法、
  适配器实现类，运行期按配置字符串装配。新增一种 PLC 协议或一种寻优算法，
  只需实现接口并注册，无需改动引擎代码。
* **设备注册表**（:class:`DeviceRegistry`）——保存设备档案、测点表、
  可调参数表与铭牌参数，是数据接入层、健康层、控制层共同的元数据来源。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Iterable, List, Optional, Type, TypeVar

from .enums import DeviceType, SignalKind
from .errors import PointNotFound, RegistryError
from .logging_setup import get_logger
from .types import ParameterSpec, SignalPoint

logger = get_logger("core.registry")

T = TypeVar("T")


class ComponentRegistry(Generic[T]):
    """按名称注册实现类或工厂函数。"""

    def __init__(self, category: str):
        self._category = category
        self._items: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def register(self, name: str, factory: Any, override: bool = False) -> None:
        """注册一个实现。``factory`` 可以是类，也可以是可调用工厂。"""
        key = name.strip().lower()
        with self._lock:
            if key in self._items and not override:
                raise RegistryError(
                    f"{self._category} 中已存在同名实现: {name}",
                    {"category": self._category, "name": name},
                )
            self._items[key] = factory
        logger.debug("已注册 %s 实现: %s", self._category, name)

    def decorator(self, name: str) -> Callable[[Type[T]], Type[T]]:
        """装饰器写法::

            @DRIVER_REGISTRY.decorator("modbus_tcp")
            class ModbusTcpDriver(ProtocolDriver): ...
        """

        def wrap(cls: Type[T]) -> Type[T]:
            self.register(name, cls)
            return cls

        return wrap

    def get(self, name: str) -> Any:
        key = str(name).strip().lower()
        with self._lock:
            if key not in self._items:
                raise RegistryError(
                    f"{self._category} 中不存在实现: {name}（可选: {sorted(self._items)}）",
                    {"category": self._category, "name": name},
                )
            return self._items[key]

    def create(self, name: str, *args: Any, **kwargs: Any) -> T:
        """按名称实例化。"""
        factory = self.get(name)
        return factory(*args, **kwargs)

    def has(self, name: str) -> bool:
        with self._lock:
            return str(name).strip().lower() in self._items

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


#: 全局注册表实例
DRIVER_REGISTRY: ComponentRegistry[Any] = ComponentRegistry("数据接入驱动")
CONTROL_ADAPTER_REGISTRY: ComponentRegistry[Any] = ComponentRegistry("控制系统适配器")
OPTIMIZER_REGISTRY: ComponentRegistry[Any] = ComponentRegistry("寻优算法")
SURROGATE_REGISTRY: ComponentRegistry[Any] = ComponentRegistry("代理模型")


@dataclass
class DeviceProfile:
    """设备档案。汇总一台设备的全部元数据。"""

    device_id: str
    name: str
    device_type: DeviceType = DeviceType.GENERIC
    #: 所属产线/区域，用于多场景管理
    area: str = ""
    #: 铭牌参数：额定功率(kW)、额定转速(rpm)、额定电流(A) 等
    nameplate: Dict[str, float] = field(default_factory=dict)
    #: 测点表
    points: Dict[str, SignalPoint] = field(default_factory=dict)
    #: 可调运行参数表
    parameters: Dict[str, ParameterSpec] = field(default_factory=dict)
    #: 机械结构参数（轴承滚子数、齿轮齿数等），供机理特征计算
    mechanics: Dict[str, float] = field(default_factory=dict)
    #: 绑定的场景配置名
    scenario: str = ""
    enabled: bool = True
    remark: str = ""

    # -- 测点访问 ---------------------------------------------------------
    def add_point(self, point: SignalPoint) -> None:
        self.points[point.point_id] = point

    def point(self, point_id: str) -> SignalPoint:
        if point_id not in self.points:
            raise PointNotFound(
                f"设备 {self.device_id} 不存在测点 {point_id}",
                {"device_id": self.device_id, "point_id": point_id},
            )
        return self.points[point_id]

    def points_of_kind(self, kind: SignalKind) -> List[SignalPoint]:
        return [p for p in self.points.values() if p.kind is kind]

    def first_point_of(self, kind: SignalKind) -> Optional[SignalPoint]:
        pts = self.points_of_kind(kind)
        return pts[0] if pts else None

    def writable_points(self) -> List[SignalPoint]:
        return [p for p in self.points.values() if p.writable]

    # -- 参数访问 ---------------------------------------------------------
    def add_parameter(self, spec: ParameterSpec) -> None:
        self.parameters[spec.param_id] = spec

    def parameter(self, param_id: str) -> ParameterSpec:
        if param_id not in self.parameters:
            raise RegistryError(
                f"设备 {self.device_id} 不存在可调参数 {param_id}",
                {"device_id": self.device_id, "param_id": param_id},
            )
        return self.parameters[param_id]

    def parameter_list(self) -> List[ParameterSpec]:
        return list(self.parameters.values())

    def nameplate_of(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.nameplate.get(key, default))
        except (TypeError, ValueError):
            return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type.code,
            "device_type_label": self.device_type.label,
            "area": self.area,
            "scenario": self.scenario,
            "enabled": self.enabled,
            "nameplate": dict(self.nameplate),
            "mechanics": dict(self.mechanics),
            "point_count": len(self.points),
            "parameter_count": len(self.parameters),
            "points": [p.to_dict() for p in self.points.values()],
            "parameters": [s.to_dict() for s in self.parameters.values()],
            "remark": self.remark,
        }


class DeviceRegistry:
    """设备档案注册表。"""

    def __init__(self) -> None:
        self._devices: Dict[str, DeviceProfile] = {}
        self._lock = threading.RLock()

    def register(self, profile: DeviceProfile, override: bool = False) -> None:
        with self._lock:
            if profile.device_id in self._devices and not override:
                raise RegistryError(
                    f"设备已注册: {profile.device_id}",
                    {"device_id": profile.device_id},
                )
            self._devices[profile.device_id] = profile
        logger.info("已注册设备 %s（%s / %s）", profile.device_id, profile.name,
                    profile.device_type.label)

    def unregister(self, device_id: str) -> bool:
        with self._lock:
            return self._devices.pop(device_id, None) is not None

    def get(self, device_id: str) -> DeviceProfile:
        with self._lock:
            if device_id not in self._devices:
                raise RegistryError(f"未注册的设备: {device_id}", {"device_id": device_id})
            return self._devices[device_id]

    def find(self, device_id: str) -> Optional[DeviceProfile]:
        with self._lock:
            return self._devices.get(device_id)

    def all(self) -> List[DeviceProfile]:
        with self._lock:
            return list(self._devices.values())

    def enabled_devices(self) -> List[DeviceProfile]:
        return [d for d in self.all() if d.enabled]

    def by_type(self, device_type: DeviceType) -> List[DeviceProfile]:
        return [d for d in self.all() if d.device_type is device_type]

    def by_area(self, area: str) -> List[DeviceProfile]:
        return [d for d in self.all() if d.area == area]

    def resolve_point(self, device_id: str, point_id: str) -> SignalPoint:
        return self.get(device_id).point(point_id)

    def device_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._devices)

    def __len__(self) -> int:
        with self._lock:
            return len(self._devices)

    def __contains__(self, device_id: object) -> bool:
        with self._lock:
            return device_id in self._devices

    def to_dict(self) -> Dict[str, Any]:
        return {"count": len(self), "devices": [d.to_dict() for d in self.all()]}


__all__ = [
    "ComponentRegistry",
    "DeviceProfile",
    "DeviceRegistry",
    "DRIVER_REGISTRY",
    "CONTROL_ADAPTER_REGISTRY",
    "OPTIMIZER_REGISTRY",
    "SURROGATE_REGISTRY",
]
