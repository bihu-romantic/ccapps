# 库存策略第一版

在现有 safetystock 应用上新增可审计的库存策略，不依赖或复刻 frePPLe 企业版模块。当前仅场景2已执行数据库迁移。

## 使用

- 页面：场景2 `/scenario2/inventory/policies/`，菜单“库存策略 SS / ROQ”。
- 分组、规则、单品覆盖分别通过页面链接进入管理表单。
- `inventoryplan --database=scenario2`：计算并保存结果，不修改 Buffer 库存目标。
- 加 `--apply`：设置 Buffer.minimum=SS、Buffer.maximum=SS+ROQ，采用补到目标上限的策略，不是强制固定ROQ下单。
- `runplan --database=scenario2 --env=supply,inventorypolicy`：先计算目标，再加载库存并运行供应计划。
- 原来的 safetystock 任务在 inventorypolicy 同时启用时自动跳过，避免覆盖分组策略。

## 算法和口径

- 基于计划日期之前完整N天的销售需求，默认365天。包含closed历史销售，排除canceled/quote；没有需求的天按零计入。
- 同物料下游需求沿当前有效、优先级最高的单一路径向调拨上游汇总，不重复叠加备选路径。
- 直接读取日需求波动；不把它宣称为剔除季节趋势后的预测误差。
- SS = NormalQuantile(service_level/100) × sqrt(L × sigma_d² + mean_d² × sigma_L²)。service_level是正态假设下的周期服务水平，不是fill rate。
- ROQ = sqrt(2 × mean_daily × 365 × fixed_order_cost / (item.cost × holding_cost))。holding_cost为年比率，0.05表示5%。零成本时提示EOQ不可计算，仍可由ROQ下限指定。
- 数量/覆盖天数下限取较小值，上限取较大值；上限最后应用，发生冲突时上限优先。未设置的null与明确设置为0不同。
- 分组规则按priority升序、同优先级按ID，逐参数取首个匹配值；单品非null覆盖最后应用。计算结果记录参数来源、方法、日期和警告。
- SS/ROQ可为小数，实际采购与调拨由供应求解器处理最小量、倍数等约束。
- 提前期由当前有效采购/调拨路径计算；遇到受保护的上游库存点停止累加。不备库存节点不作为解耦点。
- do_not_stock=1时，SS=0、ROQ=1。
- 分组表达式只支持白名单字段、常量比较、LIKE、AND；不执行用户输入的SQL。

## 第一版边界

未包含季节预测、间歇需求分布、制造BOM的需求展开/提前期、供应商工作日历和多源份额分摊。无有效路径时使用默认提前期并提示。批次库存以及已有库存上下限日历仅展示结果，不自动覆盖。计算目标本身不会生成未来销售预测。

库存应用需具备库存策略修改及Buffer修改权限；计算与结果发布在数据库事务中完成。原始场景2数据和应用前状态已有pg_dump备份。

## 验证

`python -m unittest ccAPPSdb.safetystock.tests.test_policy -v`

场景2实测：4个分组、3条官网规则、33个库存节点。SS/ROQ接入MRP后生成11张采购建议和24张调拨建议。最小预计库存0；全部节点最终达到安全库存目标。26个初期安全库存不足事件均在2021-02-25前结束，属于初始库存不足和供应提前期造成的等待。
