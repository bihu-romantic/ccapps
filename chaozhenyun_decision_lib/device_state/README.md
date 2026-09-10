# 设备状态与能力画像模块

本模块用于把设备运行特征转换为设备状态、健康等级、可用性、能力系数和可用时间窗口。当前代码提供基础算法能力，主要供设备状态写表和排程资源能力约束使用。

## 主要文件

```text
analysis.py   工况识别、健康评估、时域/频域特征计算
models.py     设备状态、能力和时间窗口数据结构
profiler.py   设备状态画像生成逻辑
```

## 处理逻辑

```text
设备运行特征
  -> 工况识别
  -> 健康评估
  -> 状态推导
  -> 能力系数 capacity_factor
  -> 可用性 available
  -> 设备能力画像
```

排程联动时，设备状态通过运行表写入：

```text
device_state_updates
  -> schedule_monitor.py
  -> device_state_history
  -> reschedule_events
  -> 重调度
```

排程侧主要使用以下字段：

- `device_id`：设备编号。
- `status`：设备状态。
- `available`：是否可用。
- `capacity_factor`：有效能力系数。
- `risk_level`：风险等级。

当 `available=0` 或 `capacity_factor<=0` 时，对应排产资源不可用；当 `0<capacity_factor<1` 时，工序时间按 `原工时 / capacity_factor` 折算。
