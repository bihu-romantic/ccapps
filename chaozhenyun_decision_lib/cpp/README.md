# C++ 排程计算内核

本目录实现 `chaozhenyun_scheduler` 命令行计算内核。当前生产化主流程由 Python 表驱动工具调用该内核；C++ 程序内部读取临时运行输入并输出临时计算结果，随后由 Python 导入 SQLite 运行表。

## 构建

```bash
cmake -S chaozhenyun_decision_lib/cpp -B chaozhenyun_decision_lib/cpp/build
cmake --build chaozhenyun_decision_lib/cpp/build
```

## 支持模式

| 模式 | 用途 |
| --- | --- |
| `mrp` | 不接入设备状态的 MRP 基准排程 |
| `mrp_device` | 接入设备状态与能力约束的 MRP 排程 |
| `ga` | GA 顺序优化后交回 MRP 约束解码 |
| `ga_device` | 接入设备状态的 GA+MRP 排程 |
| `reschedule_device` | 基于设备事件、上一版计划和当前时点执行重调度 |
| `backward` | 根据交期释放时间的 MRP 排程模式 |

## 与表驱动流程的关系

当前推荐入口不是直接手工生成常驻结果 CSV，而是使用：

```bash
python3 chaozhenyun_decision_lib/tools/schedule_monitor.py --case job_schedule --mode mrp --once
```

表驱动工具会执行：

```text
SQLite 输入表
  -> 临时运行输入
  -> chaozhenyun_scheduler
  -> 临时计算结果
  -> schedule_versions / schedule_tasks / schedule_problems / schedule_diffs
```

因此，C++ 输出文件只是内部适配文件；对外结果以 SQLite 表为准。

## 重调度规则

`reschedule_device` 模式读取上一版计划、设备事件和当前时点：

```text
end_seconds <= now_seconds
  -> confirmed

start_seconds < now_seconds < end_seconds
  -> running

其他任务
  -> planned
```

已确认和正在执行任务保持不变；受设备事件影响的后续任务重新排程；未受影响任务尽量保持原计划。

## 设备状态约束

设备状态通过 `device_resource_map` 映射到排产资源：

- `available=0` 或 `capacity_factor<=0`：对应资源不可用。
- `0<capacity_factor<1`：工序时间按 `原工时 / capacity_factor` 折算。
- 未映射设备状态的资源按原能力运行。

## GA 与 MRP 的关系

GA 模块只负责搜索订单顺序和候选资源选择。GA 输出不会直接成为最终计划，而是交回 MRP 约束解码器，由 MRP 继续校验物料、工艺前后关系、资源容量、设备状态和冻结任务。
