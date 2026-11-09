"""Transparent first-version inventory policy math, independent of Django."""

import math
import operator
import re
from statistics import NormalDist

DEFAULTS = {
    "service_level": 95.0,
    "leadtime_days": None,
    "leadtime_deviation": 0.0,
    "demand_deviation": None,
    "ss_min_qty": None,
    "ss_max_qty": None,
    "ss_min_poc": None,
    "ss_max_poc": None,
    "roq_min_qty": None,
    "roq_max_qty": None,
    "roq_min_poc": None,
    "roq_max_poc": None,
    "fixed_order_cost": 20.0,
    "holding_cost": 0.05,
    "do_not_stock": 0.0,
}
FIELDS = {
    f"{entity}.{field}"
    for entity in ("item", "location")
    for field in ("name", "category", "subcategory", "cost", "description")
}
FIELDS.remove("location.cost")
CLAUSE = re.compile(
    r"\s*(item|location)\.(\w+)\s*(<=|>=|!=|<>|=|<|>|like\b)\s*"
    r"('(?:[^']|'')*'|[-+]?\d+(?:\.\d+)?)\s*",
    re.I,
)


def compile_segment(query):
    """Restricted SQL-like AND predicates. Never execute user-provided SQL."""
    clauses, pos = [], 0
    while pos < len(query):
        match = CLAUSE.match(query, pos)
        if not match:
            raise ValueError("分组仅支持白名单字段比较、LIKE及AND连接")
        entity, field, op, raw = match.groups()
        key = f"{entity.lower()}.{field.lower()}"
        if key not in FIELDS:
            raise ValueError(f"分组字段不支持：{key}")
        value = raw[1:-1].replace("''", "'") if raw.startswith("'") else float(raw)
        clauses.append((key, op.lower(), value))
        pos = match.end()
        if pos == len(query):
            break
        conjunction = re.match(r"and\s+", query[pos:], re.I)
        if not conjunction:
            raise ValueError("分组仅支持AND连接，禁止SQL函数或其他表达式")
        pos += conjunction.end()
        if pos == len(query):
            raise ValueError("AND之后缺少条件")
    if not clauses:
        raise ValueError("分组条件不能为空")
    return clauses


def matches(clauses, attributes):
    comparisons = {
        "=": operator.eq,
        "!=": operator.ne,
        "<>": operator.ne,
        "<": operator.lt,
        ">": operator.gt,
        "<=": operator.le,
        ">=": operator.ge,
    }
    for key, op, expected in clauses:
        actual = attributes.get(key)
        if actual is None:
            return False
        if op == "like":
            pattern = "".join(
                ".*" if c == "%" else "." if c == "_" else re.escape(c)
                for c in str(expected)
            )
            valid = re.fullmatch(pattern, str(actual), re.S) is not None
        else:
            if isinstance(expected, float):
                try:
                    actual = float(actual)
                except (ValueError, TypeError):
                    return False
            valid = comparisons[op](actual, expected)
        if not valid:
            return False
    return True


def validate_parameters(values):
    if not isinstance(values, dict):
        raise ValueError("单品覆盖必须是JSON对象")
    for key, raw in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"未知库存参数：{key}")
        if raw is None:
            continue
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key}必须是非负有限数")
        if key == "service_level" and not 50 <= value < 100:
            raise ValueError("周期服务水平必须在50（含）至100（不含）之间")
        if key == "do_not_stock" and value not in (0, 1):
            raise ValueError("do_not_stock必须为0或1")


def bounded(value, mean, params, prefix):
    # Match the published frePPLe combination rule, including zero bounds.
    lows = [
        params[f"{prefix}_min_qty"],
        (
            None
            if params[f"{prefix}_min_poc"] is None
            else mean * params[f"{prefix}_min_poc"]
        ),
    ]
    highs = [
        params[f"{prefix}_max_qty"],
        (
            None
            if params[f"{prefix}_max_poc"] is None
            else mean * params[f"{prefix}_max_poc"]
        ),
    ]
    lows, highs = [v for v in lows if v is not None], [
        v for v in highs if v is not None
    ]
    if lows:
        value = max(value, min(lows))
    if highs:
        value = min(value, max(highs))
    return value


def calculate(mean, deviation, leadtime, cost, params):
    validate_parameters(params)
    if any(not math.isfinite(v) or v < 0 for v in (mean, deviation, leadtime, cost)):
        raise ValueError("需求、波动、提前期和成本必须为非负有限数")
    p = {**DEFAULTS, **{k: float(v) for k, v in params.items() if v is not None}}
    if p["do_not_stock"]:
        return {"ss": 0.0, "roq": 1.0, "warnings": []}
    leadtime = p["leadtime_days"] if p["leadtime_days"] is not None else leadtime
    deviation = (
        p["demand_deviation"] if p["demand_deviation"] is not None else deviation
    )
    z = NormalDist().inv_cdf(p["service_level"] / 100)
    ss = z * math.sqrt(leadtime * deviation**2 + mean**2 * p["leadtime_deviation"] ** 2)
    warnings = []
    holding = cost * p["holding_cost"]
    if mean <= 0:
        roq = 0.0
        warnings.append("历史窗口无需求，统计计算不产生补货目标")
    elif holding <= 0:
        roq = 0.0
        warnings.append("物料价值或持有成本率为0，EOQ不可计算，请设置ROQ下限")
    else:
        roq = math.sqrt(2 * mean * 365 * p["fixed_order_cost"] / holding)
    return {
        "ss": round(bounded(ss, mean, p, "ss"), 6),
        "roq": round(bounded(roq, mean, p, "roq"), 6),
        "warnings": warnings,
    }
