# 超真云·工业系统资源智能决策算法库 V1.0

本目录为工业资源智能决策算法库源码。当前主流程采用 SQLite 运行表组织数据，不依赖 API 服务。初始案例数据可以从 CSV 导入，但计算过程、设备状态更新、重调度事件、排程版本、任务结果、问题诊断和计划差异均写入数据库表。

## 目录结构

```text
cpp/             C++ MRP、GA 和重调度计算内核
device_state/    设备状态识别、健康评估与能力画像模块
case_data/       车间调度和设备状态案例初始输入
tools/           表驱动运行、状态监控、甘特图和检查工具
tests/           回归测试
```

## 当前主流程

```text
初始 CSV 输入
  -> 导入 SQLite 输入表
  -> 生成初始排程版本
  -> 设备状态写入 device_state_updates
  -> schedule_monitor 周期扫描
  -> 状态变化生成 reschedule_events
  -> 按写表时间划分 confirmed / running / planned
  -> 调用 C++ reschedule_device 重调度
  -> 结果写入 schedule_versions / schedule_tasks / schedule_problems / schedule_diffs
  -> 从 schedule_tasks 生成 table_gantt.html
```

## 主要工具

| 工具 | 用途 |
| --- | --- |
| `tools/table_reschedule_workflow.py` | 创建运行表、导入初始输入、执行一次表驱动排程与重调度 |
| `tools/schedule_monitor.py` | 周期扫描设备状态更新表，发现变化后触发重调度 |
| `tools/write_device_update.py` | 写入一条设备状态更新，模拟外部设备采集系统写表 |
| `tools/inspect_runtime_tables.py` | 查看输入表、设备状态、事件、计划版本、任务状态和差异 |
| `tools/build_table_gantt.py` | 从数据库排程任务表生成 HTML 甘特图 |
| `tools/run_local_workflow.sh` | 本地一键运行表驱动流程 |

## 一键运行

```bash
bash chaozhenyun_decision_lib/tools/run_local_workflow.sh job_schedule
```

该脚本会执行：

1. 初始化 SQLite 运行库并导入初始案例数据。
2. 生成初始 `mrp_device` 排程版本。
3. 写入一条设备状态更新。
4. 运行状态监控任务处理该更新。
5. 如设备状态变化，触发重调度。
6. 生成数据库甘特图。
7. 输出运行表检查结果。

## 分步运行

初始化并生成基准计划：

```bash
python3 chaozhenyun_decision_lib/tools/schedule_monitor.py \
  --case job_schedule \
  --mode mrp \
  --reset \
  --once
```

写入设备状态更新：

```bash
python3 chaozhenyun_decision_lib/tools/write_device_update.py \
  --case job_schedule \
  --device-id FAN-01 \
  --status fault \
  --available 0 \
  --capacity-factor 0 \
  --risk-level high \
  --write-seconds 28800
```

处理一次设备状态更新：

```bash
python3 chaozhenyun_decision_lib/tools/schedule_monitor.py \
  --case job_schedule \
  --mode mrp \
  --once
```

长期 5 分钟周期扫描：

```bash
python3 chaozhenyun_decision_lib/tools/schedule_monitor.py \
  --case job_schedule \
  --mode mrp \
  --interval-seconds 300
```

查看运行表：

```bash
python3 chaozhenyun_decision_lib/tools/inspect_runtime_tables.py --case job_schedule
```

生成甘特图：

```bash
python3 chaozhenyun_decision_lib/tools/build_table_gantt.py --case job_schedule
```

生成文件：

```text
case_data/job_schedule/table_gantt.html
```

## 运行表

基础输入表：

- `case_planning_dates`
- `case_demands`
- `case_resources`
- `case_operations`
- `case_operation_resources`
- `case_operation_materials`
- `case_inventory`
- `case_purchasable_items`
- `case_routes`
- `case_operation_alternatives`
- `case_resource_calendars`

设备状态与事件表：

- `device_resource_map`
- `device_state_updates`
- `device_state_history`
- `reschedule_events`

排程结果表：

- `schedule_versions`
- `schedule_tasks`
- `schedule_problems`
- `schedule_diffs`

## 回归验证

```bash
python3 -m pytest -q chaozhenyun_decision_lib/tests
```
