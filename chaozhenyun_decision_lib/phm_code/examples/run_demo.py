"""
全链路演示脚本
==============

在无任何现场设备的条件下，完整跑通系统的五大功能：

1. 设备实时数据接入（仿真驱动模拟 PLC 采集）
2. 工况识别与健康评估
3. 运行参数优化建议
4. 与控制系统闭环联动（写值 → 回读 → 校验）
5. 执行效果反馈学习（前后对比 → 奖励 → 知识回写）

运行::

    python examples/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maco.bootstrap import build_demo_system          # noqa: E402
from maco.core.enums import ControlMode               # noqa: E402


def line(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(f"  {title}")
        print("=" * 78)


def main() -> int:
    line("步骤 0 · 系统装配")
    engine, sims = build_demo_system(mode=ControlMode.AUTONOMOUS)
    print(f"已装配设备：{', '.join(engine.registry.device_ids())}")
    for device_id, sim in sims.items():
        print(f"  {device_id}: {type(sim).__name__}，"
              f"初始设定 {sim.setpoints}")

    device = "FAN-01"
    profile = engine.registry.get(device)
    sim = sims[device]

    # ---------------------------------------------------------------
    line("步骤 1 · 数据接入与基线积累")
    for i in range(40):
        engine.gateway.collector(device).poll_once()
    coverage = engine.buffer.coverage(device)
    print(f"缓冲覆盖：{coverage['points']} 个测点，"
          f"{coverage['total_samples']} 条样本，跨度 {coverage['span']:.0f}s")
    frame = engine.buffer.latest_frame(device)
    print("最新一帧（部分）：")
    for pid in ("speed", "power", "flow", "bearing_temp", "vib_de"):
        s = frame.get(pid)
        if s:
            print(f"  {profile.point(pid).name:<14} {s.value:>8.2f} "
                  f"{profile.point(pid).unit:<6} 质量={s.quality.label}")

    # ---------------------------------------------------------------
    line("步骤 2 · 工况识别与健康评估")
    for _ in range(25):
        result = engine.decision_cycle(device)
    print(f"工况：{result.condition.label}（{result.condition.condition_id}）"
          f" 稳定性={result.condition.stability.label}"
          f" 置信度={result.condition.confidence:.3f}")
    print(f"工况库已建立 {len(engine.recognizer.library(device).all())} 个工况档案")
    print(f"健康度：{result.health.score:.3f}（{result.health.grade.label}）"
          f" 趋势={result.health.trend.label}")
    print("分项指标（最差 3 项）：")
    for ind in result.health.worst_indicators(3):
        print(f"  {ind.label:<18} 值={ind.value:>8.3f}{ind.unit:<5} "
              f"分={ind.score:.3f} 偏离={ind.deviation:+.2f}σ")

    # ---------------------------------------------------------------
    line("步骤 3 · 运行参数优化建议")
    print(f"代理模型样本数：{engine.advisor.surrogate(profile).sample_count}")
    advice = result.advice
    if advice and advice.changes:
        print(f"建议编号 {advice.advice_id}，算法={advice.algorithm or '知识驱动'}，"
              f"置信度={advice.confidence:.3f}，风险={advice.risk.label}")
        for c in advice.changes:
            print(f"  {c.name}: {c.current_value:.2f}{c.unit} → "
                  f"{c.proposed_value:.2f}{c.unit}（{c.delta:+.2f}）")
        print(f"说明：{advice.explanation}")
        print("证据链：")
        for e in advice.evidences[:5]:
            print(f"  [{e.kind.label}] {e.statement}（可信度 {e.confidence:.2f}）")
    else:
        print(f"本轮无建议：{advice.explanation if advice else '-'}")

    # ---------------------------------------------------------------
    line("步骤 4 · 与控制系统闭环联动")
    if result.loop:
        print(f"闭环阶段：{result.loop.stage}，结论：{result.loop.outcome}")
        if result.loop.command:
            cmd = result.loop.command
            print(f"指令 {cmd.command_id}，状态={cmd.state.label}")
            for item in cmd.items:
                print(f"  写点 {item.point_id}: "
                      f"{item.previous_value:.2f} → {item.value:.2f}{item.unit}")
            for note in cmd.safety_notes:
                print(f"  安全门：{note}")
        if result.loop.execution:
            ex = result.loop.execution
            print(f"执行结果：{'成功' if ex.success else '失败'}，"
                  f"回读={ {k: round(v, 2) for k, v in ex.readback.items()} }，"
                  f"耗时 {ex.duration_ms:.1f}ms")
    print(f"仿真侧收到的写入记录：{sim.write_log[-3:]}")

    # ---------------------------------------------------------------
    line("步骤 5 · 执行效果反馈学习")
    pending = engine.learner.pending_count()
    print(f"待评估任务：{pending}")
    # 继续采集，把 "调整后" 的观测窗填满，然后触发评估
    for _ in range(60):
        engine.gateway.collector(device).poll_once()
    evaluated = 0
    for cmd_id in engine.learner.pending_command_ids():
        ev = engine.learner.run_evaluation(cmd_id)
        if ev:
            evaluated += 1
            print(f"\n评估 {cmd_id}：奖励={ev.reward:+.4f}，"
                  f"采纳={'是' if ev.accepted else '否'}")
            print(f"  {ev.verdict}")
            for comp in ev.comparisons:
                print(f"  {comp.label:<16} {comp.before_mean:>8.2f} → "
                      f"{comp.after_mean:>8.2f}{comp.unit:<6} "
                      f"({comp.delta_ratio * 100:+.1f}%, "
                      f"t={comp.t_stat:+.2f}, "
                      f"{'显著' if comp.significant else '不显著'})")
    if evaluated == 0:
        print("本次演示未产生可评估的下发（可能被安全门拦截或无建议）")

    # ---------------------------------------------------------------
    line("步骤 6 · 故障注入验证健康评估")
    sim.inject_fault("bearing_outer", severity=0.8)
    sim.degradation = 0.45
    for _ in range(20):
        result = engine.decision_cycle(device)
    print(f"注入轴承外圈故障后：健康度={result.health.score:.3f}"
          f"（{result.health.grade.label}）")
    print(f"疑似故障模式：{[(p.label, round(c, 2)) for p, c in result.health.patterns]}")
    print(f"异常项（前 5）：")
    for a in result.health.anomalies[:5]:
        print(f"  - {a}")
    if result.loop:
        print(f"闭环处置：{result.loop.outcome}")

    # ---------------------------------------------------------------
    line("步骤 7 · 系统总体状态")
    status = engine.status()
    print(f"系统：{status['system']['name']} v{status['system']['version']}")
    print(f"决策周期数：{status['system']['cycles']}，"
          f"控制模式：{status['system']['control_mode_label']}")
    print(f"采集：{status['acquisition']['buffer']['samples']} 条样本，"
          f"清洗命中 {status['acquisition']['cleansing']}")
    print(f"安全门：通过 {status['safety']['passed']} / "
          f"驳回 {status['safety']['rejected']}")
    print(f"知识库：{status['knowledge']['rules']} 条规则，"
          f"{status['knowledge']['constraints']} 条约束，"
          f"案例 {status['knowledge']['cases']['total']} 条")
    print(f"反馈学习：{status['feedback']['evaluations']} 次评估，"
          f"采纳率 {status['feedback']['accept_rate'] * 100:.0f}%，"
          f"平均奖励 {status['feedback']['avg_reward']:+.4f}")
    print(f"审计账本：{status['audit']['records']} 条记录")
    verify = engine.audit.verify_chain()
    print(f"审计链校验：{'通过' if verify['ok'] else '失败'}"
          f"（共 {verify['checked']} 条）")

    line("演示结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
