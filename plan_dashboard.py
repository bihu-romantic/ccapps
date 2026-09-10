#!/usr/bin/env python3
"""
Simple web dashboard for ccAPPS plan results.
Parses the constrained plan output (tab-separated format) and displays it as HTML.
"""
import http.server
import json
import os

PLAN_FILE = os.path.join(os.path.dirname(__file__), "demo_constrained.xml")


def parse_plan():
    buffers = []
    demands = []
    resources = []
    operations = []

    with open(PLAN_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            kind = parts[0]
            if kind == "BUFFER":
                buffers.append({
                    "name": parts[1],
                    "date": parts[2],
                    "change": float(parts[3]),
                    "onhand": float(parts[4]),
                })
            elif kind == "DEMAND":
                demands.append({
                    "name": parts[1],
                    "due": parts[2],
                    "quantity": float(parts[3]),
                })
            elif kind == "RESOURCE":
                resources.append({
                    "name": parts[1],
                    "date": parts[2],
                    "change": float(parts[3]),
                    "load": float(parts[4]),
                })
            elif kind == "OPERATION":
                operations.append({
                    "name": parts[1],
                    "start": parts[2],
                    "end": parts[3],
                    "quantity": float(parts[4]),
                })

    # Compute summaries
    unique_demands = {}
    for d in demands:
        name = d["name"]
        if name not in unique_demands:
            unique_demands[name] = d

    unique_ops = {}
    for op in operations:
        name = op["name"]
        if name not in unique_ops:
            unique_ops[name] = {"count": 0, "total_qty": 0}
        unique_ops[name]["count"] += 1
        unique_ops[name]["total_qty"] += op["quantity"]

    return {
        "name": "Furniture Factory Manufacturing Demo",
        "current": "2026-05-01",
        "buffers": buffers,
        "demands": list(unique_demands.values()),
        "resources": resources,
        "operations": operations,
        "summary": {
            "buffer_entries": len(buffers),
            "demands": len(unique_demands),
            "resource_entries": len(resources),
            "operations": len(operations),
            "unique_operations": len(unique_ops),
        },
        "unique_ops": unique_ops,
    }


def build_html(data):
    # Operations table
    ops_html = '<table><tr><th>Operation</th><th>Start</th><th>End</th><th>Quantity</th></tr>'
    for op in sorted(data["operations"], key=lambda x: (x["start"], x["name"])):
        ops_html += f'<tr><td>{op["name"]}</td><td>{op["start"]}</td><td>{op["end"]}</td><td>{op["quantity"]:.0f}</td></tr>'
    ops_html += "</table>"

    # Operation summary
    unique_html = '<table><tr><th>Operation</th><th>Orders</th><th>Total Quantity</th></tr>'
    for name, info in sorted(data["unique_ops"].items()):
        unique_html += f'<tr><td>{name}</td><td>{info["count"]}</td><td>{info["total_qty"]:.0f}</td></tr>'
    unique_html += "</table>"

    # Demands
    demands_html = '<table><tr><th>Demand</th><th>Due</th><th>Quantity</th></tr>'
    for d in sorted(data["demands"], key=lambda x: x["due"]):
        demands_html += f'<tr><td>{d["name"]}</td><td>{d["due"][:10]}</td><td>{d["quantity"]:.0f}</td></tr>'
    demands_html += "</table>"

    # Resource loads
    resource_names = sorted(set(r["name"] for r in data["resources"]))
    res_html = '<table><tr><th>Resource</th><th>Date</th><th>Change</th><th>Load</th></tr>'
    for r in data["resources"]:
        res_html += f'<tr><td>{r["name"]}</td><td>{r["date"]}</td><td>{r["change"]:.0f}</td><td>{r["load"]:.0f}</td></tr>'
    res_html += "</table>"

    # Buffer flow (just first 20)
    buf_sample = data["buffers"][:20]
    buf_html = '<table><tr><th>Buffer</th><th>Date</th><th>Change</th><th>On Hand</th></tr>'
    for b in buf_sample:
        cls = "negative" if b["change"] < 0 else ""
        buf_html += f'<tr class="{cls}"><td>{b["name"]}</td><td>{b["date"]}</td><td>{b["change"]:.0f}</td><td>{b["onhand"]:.0f}</td></tr>'
    buf_html += "</table>"

    s = data["summary"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ccAPPS Plan Results</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 20px; background: #f0f2f5; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  h1 {{ color: #1a1a2e; border-bottom: 3px solid #4a90d9; padding-bottom: 10px; }}
  h2 {{ color: #333; margin-top: 30px; }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }}
  .card {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align: center; }}
  .card .value {{ font-size: 2.2em; font-weight: 700; color: #4a90d9; }}
  .card .label {{ font-size: 0.85em; color: #666; margin-top: 5px; text-transform: uppercase; letter-spacing: 0.5px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
  th {{ background: linear-gradient(135deg, #4a90d9, #357abd); color: white; padding: 10px 12px; text-align: left; font-weight: 600; font-size: 0.9em; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
  tr:hover {{ background: #f0f7ff; }}
  tr.negative {{ background: #fff5f5; }}
  .section {{ background: white; border-radius: 10px; padding: 20px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .section-header {{ display: flex; justify-content: space-between; align-items: center; }}
  .badge {{ background: #4a90d9; color: white; border-radius: 12px; padding: 2px 10px; font-size: 0.8em; }}
  .meta {{ color: #888; font-size: 0.9em; margin-bottom: 10px; }}
  .tab-container {{ margin: 20px 0; }}
  .tabs {{ display: flex; gap: 5px; margin-bottom: 0; }}
  .tab {{ padding: 10px 20px; background: #e0e0e0; border: none; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 0.9em; }}
  .tab.active {{ background: white; font-weight: 600; color: #4a90d9; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
</style>
</head>
<body>
<div class="container">
  <h1>Furniture Factory Manufacturing Demo</h1>
  <p class="meta">Constrained MRP Plan &bull; Current date: 2026-05-01 &bull; Calendar: Working Days (Mon-Fri, 8:00-17:00)</p>

  <div class="summary">
    <div class="card"><div class="value">{s['operations']}</div><div class="label">Operation Plans</div></div>
    <div class="card"><div class="value">{s['unique_operations']}</div><div class="label">Unique Operations</div></div>
    <div class="card"><div class="value">{s['demands']}</div><div class="label">Demands</div></div>
    <div class="card"><div class="value">{s['buffer_entries']}</div><div class="label">Buffer Movements</div></div>
    <div class="card"><div class="value">{s['resource_entries']}</div><div class="label">Resource Events</div></div>
  </div>

  <div class="tab-container">
    <div class="tabs">
      <button class="tab active" onclick="showTab('ops')">Operation Plans</button>
      <button class="tab" onclick="showTab('summary')">Operation Summary</button>
      <button class="tab" onclick="showTab('demands')">Demands</button>
      <button class="tab" onclick="showTab('resources')">Resource Load</button>
      <button class="tab" onclick="showTab('buffers')">Buffer Flow</button>
    </div>
  </div>

  <div id="ops" class="tab-content active">
    <div class="section" style="margin-top:0;border-radius:0 8px 8px 8px;">
      <div class="section-header"><h2>Operation Plans</h2><span class="badge">{s['operations']} plans</span></div>
      {ops_html}
    </div>
  </div>

  <div id="summary" class="tab-content">
    <div class="section" style="margin-top:0;border-radius:0 8px 8px 8px;">
      <div class="section-header"><h2>Operations by Type</h2><span class="badge">{s['unique_operations']} types</span></div>
      {unique_html}
    </div>
  </div>

  <div id="demands" class="tab-content">
    <div class="section" style="margin-top:0;border-radius:0 8px 8px 8px;">
      <div class="section-header"><h2>Customer Demands & Forecasts</h2><span class="badge">{s['demands']} demands</span></div>
      {demands_html}
    </div>
  </div>

  <div id="resources" class="tab-content">
    <div class="section" style="margin-top:0;border-radius:0 8px 8px 8px;">
      <div class="section-header"><h2>Resource Load Timeline</h2><span class="badge">{s['resource_entries']} events</span></div>
      {res_html}
    </div>
  </div>

  <div id="buffers" class="tab-content">
    <div class="section" style="margin-top:0;border-radius:0 8px 8px 8px;">
      <div class="section-header"><h2>Buffer Material Flow</h2><span class="badge">{s['buffer_entries']} movements (showing first 20)</span></div>
      {buf_html}
    </div>
  </div>

  <p style="text-align:center;color:#999;margin-top:40px;font-size:0.85em;">
    Generated by ccAPPS MRP Solver &bull; <a href="/api/plan">JSON API</a> &bull; Plan files: <code>demo_constrained.xml</code>, <code>demo_unconstrained.xml</code>
  </p>
</div>
<script>
function showTab(id) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body>
</html>"""


class PlanHandler(http.server.BaseHTTPRequestHandler):
    data = None

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if PlanHandler.data is None:
                PlanHandler.data = parse_plan()
            self.wfile.write(build_html(PlanHandler.data).encode("utf-8"))
        elif self.path == "/api/plan":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if PlanHandler.data is None:
                PlanHandler.data = parse_plan()
            self.wfile.write(json.dumps(PlanHandler.data, indent=2, default=str).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


if __name__ == "__main__":
    PlanHandler.data = parse_plan()
    port = 8080
    print(f"\n  ========================================")
    print(f"  ccAPPS Plan Dashboard")
    print(f"  http://localhost:{port}/")
    print(f"  ========================================\n")
    httpd = http.server.HTTPServer(("0.0.0.0", port), PlanHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
