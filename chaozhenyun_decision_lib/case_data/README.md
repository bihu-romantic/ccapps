# 案例数据说明

本目录保存算法库的初始输入数据。当前主流程会先将初始 CSV 导入 SQLite 运行表，然后基于表数据计算。CSV 是初始输入来源，不作为中间结果和最终结果主通道。

## 车间调度案例

目录：

```text
case_data/job_schedule
```

主要初始输入文件：

| 文件 | 导入表 | 说明 |
| --- | --- | --- |
| `planning_dates.csv` | `case_planning_dates` | 计划起点和默认交期 |
| `demands.csv` | `case_demands` | 订单、物料、数量、优先级 |
| `resources.csv` | `case_resources` | 资源编号和容量 |
| `operations.csv` | `case_operations` | 工序、产出物料、工时和批量规则 |
| `operation_resources.csv` | `case_operation_resources` | 工序和资源占用关系 |
| `operation_materials.csv` | `case_operation_materials` | 工序物料投入和产出 |
| `inventory.csv` | `case_inventory` | 期初库存 |
| `purchasable_items.csv` | `case_purchasable_items` | 可采购物料和提前期 |
| `routes.csv` | `case_routes` | 产品工艺路线 |
| `operation_alternatives.csv` | `case_operation_alternatives` | 替代工序 |
| `resource_calendars.csv` | `case_resource_calendars` | 资源日历 |
| `device_resource_map.csv` | `device_resource_map` | 设备和排产资源映射 |
| `device_states.csv` | `device_state_history` | 初始设备状态 |

设备映射示例：

```csv
device_id,resource_id
FAN-01,M056
AC-01,M057
```

设备状态示例：

```csv
device_id,status,available,capacity_factor,risk_level
FAN-01,degraded,1,0.65,medium
AC-01,normal,1,0.85,low
```

## 设备状态案例

目录：

```text
case_data/device_state_case
```

该目录保留设备状态模块的样例输入，用于验证 `device_state` 包中的状态识别和能力画像算法。排程主流程通过 `device_state_updates` 表接收设备状态更新。

## 结果保存位置

当前主流程结果保存在：

```text
case_data/job_schedule/planning_runtime.sqlite
```

主要结果表：

- `schedule_versions`
- `schedule_tasks`
- `schedule_problems`
- `schedule_diffs`
- `device_state_history`
- `reschedule_events`

甘特图文件：

```text
case_data/job_schedule/table_gantt.html
```
