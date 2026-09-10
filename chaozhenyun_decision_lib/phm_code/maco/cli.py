"""
命令行入口
==========

提供运维人员日常使用的命令：

    python -m maco.cli demo               运行全链路演示（全仿真）
    python -m maco.cli run --config ...   按配置启动现场系统
    python -m maco.cli serve              启动系统并开放 HTTP 接口
    python -m maco.cli status             打印一次系统状态
    python -m maco.cli cycle --device ... 手动触发一次决策
    python -m maco.cli scenarios          列出内置场景
    python -m maco.cli verify-audit       校验审计账本完整性
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from .bootstrap import build_demo_system, build_field_system
from .core.config import Config
from .core.enums import ControlMode
from .core.logging_setup import get_logger, setup_logging
from .engine import ControlEngine
from .scenario.presets import list_scenarios

logger = get_logger("cli")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _print_cycle(result: Any) -> None:
    print(result.summary())
    if result.advice and result.advice.changes:
        print("  建议：")
        for c in result.advice.changes:
            print(f"    {c.name}: {c.current_value:.2f} → "
                  f"{c.proposed_value:.2f}{c.unit}（{c.delta:+.2f}）")
        print(f"  说明：{result.advice.explanation}")
    if result.loop:
        print(f"  闭环：{result.loop.stage} / {result.loop.outcome}")


# ==========================================================================
# 子命令
# ==========================================================================
def cmd_demo(args: argparse.Namespace) -> int:
    """全仿真演示：跑若干轮决策，打印每轮结果。"""
    engine, sims = build_demo_system(
        mode=ControlMode.from_text(args.mode, ControlMode.AUTONOMOUS)
    )
    device = args.device or engine.registry.device_ids()[0]
    print(f"演示设备：{device}，控制模式：{engine.safety.mode.label}")

    print("\n--- 阶段一：积累基线 ---")
    collector = engine.gateway.collector(device)
    for _ in range(args.warmup):
        collector.poll_once()
    print(f"已采集 {engine.buffer.coverage(device)['total_samples']} 条样本")

    print("\n--- 阶段二：决策闭环 ---")
    for i in range(args.rounds):
        result = engine.decision_cycle(device)
        print(f"[{i + 1:02d}] ", end="")
        _print_cycle(result)

    if args.inject:
        print(f"\n--- 阶段三：注入故障 {args.inject} ---")
        sims[device].inject_fault(args.inject, 0.8)
        sims[device].degradation = 0.5
        for i in range(5):
            result = engine.decision_cycle(device)
            print(f"[F{i + 1}] ", end="")
            _print_cycle(result)

    print("\n--- 阶段四：效果评估 ---")
    for _ in range(args.warmup):
        collector.poll_once()
    for cmd_id in engine.learner.pending_command_ids():
        ev = engine.learner.run_evaluation(cmd_id)
        if ev:
            print(f"  {cmd_id}: 奖励={ev.reward:+.4f} "
                  f"采纳={'是' if ev.accepted else '否'}")
            print(f"    {ev.verdict}")

    print("\n--- 系统状态 ---")
    status = engine.status()
    print(f"决策周期 {status['system']['cycles']} 次，"
          f"安全门通过 {status['safety']['passed']} / "
          f"驳回 {status['safety']['rejected']}")
    print(f"工况库 {sum(len(l['conditions']) for l in status['condition']['libraries'])} 个工况，"
          f"案例库 {status['knowledge']['cases']['total']} 条")
    print(f"审计 {status['audit']['records']} 条，"
          f"链校验 {'通过' if engine.audit.verify_chain()['ok'] else '失败'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """按配置启动现场系统，持续运行。"""
    engine = build_field_system(args.config)
    engine.start(decision_interval_s=args.interval)
    print(f"系统已启动，Ctrl+C 停止。设备：{engine.registry.device_ids()}")
    try:
        while True:
            time.sleep(5.0)
    except KeyboardInterrupt:
        print("\n收到停止信号")
    finally:
        engine.stop()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """启动系统并开放 HTTP 接口。"""
    from .api.server import ApiServer

    if args.demo:
        engine, _ = build_demo_system()
    else:
        engine = build_field_system(args.config)
    engine.start(decision_interval_s=args.interval)

    token = os.environ.get("MACO_API_TOKEN", "")
    server = ApiServer(engine, host=args.host, port=args.port, access_token=token)
    if not token:
        logger.warning("未设置 MACO_API_TOKEN，写操作接口将不做令牌校验")
    try:
        server.start(background=False)
    except KeyboardInterrupt:
        print("\n收到停止信号")
    finally:
        server.stop()
        engine.stop()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    engine, _ = build_demo_system()
    for _ in range(10):
        for device in engine.registry.device_ids():
            engine.gateway.collector(device).poll_once()
    _print_json(engine.status())
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    engine, _ = build_demo_system()
    device = args.device or engine.registry.device_ids()[0]
    collector = engine.gateway.collector(device)
    for _ in range(args.warmup):
        collector.poll_once()
    result = engine.decision_cycle(device)
    if args.json:
        _print_json(result.to_dict())
    else:
        _print_cycle(result)
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    for item in list_scenarios():
        print(f"[{item['scenario_id']}] {item['name']}（{item['device_type']}）")
        print(f"    {item['description']}")
        print(f"    目标：{', '.join(o['name'] for o in item['objectives'])}")
        print(f"    专属规则 {item['rules']} 条，专属约束 {item['constraints']} 条\n")
    return 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    from .core.audit import AuditTrail

    trail = AuditTrail(args.file, enable=True)
    result = trail.verify_chain()
    print(f"审计账本：{args.file}")
    print(f"校验结果：{'通过' if result['ok'] else '失败'}")
    print(f"已检查记录：{result['checked']} 条")
    if not result["ok"]:
        print(f"断裂位置：序号 {result['broken_at']}，原因：{result['reason']}")
        return 1
    return 0


# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maco", description="多工况自适应工业设备控制优化系统"
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="全仿真演示")
    p.add_argument("--device", default="", help="设备号")
    p.add_argument("--rounds", type=int, default=10, help="决策轮数")
    p.add_argument("--warmup", type=int, default=60, help="预热采集次数")
    p.add_argument("--mode", default="autonomous", help="控制模式")
    p.add_argument("--inject", default="", help="注入故障类型")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("run", help="按配置启动现场系统")
    p.add_argument("--config", default="config/system.json")
    p.add_argument("--interval", type=float, default=30.0, help="决策周期（秒）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="启动系统并开放 HTTP 接口")
    p.add_argument("--config", default="config/system.json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--demo", action="store_true", help="使用仿真设备")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("status", help="打印系统状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cycle", help="手动触发一次决策")
    p.add_argument("--device", default="")
    p.add_argument("--warmup", type=int, default=60)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cycle)

    p = sub.add_parser("scenarios", help="列出内置场景")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("verify-audit", help="校验审计账本完整性")
    p.add_argument("--file", default="data/store/audit.jsonl")
    p.set_defaults(func=cmd_verify_audit)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.error("命令执行失败: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
