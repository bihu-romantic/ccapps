#!/usr/bin/env python3
"""Export a scenario schedule to a self-contained interactive HTML file."""

import argparse
import json
import os
from datetime import datetime, timedelta
from html import escape

import psycopg2
from psycopg2.extras import RealDictCursor


def rows(cursor, sql, params=()):
    cursor.execute(sql, params)
    result = []
    for row in cursor.fetchall():
        clean = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                value = value.isoformat(sep=" ", timespec="seconds")
            elif hasattr(value, "as_tuple"):
                value = float(value)
            clean[key] = value
        result.append(clean)
    return result


def parse_dt(value):
    return datetime.fromisoformat(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="ccapps6")
    parser.add_argument("--scenario", default="场景6")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--window-start",
        help="Utilization / timeline start ISO datetime (default: currentdate)",
    )
    parser.add_argument(
        "--window-end",
        help="Plan / timeline end ISO datetime (default: currentdate + 3 days)",
    )
    parser.add_argument(
        "--util-days",
        type=int,
        default=3,
        help="Days used for resource utilization chips (from window-start)",
    )
    args = parser.parse_args()
    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "ccapps"),
        password=os.environ.get("POSTGRES_PASSWORD", "ccapps"),
        dbname=args.database,
    )
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        current = rows(
            cur, "select value from common_parameter where name='currentdate'"
        )[0]["value"]
        window_start = parse_dt(args.window_start) if args.window_start else parse_dt(
            str(current).replace(" ", "T")
        )
        if args.window_end:
            window_end = parse_dt(args.window_end)
        else:
            window_end = window_start + timedelta(days=3)
        util_end = window_start + timedelta(days=args.util_days)
        horizon_hours = max(
            24.0, (window_end - window_start).total_seconds() / 3600.0
        )
        orders = rows(
            cur,
            """
            select d.name as order_no,
                   coalesce(d.category,'') as category,
                   d.item_id as item,
                   coalesce(i.description,'') as item_name,
                   d.quantity, d.plannedquantity as planned,
                   d.due, d.deliverydate,
                   case when d.plannedquantity >= d.quantity - 0.00001 then '完全排满'
                        when d.plannedquantity > 0.00001 then '部分满足'
                        else '未排' end as result
            from demand d left join item i on i.name=d.item_id
            where d.status in ('open','quote')
            order by d.due,d.name
            """,
        )
        assignments = rows(
            cur,
            """
            select r.category, r.name as resource,
                   coalesce(r.subcategory,'') as resource_group,
                   r.type as resource_type, o.reference,
                   o.operation_id as operation, coalesce(op.item_id,o.item_id,'') as item,
                   coalesce(i.description,'') as item_name,
                   o.quantity, opr.quantity as resource_qty,
                   o.startdate as start, o.enddate as finish
            from operationplanresource opr
            join operationplan o on o.reference=opr.operationplan_id
            join resource r on r.name=opr.resource_id
            left join operation op on op.name=o.operation_id
            left join item i on i.name=coalesce(op.item_id,o.item_id)
            where o.type='MO' and o.status='proposed'
              and r.category in ('设备','人员','模具')
            order by r.category,r.name,o.startdate,o.reference
            """,
        )
        summaries = rows(
            cur,
            """
            select r.category, r.name as resource, r.type as resource_type,
                   r.maximum,
                   coalesce(sum(rp.available),0) as available,
                   coalesce(sum(rp.load),0) as load,
                   case when coalesce(sum(rp.available),0)>0 and r.type<>'infinite'
                        then 100*sum(rp.load)/sum(rp.available) else null end as utilization
            from resource r
            left join out_resourceplan rp on rp.resource=r.name
              and rp.startdate >= %s
              and rp.startdate < %s
            where r.category in ('设备','人员','模具')
            group by r.category,r.name,r.type,r.maximum
            having coalesce(sum(rp.load),0)>0
            order by r.category,r.name
            """,
            (window_start, util_end),
        )
    conn.close()

    payload = json.dumps(
        {
            "orders": orders,
            "assignments": assignments,
            "summaries": summaries,
            "origin": window_start.isoformat(sep="T", timespec="seconds"),
            "horizon_hours": horizon_hours,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    full = sum(1 for r in orders if r["result"] == "完全排满")
    partial = sum(1 for r in orders if r["result"] == "部分满足")
    unplanned = len(orders) - full - partial
    title = f"{args.scenario} 排产结果"
    window_label = (
        f"{window_start.strftime('%Y-%m-%d %H:%M')} 至 "
        f"{window_end.strftime('%Y-%m-%d %H:%M')}"
    )
    html = (
        TEMPLATE.replace("__TITLE__", escape(title))
        .replace("__CURRENT__", escape(str(current)))
        .replace("__WINDOW__", escape(window_label))
        .replace("__TOTAL__", str(len(orders)))
        .replace("__FULL__", str(full))
        .replace("__PARTIAL__", str(partial))
        .replace("__UNPLANNED__", str(unplanned))
        .replace("__DATA__", payload)
    )
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(
        json.dumps(
            {
                "output": args.output,
                "orders": len(orders),
                "full": full,
                "partial": partial,
                "unplanned": unplanned,
                "assignments": len(assignments),
                "equipment": sum(1 for x in assignments if x["category"] == "设备"),
                "people": sum(1 for x in assignments if x["category"] == "人员"),
                "molds": sum(1 for x in assignments if x["category"] == "模具"),
                "window": window_label,
            },
            ensure_ascii=False,
        )
    )


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
:root{--bg:#f4f7fb;--card:#fff;--ink:#182230;--muted:#667085;--line:#e5eaf1;--blue:#2563eb;--green:#16a34a;--amber:#d97706;--red:#dc2626}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:24px 28px 18px;background:linear-gradient(120deg,#172554,#1d4ed8);color:#fff}h1{margin:0 0 5px;font-size:25px}.sub{opacity:.82}
.wrap{padding:18px 24px 30px}.kpis{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin-bottom:16px}.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.kpi b{display:block;font-size:24px}.kpi span{color:var(--muted)}
.tabs{display:flex;gap:8px;margin:0 0 12px}.tab{border:1px solid var(--line);background:#fff;padding:9px 18px;border-radius:8px;cursor:pointer}.tab.active{background:var(--blue);color:#fff;border-color:var(--blue)}
.panel{display:none}.panel.active{display:block}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}.toolbar input,.toolbar select{border:1px solid #ccd5e1;border-radius:7px;padding:8px 10px;background:#fff;min-width:180px}
.box{background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}.scroll{overflow:auto;max-height:68vh}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}th{position:sticky;top:0;background:#f8fafc;z-index:2;color:#475467}tr:hover td{background:#f8fbff}.num{text-align:right}.muted{color:var(--muted)}
.badge{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px}.full{background:#dcfce7;color:#166534}.partial{background:#fef3c7;color:#92400e}.none{background:#fee2e2;color:#991b1b}
.timeline{position:relative;width:240px;height:18px;background:repeating-linear-gradient(90deg,#eef2f7 0,#eef2f7 79px,#dbe2ea 80px);border-radius:4px;overflow:hidden}.bar{position:absolute;top:3px;height:12px;min-width:2px;border-radius:3px;background:#3b82f6}.设备 .bar{background:#2563eb}.人员 .bar{background:#16a34a}.模具 .bar{background:#9333ea}
.pager{display:flex;justify-content:space-between;align-items:center;padding:10px}.pager button{border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 12px;cursor:pointer}.summary{display:flex;gap:8px;overflow:auto;margin-bottom:10px}.chip{background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 10px;white-space:nowrap}.chip b{margin-left:6px}
@media(max-width:760px){.wrap{padding:12px}.kpis{grid-template-columns:repeat(2,1fr)}.timeline{width:150px}.tabs{overflow:auto}}
@media print{.toolbar,.pager,.tabs{display:none}.panel{display:block!important}.scroll{max-height:none;overflow:visible}th{position:static}}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sub">计划时点 __CURRENT__ · 排程窗口 __WINDOW__ · 独立离线文件</div></header>
<main class="wrap"><section class="kpis"><div class="kpi"><b>__TOTAL__</b><span>订单总数</span></div><div class="kpi"><b>__FULL__</b><span>完全排满</span></div><div class="kpi"><b>__PARTIAL__</b><span>部分满足</span></div><div class="kpi"><b>__UNPLANNED__</b><span>完全未排</span></div></section>
<nav class="tabs"><button class="tab active" data-tab="订单">订单</button><button class="tab" data-tab="设备">设备</button><button class="tab" data-tab="人员">人员</button><button class="tab" data-tab="模具">模具</button></nav>
<section id="订单" class="panel active"><div class="toolbar"><input class="search" placeholder="搜索订单、物料、类别或名称"><select class="status"><option value="">全部状态</option><option>完全排满</option><option>部分满足</option><option>未排</option></select><select class="category"><option value="">全部类别</option><option>预警</option><option>未入库</option><option>备库</option></select></div><div class="box"><div class="scroll"><table><thead><tr><th>订单</th><th>类别</th><th>物料</th><th>物料名称</th><th class="num">需求</th><th class="num">已排</th><th>结果</th><th>交期</th><th>计划完成</th></tr></thead><tbody></tbody></table><div class="pager"></div></div></div></section>
<section id="设备" class="panel resource"></section><section id="人员" class="panel resource"></section><section id="模具" class="panel resource"></section>
</main><script>const DATA=__DATA__;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>Number(n||0).toLocaleString('zh-CN',{maximumFractionDigits:1}); const dt=s=>s?String(s).replace('T',' ').slice(0,16):'—';
const origin=new Date(DATA.origin.includes('+')||DATA.origin.endsWith('Z')?DATA.origin:DATA.origin+'+08:00'), horizon=(DATA.horizon_hours||72)*3600e3;
function timeline(a,b,cat){let l=Math.max(0,Math.min(100,(new Date(a)-origin)/horizon*100)),r=Math.max(l,Math.min(100,(new Date(b)-origin)/horizon*100));return `<div class="timeline ${cat}" title="${dt(a)} — ${dt(b)}"><i class="bar" style="left:${l}%;width:${Math.max(.6,r-l)}%"></i></div>`}
function pager(el,count,page,set){let pages=Math.max(1,Math.ceil(count/100));el.innerHTML=`<span>共 ${count.toLocaleString()} 条 · 第 ${page}/${pages} 页</span><span><button ${page<=1?'disabled':''} data-p="${page-1}">上一页</button> <button ${page>=pages?'disabled':''} data-p="${page+1}">下一页</button></span>`;el.querySelectorAll('button').forEach(b=>b.onclick=()=>set(+b.dataset.p))}
function orderView(){const p=document.querySelector('#订单'),q=p.querySelector('.search'),s=p.querySelector('.status'),c=p.querySelector('.category'),tb=p.querySelector('tbody'),pg=p.querySelector('.pager');let page=1;function draw(np=1){page=np;let term=q.value.trim().toLowerCase(),list=DATA.orders.filter(x=>(!s.value||x.result===s.value)&&(!c.value||x.category===c.value)&&(!term||`${x.order_no} ${x.category} ${x.item} ${x.item_name}`.toLowerCase().includes(term)));tb.innerHTML=list.slice((page-1)*100,page*100).map(x=>`<tr><td>${esc(x.order_no)}</td><td>${esc(x.category||'—')}</td><td>${esc(x.item)}</td><td>${esc(x.item_name)}</td><td class="num">${fmt(x.quantity)}</td><td class="num">${fmt(x.planned)}</td><td><span class="badge ${x.result==='完全排满'?'full':x.result==='部分满足'?'partial':'none'}">${x.result}</span></td><td>${dt(x.due)}</td><td>${dt(x.deliverydate)}</td></tr>`).join('');pager(pg,list.length,page,draw)}q.oninput=s.onchange=c.onchange=()=>draw(1);draw()}
function resourceView(cat){let root=document.getElementById(cat),summary=DATA.summaries.filter(x=>x.category===cat).sort((a,b)=>(b.load||0)-(a.load||0)),all=DATA.assignments.filter(x=>x.category===cat);root.innerHTML=`<div class="summary">${summary.slice(0,20).map(x=>`<div class="chip">${esc(x.resource)}<b>${x.utilization==null?fmt(x.load)+'负荷':fmt(x.utilization)+'%'}</b></div>`).join('')}</div><div class="toolbar"><input class="search" placeholder="搜索资源、工序、物料或批次"><select class="res"><option value="">全部资源</option>${[...new Set(all.map(x=>x.resource))].map(x=>`<option>${esc(x)}</option>`).join('')}</select></div><div class="box"><div class="scroll"><table><thead><tr><th>${cat}</th><th>批次</th><th>工序</th><th>物料</th><th>物料名称</th><th class="num">生产数量</th><th>开始</th><th>结束</th><th>时间轴</th></tr></thead><tbody></tbody></table><div class="pager"></div></div></div>`;let q=root.querySelector('.search'),sel=root.querySelector('.res'),tb=root.querySelector('tbody'),pg=root.querySelector('.pager');function draw(page=1){let term=q.value.trim().toLowerCase(),list=all.filter(x=>(!sel.value||x.resource===sel.value)&&(!term||`${x.resource} ${x.reference} ${x.operation} ${x.item} ${x.item_name}`.toLowerCase().includes(term)));tb.innerHTML=list.slice((page-1)*100,page*100).map(x=>`<tr><td>${esc(x.resource)}</td><td>${esc(x.reference)}</td><td>${esc(x.operation)}</td><td>${esc(x.item)}</td><td>${esc(x.item_name)}</td><td class="num">${fmt(x.quantity)}</td><td>${dt(x.start)}</td><td>${dt(x.finish)}</td><td>${timeline(x.start,x.finish,cat)}</td></tr>`).join('');pager(pg,list.length,page,draw)}q.oninput=sel.onchange=()=>draw(1);draw()}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')});orderView();['设备','人员','模具'].forEach(resourceView);
</script></body></html>'''


if __name__ == "__main__":
    main()
