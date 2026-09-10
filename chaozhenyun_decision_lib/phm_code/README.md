# 多工况自适应工业设备控制优化系统 V1.0

**MACO** — Multi-condition Adaptive Control Optimization for Industrial Equipment

面向工业设备的知识驱动自主控制决策软件。实时接入设备运行数据，自动识别当前工况，
在该工况下评估设备健康状态，结合机理知识、规则推理与历史案例生成运行参数优化建议，
经安全校验后与 PLC / SCADA / DCS 闭环联动，并对执行效果做统计评估、把经验回写知识库，
形成持续自我改进的闭环。

---

## 五项核心功能

| # | 功能 | 实现位置 |
|---|---|---|
| 1 | 设备实时数据接入（温度/振动/电流/转速/能耗） | `maco/acquisition/` |
| 2 | 状态识别与健康评估 | `maco/condition/` + `maco/health/` |
| 3 | 运行参数优化建议 | `maco/optimization/` + `maco/knowledge/` |
| 4 | 与 PLC / SCADA / DCS 闭环联动 | `maco/control/` |
| 5 | 执行效果反馈学习 | `maco/feedback/` |

## 快速开始

```bash
pip install -r requirements.txt

# 全仿真演示，无需任何现场设备
python examples/run_demo.py

# 命令行
python -m maco.cli demo --rounds 15
python -m maco.cli scenarios
python -m maco.cli verify-audit
```

代码方式：

```python
from maco.bootstrap import build_demo_system

engine, sims = build_demo_system()
result = engine.decision_cycle("FAN-01")
print(result.summary())
print(result.advice.explanation)
```

## 目录结构

```
maco/
├── core/            内核：类型、配置、事件总线、调度、注册、审计
├── acquisition/     数据接入：五种协议驱动、时序缓冲、五道清洗
│   └── drivers/     Modbus TCP / OPC UA / S7 / MQTT / 仿真
├── features/        特征提取：时域、频域、包络谱、热电能效
├── condition/       工况识别：工况库、归属判定、稳态判定
├── health/          健康评估：基线、指标、异常、故障模式、退化趋势
├── knowledge/       知识层：规则推理、约束、案例复用
├── optimization/    参数寻优：搜索空间、代理模型、三种算法、建议生成
├── control/         控制联动：三类适配器、七道安全门、闭环状态机
├── feedback/        反馈学习：效果评估、奖励、在线学习、知识回写
├── scenario/        场景配置：四套内置场景
├── simulation/      设备仿真：风机、空压机
├── storage/         文件式仓储
├── api/             REST 接口
├── engine.py        总装引擎
├── bootstrap.py     装配脚手架
└── cli.py           命令行入口

docs/
├── 软件说明书.md         14 章，软著文本材料主体
├── 系统设计说明书.md      设计决策与理由
└── 软著申请信息.md        申请表填写参考

tests/                3 个测试文件
examples/run_demo.py  全链路演示
config/system.json    现场配置样例
```

## 运行环境

- Python 3.10+
- NumPy 1.24+（唯一必需的第三方依赖）

系统不引入 Web 框架、不引入数据库、不引入机器学习框架，可直接部署在边缘计算网关。

## 四种控制模式

| 模式 | 行为 | 建议使用阶段 |
|---|---|---|
| `observe` 只读观察 | 只采集与评估，不写 | 投运头两周，积累基线 |
| `advisory` 建议模式 | 生成建议供人工执行 | 第一个月，建立信任 |
| `supervised` 半自动 | 指令进待审批队列 | 过渡期 |
| `autonomous` 自主闭环 | 通过安全校验后自动下发 | 验证充分后 |

不建议跳过前面的阶段直接开自主闭环。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 许可

版权所有，保留全部权利。
