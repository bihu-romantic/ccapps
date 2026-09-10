#!/usr/bin/env python3
"""Build a simple static Gantt chart from schedule_tasks in SQLite."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import html
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_plan_start(conn: sqlite3.Connection) -> str:
    row = conn.execute("select plan_start from case_planning_dates limit 1").fetchone()
    if row and row["plan_start"]:
        return str(row["plan_start"])
    return "2026-08-07T07:00:00"


def latest_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("select max(id) from schedule_versions").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("no schedule_versions found")
    return int(row[0])


def load_tasks(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        """
        select demand, operation, resources, quantity, start_seconds, end_seconds, status
        from schedule_tasks
        where version_id = ?
        order by start_seconds, end_seconds, demand, operation
        """,
        (version_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_version(conn: sqlite3.Connection, version_id: int) -> dict:
    row = conn.execute("select * from schedule_versions where id = ?", (version_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"version not found: {version_id}")
    return dict(row)


def date_label(plan_start: str, seconds: float) -> str:
    try:
        base = datetime.fromisoformat(plan_start)
        return (base + timedelta(seconds=float(seconds))).strftime("%m-%d %H:%M")
    except ValueError:
        return str(seconds)


def static_summary_html(tasks: list[dict], plan_start: str) -> str:
    if not tasks:
        return '<div class="card">任务数：0</div>'
    resources = set()
    status_counts: dict[str, int] = {}
    for task in tasks:
        status_counts[str(task["status"])] = status_counts.get(str(task["status"]), 0) + 1
        for resource in str(task["resources"]).split("+"):
            if resource.strip():
                resources.add(resource.strip())
    start = min(float(task["start_seconds"]) for task in tasks)
    end = max(float(task["end_seconds"]) for task in tasks)
    cards = [
        f'<div class="card">任务数：{len(tasks)}</div>',
        f'<div class="card">资源数：{len(resources)}</div>',
        f'<div class="card">时间范围：{html.escape(date_label(plan_start, start))} - {html.escape(date_label(plan_start, end))}</div>',
    ]
    cards.extend(
        f'<div class="card">{html.escape(status)}：{count}</div>'
        for status, count in sorted(status_counts.items())
    )
    return "\n    ".join(cards)


def static_table_html(tasks: list[dict], plan_start: str, limit: int = 100) -> str:
    rows = []
    for task in tasks[:limit]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(task['demand']))}</td>"
            f"<td>{html.escape(str(task['operation']))}</td>"
            f"<td>{html.escape(str(task['resources']))}</td>"
            f"<td>{html.escape(str(task['quantity']))}</td>"
            f"<td>{html.escape(str(task['status']))}</td>"
            f"<td>{html.escape(date_label(plan_start, float(task['start_seconds'])))}</td>"
            f"<td>{html.escape(date_label(plan_start, float(task['end_seconds'])))}</td>"
            "</tr>"
        )
    return "\n      ".join(rows)


def static_gantt_svg(tasks: list[dict], plan_start: str) -> str:
    if not tasks:
        return '<svg id="chart" height="80"><text x="20" y="40">没有排程任务。</text></svg>'

    expanded: list[dict] = []
    for index, task in enumerate(tasks):
        for resource in str(task["resources"]).split("+"):
            resource = resource.strip()
            if resource:
                expanded.append({"task": task, "index": index, "resource": resource, "layer": 0})

    by_resource: dict[str, list[dict]] = {}
    for item in expanded:
        by_resource.setdefault(item["resource"], []).append(item)

    groups = []
    lanes = []
    for resource in sorted(by_resource):
        layer_ends: list[float] = []
        items = sorted(by_resource[resource], key=lambda x: (float(x["task"]["start_seconds"]), float(x["task"]["end_seconds"])))
        for item in items:
            start = float(item["task"]["start_seconds"])
            layer = next((idx for idx, end in enumerate(layer_ends) if end <= start), -1)
            if layer < 0:
                layer = len(layer_ends)
                layer_ends.append(0.0)
            layer_ends[layer] = float(item["task"]["end_seconds"])
            item["layer"] = layer
        first = len(lanes)
        layer_count = max(1, len(layer_ends))
        groups.append({"resource": resource, "first": first, "layers": layer_count})
        for layer in range(layer_count):
            lanes.append({"resource": resource, "layer": layer})

    group_index = {group["resource"]: group for group in groups}
    start_min = min(float(task["start_seconds"]) for task in tasks)
    end_max = max(float(task["end_seconds"]) for task in tasks)
    label_width = 210
    top = 44
    lane_height = 28
    width = int(label_width + max(1000, (end_max - start_min) / 3600 * 8))
    height = int(top + len(lanes) * lane_height + 24)
    span = end_max - start_min or 1.0

    status_color = {
        "planned": "#2f80ed",
        "confirmed": "#8a94a6",
        "running": "#27ae60",
        "material_shortage": "#e67e22",
    }

    out = [f'<svg id="chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for i in range(9):
        t = start_min + span * i / 8
        x = label_width + (width - label_width) * i / 8
        out.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{height - 18}"/>')
        out.append(
            f'<text class="axis" x="{x:.1f}" y="22" text-anchor="middle">'
            f'{html.escape(date_label(plan_start, t))}</text>'
        )

    for lane_idx, lane in enumerate(lanes):
        y = top + lane_idx * lane_height
        out.append(f'<line class="lane" x1="0" x2="{width}" y1="{y + lane_height}" y2="{y + lane_height}"/>')
        if lane["layer"] == 0:
            out.append(f'<text class="resource" x="8" y="{y + 19}">{html.escape(lane["resource"])}</text>')

    for item in expanded:
        task = item["task"]
        group = group_index[item["resource"]]
        lane_idx = int(group["first"]) + int(item["layer"])
        task_start = float(task["start_seconds"])
        task_end = float(task["end_seconds"])
        x = label_width + (task_start - start_min) / span * (width - label_width)
        w = max(2.0, (task_end - task_start) / span * (width - label_width))
        y = top + lane_idx * lane_height + 5
        color = status_color.get(str(task["status"]), "#2f80ed")
        title = (
            f"{task['demand']} | {task['operation']} | {item['resource']} | "
            f"{task['status']} | {date_label(plan_start, task_start)} - {date_label(plan_start, task_end)}"
        )
        out.append(
            f'<rect class="bar" fill="{color}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="20" rx="3">'
            f'<title>{html.escape(title)}</title></rect>'
        )
    out.append("</svg>")
    return "\n".join(out)


def render_html(tasks: list[dict], version: dict, plan_start: str) -> str:
    data = json.dumps(tasks, ensure_ascii=False)
    summary_html = static_summary_html(tasks, plan_start)
    table_html = static_table_html(tasks, plan_start)
    gantt_svg = static_gantt_svg(tasks, plan_start)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>超真云 - 设备排程甘特图</title>
  <style>
    body {{ margin: 0; padding: 20px; font-family: Arial, sans-serif; background: #f5f7fb; color: #172033; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .meta {{ color: #526077; font-size: 13px; margin-bottom: 14px; }}
    .legend {{ display: flex; gap: 14px; margin: 0 0 12px; font-size: 13px; color: #526077; }}
    .dot {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 4px; vertical-align: -2px; }}
    .panel {{ background: #fff; border: 1px solid #dce3ee; border-radius: 8px; overflow: auto; box-shadow: 0 2px 8px #1523450d; }}
    .summary {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 12px; }}
    .card {{ background: #fff; border: 1px solid #dce3ee; border-radius: 8px; padding: 8px 10px; font-size: 13px; box-shadow: 0 1px 5px #1523450a; }}
    svg {{ display: block; min-width: 1000px; }}
    .grid {{ stroke: #e7ecf4; stroke-width: 1; }}
    .lane {{ stroke: #edf1f7; stroke-width: 1; }}
    .axis {{ fill: #526077; font-size: 11px; }}
    .resource {{ fill: #172033; font-size: 12px; }}
    .bar {{ stroke: #fff; stroke-width: 1; cursor: pointer; }}
    .planned {{ fill: #2f80ed; }}
    .confirmed {{ fill: #8a94a6; }}
    .running {{ fill: #27ae60; }}
    .material_shortage {{ fill: #e67e22; }}
    .tooltip {{ position: fixed; display: none; pointer-events: none; max-width: 380px; padding: 9px 11px; border-radius: 6px; color: white; background: #172033; font-size: 12px; line-height: 1.5; box-shadow: 0 3px 12px #0004; }}
    .table-panel {{ margin-top: 14px; background: #fff; border: 1px solid #dce3ee; border-radius: 8px; overflow: auto; box-shadow: 0 2px 8px #1523450d; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 900px; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid #edf1f7; padding: 6px 8px; text-align: left; white-space: nowrap; }}
    th {{ background: #f8fafd; color: #526077; position: sticky; top: 0; }}
  </style>
</head>
<body>
  <h1>设备排程甘特图</h1>
  <div class="meta">计划起点：{html.escape(plan_start)}</div>
  <div id="summary" class="summary">
    {summary_html}
  </div>
  <div class="legend">
    <span><i class="dot planned"></i>蓝色：计划任务 planned</span>
    <span><i class="dot confirmed"></i>灰色：已确认/已完成 confirmed</span>
    <span><i class="dot running"></i>绿色：正在执行 running</span>
    <span><i class="dot material_shortage"></i>橙色：缺料风险 material_shortage</span>
  </div>
  <div class="panel">{gantt_svg}</div>
  <div class="table-panel">
    <table>
      <thead><tr><th>订单</th><th>工序</th><th>资源</th><th>数量</th><th>状态</th><th>开始</th><th>结束</th></tr></thead>
      <tbody id="taskTable">
      {table_html}
      </tbody>
    </table>
  </div>
  <div id="tooltip" class="tooltip"></div>
  <script>
    const tasks = {data};
    const planStart = new Date("{plan_start}");
    const chart = document.getElementById("chart");
    const tooltip = document.getElementById("tooltip");
    const summary = document.getElementById("summary");
    const taskTable = document.getElementById("taskTable");
    const labelWidth = 210, top = 44, laneHeight = 28;

    function esc(value) {{
      return String(value).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
    }}
    function dateLabel(seconds) {{
      const d = new Date(planStart.getTime() + Number(seconds) * 1000);
      return d.toLocaleString("zh-CN", {{month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}});
    }}
    function renderSummary(start, end, resourceCount) {{
      const statusCounts = tasks.reduce((acc, task) => {{
        acc[task.status] = (acc[task.status] || 0) + 1;
        return acc;
      }}, {{}});
      summary.innerHTML = [
        `<div class="card">任务数：${{tasks.length}}</div>`,
        `<div class="card">资源数：${{resourceCount}}</div>`,
        `<div class="card">时间范围：${{dateLabel(start)}} - ${{dateLabel(end)}}</div>`,
        ...Object.entries(statusCounts).map(([k,v]) => `<div class="card">${{esc(k)}}：${{v}}</div>`)
      ].join("");
    }}
    function renderTable() {{
      taskTable.innerHTML = tasks.slice(0, 80).map(task => `
        <tr>
          <td>${{esc(task.demand)}}</td>
          <td>${{esc(task.operation)}}</td>
          <td>${{esc(task.resources)}}</td>
          <td>${{esc(task.quantity)}}</td>
          <td>${{esc(task.status)}}</td>
          <td>${{dateLabel(task.start_seconds)}}</td>
          <td>${{dateLabel(task.end_seconds)}}</td>
        </tr>
      `).join("");
    }}
    function draw() {{
      if (!tasks.length) {{
        chart.setAttribute("height", 80);
        chart.innerHTML = '<text x="20" y="40">没有排程任务。</text>';
        summary.innerHTML = '<div class="card">任务数：0</div>';
        return;
      }}
      const expanded = [];
      tasks.forEach((task, index) => {{
        String(task.resources).split("+").map(x => x.trim()).filter(Boolean).forEach(resource => expanded.push({{task, index, resource}}));
      }});
      const resourceMap = new Map();
      expanded.forEach(item => {{
        if (!resourceMap.has(item.resource)) resourceMap.set(item.resource, []);
        resourceMap.get(item.resource).push(item);
      }});
      const groups = [];
      [...resourceMap.keys()].sort().forEach(resource => {{
        const layerEnds = [];
        resourceMap.get(resource).sort((a,b) => Number(a.task.start_seconds) - Number(b.task.start_seconds)).forEach(item => {{
          let layer = layerEnds.findIndex(end => end <= Number(item.task.start_seconds));
          if (layer < 0) {{ layer = layerEnds.length; layerEnds.push(0); }}
          layerEnds[layer] = Number(item.task.end_seconds);
          item.layer = layer;
        }});
        groups.push({{resource, layers: Math.max(1, layerEnds.length)}});
      }});
      const lanes = [];
      groups.forEach(group => {{
        group.first = lanes.length;
        for (let layer = 0; layer < group.layers; layer++) lanes.push({{resource: group.resource, layer}});
      }});
      const start = Math.min(...tasks.map(t => Number(t.start_seconds)));
      const end = Math.max(...tasks.map(t => Number(t.end_seconds)));
      renderSummary(start, end, groups.length);
      renderTable();
      const width = labelWidth + Math.max(900, (end - start) / 3600 * 8);
      const height = top + lanes.length * laneHeight + 22;
      chart.setAttribute("width", width);
      chart.setAttribute("height", height);
      const out = [];
      for (let i = 0; i <= 8; i++) {{
        const t = start + (end - start) * i / 8;
        const x = labelWidth + (width - labelWidth) * i / 8;
        out.push(`<line class="grid" x1="${{x}}" x2="${{x}}" y1="${{top}}" y2="${{height - 18}}"/>`);
        out.push(`<text class="axis" x="${{x}}" y="22" text-anchor="middle">${{dateLabel(t)}}</text>`);
      }}
      lanes.forEach((lane, i) => {{
        const y = top + i * laneHeight;
        out.push(`<line class="lane" x1="0" x2="${{width}}" y1="${{y + laneHeight}}" y2="${{y + laneHeight}}"/>`);
        if (lane.layer === 0) out.push(`<text class="resource" x="8" y="${{y + 19}}">${{esc(lane.resource)}}</text>`);
      }});
      expanded.forEach(item => {{
        const group = groups.find(g => g.resource === item.resource);
        const lane = group.first + item.layer;
        const x = labelWidth + (Number(item.task.start_seconds) - start) / (end - start || 1) * (width - labelWidth);
        const w = Math.max(2, (Number(item.task.end_seconds) - Number(item.task.start_seconds)) / (end - start || 1) * (width - labelWidth));
        const y = top + lane * laneHeight + 5;
        out.push(`<rect class="bar ${{esc(item.task.status)}}" data-index="${{item.index}}" data-resource="${{esc(item.resource)}}" x="${{x}}" y="${{y}}" width="${{w}}" height="20" rx="3"/>`);
      }});
      chart.innerHTML = out.join("");
      chart.querySelectorAll(".bar").forEach(rect => {{
        rect.addEventListener("mousemove", event => {{
          const task = tasks[Number(rect.dataset.index)];
          tooltip.style.display = "block";
          tooltip.style.left = event.clientX + 14 + "px";
          tooltip.style.top = event.clientY + 14 + "px";
          tooltip.innerHTML = `
            <b>${{esc(task.demand)}}</b><br>
            工序：${{esc(task.operation)}}<br>
            资源：${{esc(rect.dataset.resource)}}<br>
            数量：${{esc(task.quantity)}}<br>
            状态：${{esc(task.status)}}<br>
            开始：${{dateLabel(task.start_seconds)}}<br>
            结束：${{dateLabel(task.end_seconds)}}
          `;
        }});
        rect.addEventListener("mouseleave", () => tooltip.style.display = "none");
      }});
    }}
    draw();
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build static Gantt HTML from SQLite schedule_tasks")
    parser.add_argument("--case", default="job_schedule")
    parser.add_argument("--db", default="")
    parser.add_argument("--version-id", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    case_dir = ROOT / "case_data" / args.case
    db_path = Path(args.db) if args.db else case_dir / "planning_runtime.sqlite"
    output = Path(args.output) if args.output else case_dir / "table_gantt.html"
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    version_id = args.version_id or latest_version(conn)
    version = load_version(conn, version_id)
    tasks = load_tasks(conn, version_id)
    output.write_text(render_html(tasks, version, read_plan_start(conn)), encoding="utf-8")
    print(f"version_id={version_id}")
    print(f"tasks={len(tasks)}")
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
