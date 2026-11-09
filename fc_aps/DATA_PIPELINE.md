# FC 原始表 → 排产主数据

通用入口：

```bash
python build_fc_dataset.py \
  --source-dir ../fc_data/20261016 \
  --profile unin_alarm \
  --output ../fc_data/20261016/fc_aps_scenario8.json
```

## 两阶段（不要混）

| 阶段 | 模块 | 做什么 |
|------|------|--------|
| A 校验+打底 | `convert_fc_aps.build()` | 表间关系验真；写成 item/buffer/operation/resource/demand 底稿 |
| B 场景规则 | `build_unin_alarm_dataset()` | 未入库路由、预警净算、共模、谁 open/closed |

然后：`import_standard_dataset` → scenario → FAS/CP。

## 导入前关系检查

```bash
python build_fc_dataset.py --source-dir ../fc_data/20261016 --profile check
```

覆盖：BOM关联、资源（机台/模具）、工时、自制/外采、经济批量/SNP、库存ATP、投入产出比、未入库历史排单 vs 当前对照可调范围。实现：`check_fc_dataset.py`。

## 文件角色

- **`build_fc_dataset.py`** — 通用 CLI（推荐）
- **`convert_fc_aps.py`** — 阶段 A
- **`build_scenario7_unin.py`** — 阶段 B（历史名保留；请用 `build_unin_alarm_dataset`）
- **`check_fc_dataset.py`** — 源表关系简单检查
- **`check_fas_constraints.py`** — 排程后约束检查（不属于转换）
