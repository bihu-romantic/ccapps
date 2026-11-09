# 自带样例

每套一个目录。目录里是链接，文件本体仍在原来的 `ccAPPSdb/*/fixtures/`，这样原有的 `loaddata` 和测试不用改。

导入到某个场景（追加，不清空）：

```bash
demo_data/import.sh manufacturing_demo scenario10
```

| 目录 | 导入名 | 内容 |
| --- | --- | --- |
| `demo` | `demo` | 两个工厂，Demand 01–12 |
| `manufacturing_demo` | `manufacturing_demo` | 家具厂 |
| `distribution_demo` | `distribution_demo` | 网球用品分销 |
| `jobshop` | `jobshop` | 机加工车间 |
| `flow_line` | `flow_line` | 流水线 |
| `job_schedule` | `job_schedule` | 柔性作业车间，设备 M001、M002… |

`demo`、`manufacturing_demo`、`distribution_demo` 导入后，订单交期会按今天重算。另外三套保持文件里的日期。

`furniture/` 不能导入数据库。`model.xml` 是引擎用的家具厂模型，两份 `plan_*.xml` 是排程结果导出。
