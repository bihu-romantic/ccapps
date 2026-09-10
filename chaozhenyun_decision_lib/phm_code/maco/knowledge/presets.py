"""
内置知识包
==========

系统出厂自带的通用规则与约束，覆盖旋转机械最常见的调节场景。
现场可通过配置文件追加或覆盖，本模块只提供 "开箱即用" 的起点。

规则动作约定：
* ``adjust.<参数名>`` —— 建议调整方向与幅度，格式 ``"+5%"`` / ``"-2"`` / ``"=45"``
* ``block.optimize`` —— 禁止本轮寻优（安全优先）
* ``pattern`` —— 推断出的故障模式
* ``note`` —— 给操作工的说明
"""

from __future__ import annotations

from typing import List

from ..core.enums import Severity
from .base import CaseBase, Constraint, ConstraintSet, KnowledgeBase, Rule, RuleEngine


def default_rules() -> List[Rule]:
    """通用规则库。"""
    return [
        Rule(
            rule_id="R-SAFE-01", name="健康度危险时禁止自动调参",
            condition="health_score < 0.4",
            actions={"block.optimize": True,
                     "note": "健康度处于危险区，应先检修再谈优化"},
            confidence=0.99, priority=100, severity=Severity.ALARM,
            description="设备已严重劣化，任何参数调整都可能掩盖问题或加剧损坏",
        ),
        Rule(
            rule_id="R-SAFE-02", name="非稳态工况禁止调参",
            condition="stability != 'steady'",
            actions={"block.optimize": True,
                     "note": "工况尚未稳定，等待稳态后再优化"},
            confidence=0.95, priority=99, severity=Severity.INFO,
            description="过渡过程中的测量值不代表稳态性能，此时寻优会得到错误结论",
        ),
        Rule(
            rule_id="R-SAFE-03", name="温度接近红线时优先降载",
            condition="temp_dev > 3.0 and load_ratio > 0.85",
            actions={"adjust.load_setpoint": "-5%",
                     "pattern": "overheat",
                     "note": "温度显著偏高且接近满载，建议先降载散热"},
            confidence=0.85, priority=90, severity=Severity.WARNING,
        ),
        Rule(
            rule_id="R-VIB-01", name="振动偏高时降低转速",
            condition="vib_dev > 2.5 and speed_ratio > 0.7",
            actions={"adjust.speed_setpoint": "-3%",
                     "note": "振动偏离基线较多，适当降速以降低激振能量"},
            confidence=0.75, priority=80, severity=Severity.WARNING,
        ),
        Rule(
            rule_id="R-VIB-02", name="轴承特征频率突出时限速并报修",
            condition="bpfo_ratio > 4.0 or bpfi_ratio > 4.0",
            actions={"adjust.speed_setpoint": "-5%",
                     "pattern": "bearing",
                     "note": "包络谱轴承特征频率突出，建议限速运行并安排检查"},
            confidence=0.80, priority=88, severity=Severity.ALARM,
        ),
        Rule(
            rule_id="R-EFF-01", name="能效偏低且工况稳定时寻优",
            condition="efficiency_dev < -1.5 and stability == 'steady' and health_score > 0.7",
            actions={"enable.optimize": True,
                     "note": "单位能耗高于本工况基线，具备寻优空间"},
            confidence=0.70, priority=60,
        ),
        Rule(
            rule_id="R-EFF-02", name="轻载长期运行建议并机调度",
            condition="load_ratio < 0.35 and stability == 'steady'",
            actions={"note": "长期轻载运行效率低，建议检查是否可减机或提负荷",
                     "adjust.load_setpoint": "+8%"},
            confidence=0.60, priority=50,
        ),
        Rule(
            rule_id="R-ELE-01", name="三相不平衡时禁止升载",
            condition="current_unbalance > 0.05",
            actions={"block.increase_load": True,
                     "note": "三相电流不平衡超标，升载会加剧电机发热"},
            confidence=0.88, priority=85, severity=Severity.WARNING,
        ),
        Rule(
            rule_id="R-TRD-01", name="退化趋势明显时收紧调整幅度",
            condition="degradation_rate < -0.01",
            actions={"limit.delta_ratio": 0.03,
                     "note": "健康度持续下滑，本阶段只做小幅调整"},
            confidence=0.72, priority=70,
        ),
        Rule(
            rule_id="R-NEW-01", name="新工况先观察不调参",
            condition="condition_is_new == True",
            actions={"block.optimize": True,
                     "note": "首次进入该工况，先积累基线再优化"},
            confidence=0.90, priority=95,
        ),
    ]


def default_constraints() -> List[Constraint]:
    """通用工艺与安全约束。"""
    return [
        Constraint("C-01", "轴承温度上限", "temp_max < 85", "hard", 1.0,
                   "轴承温度不得超过 85℃"),
        Constraint("C-02", "振动烈度上限", "vib_rms < 11.2", "hard", 1.0,
                   "振动有效值不得超过 ISO 20816 C 区上限 11.2 mm/s"),
        Constraint("C-03", "电流不超额定", "current_ratio < 1.05", "hard", 1.0,
                   "运行电流不得超过额定值的 105%"),
        Constraint("C-04", "转速在允许区间", "speed_ratio > 0.3 and speed_ratio < 1.02",
                   "hard", 1.0, "转速须在额定值的 30%~102% 之间"),
        Constraint("C-05", "三相不平衡限值", "current_unbalance < 0.08", "soft", 0.5,
                   "三相电流不平衡度宜低于 8%"),
        Constraint("C-06", "能效不劣化", "efficiency_dev > -3.0", "soft", 0.3,
                   "单位能耗不宜显著高于基线"),
    ]


def build_default_knowledge_base(rule_file: str = "", case_file: str = "") -> KnowledgeBase:
    """构造出厂知识库，并尝试叠加现场配置文件。"""
    engine = RuleEngine(default_rules())
    constraints = ConstraintSet(default_constraints())
    cases = CaseBase()
    kb = KnowledgeBase(engine, cases, constraints)
    if rule_file or case_file:
        kb.load_all(rule_file, case_file)
    return kb


__all__ = ["default_rules", "default_constraints", "build_default_knowledge_base"]
