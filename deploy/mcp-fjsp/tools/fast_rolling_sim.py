#!/usr/bin/env python3
"""快速滚动排产模拟 - 精简版"""
import urllib.request, json, ssl, base64, time
from collections import Counter

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

BASE = "https://omni-oss.czy3d.com:18370/ccapps/scenario5"

def api(method, path, data=None):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, method=method)
    auth = base64.b64encode(b"admin:admin").decode()
    req.add_header("Authorization", f"Basic {auth}")
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.data = data.encode()
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read())

# ===== Step 1: 查当前状态 =====
print("=" * 60)
print("Step 1: 查当前状态")
print("=" * 60)

params = api("GET", "/api/common/parameter/?format=json")
for p in params:
    if p["name"] == "currentdate":
        print(f"  排程时间(currentdate): {p['value']}")

wo = api("GET", "/api/input/workorder/?format=json&limit=10000")
c = Counter(w["status"] for w in wo)
print(f"  工单总数: {len(wo)}")
for s in sorted(c):
    print(f"    {s}: {c[s]}")

locked = sum(c[s] for s in ("completed","confirmed","closed") if s in c)
proposed = c.get("proposed", 0)
print(f"  → 锁定区: {locked} | 待重排: {proposed}")

# 记录重排前 proposed 工单指纹
before_proposed = {}
for w in wo:
    if w["status"] == "proposed":
        before_proposed[w["reference"]] = {
            "startdate": w.get("startdate",""),
            "enddate": w.get("enddate",""),
            "resources": tuple(sorted(r.get("resource","") for r in w.get("resources",[]))),
        }

# 记录锁定工单指纹
before_locked = {}
for w in wo:
    if w["status"] in ("confirmed","completed","closed"):
        before_locked[w["reference"]] = {
            "status": w["status"],
            "startdate": w.get("startdate",""),
            "enddate": w.get("enddate",""),
            "resources": tuple(sorted(r.get("resource","") for r in w.get("resources",[]))),
        }

# ===== Step 2: 触发 Direct GA 重排 =====
print()
print("=" * 60)
print("Step 2: 触发 Direct GA 重排")
print("=" * 60)

resp = api("POST", "/execute/api/runplan/", "constraint=capa,mfg_lt,po_lt&plantype=1&env=supply=1,CCAPPS_OPTIMIZER=ga")
task_id = resp.get("taskid")
print(f"  任务ID: {task_id}")

# 轮询状态
for i in range(30):
    time.sleep(2)
    status = api("GET", f"/execute/api/status/?id={task_id}")
    task = status.get(str(task_id), {})
    s = task.get("status")
    if s == "Done":
        print(f"  状态: Done ✅")
        print(f"  耗时: {task['started']} → {task['finished']}")
        break
    elif s == "Error":
        print(f"  状态: Error ❌ {task.get('message','')}")
        break
    else:
        print(f"  状态: {s} ...")

# ===== Step 3: 对比验证 =====
print()
print("=" * 60)
print("Step 3: 重排后对比验证")
print("=" * 60)

wo_after = api("GET", "/api/input/workorder/?format=json&limit=10000")
c_after = Counter(w["status"] for w in wo_after)
print(f"  工单总数: {len(wo)} → {len(wo_after)} {'✅' if len(wo)==len(wo_after) else '⚠️'}")
print(f"  状态分布:")
for s in sorted(set(list(c.keys()) + list(c_after.keys()))):
    b = c.get(s, 0)
    a = c_after.get(s, 0)
    print(f"    {s}: {b} → {a} {'✅' if b==a else '⚠️'}")

# 锁定工单变化检查
changed_locked = 0
for ref, b in before_locked.items():
    after = next((w for w in wo_after if w["reference"] == ref), None)
    if after:
        a_res = tuple(sorted(r.get("resource","") for r in after.get("resources",[])))
        if (b["status"] != after.get("status","") or
            b["startdate"] != after.get("startdate","") or
            b["enddate"] != after.get("enddate","") or
            b["resources"] != a_res):
            changed_locked += 1

print(f"  锁定工单变化: {changed_locked} / {len(before_locked)} {'✅ 全部锁定' if changed_locked==0 else '⚠️ 有变化'}")

# proposed 工单变化
after_proposed = {}
for w in wo_after:
    if w["status"] == "proposed":
        after_proposed[w["reference"]] = {
            "startdate": w.get("startdate",""),
            "enddate": w.get("enddate",""),
            "resources": tuple(sorted(r.get("resource","") for r in w.get("resources",[]))),
        }

time_changed = sum(1 for ref,b in before_proposed.items()
                   if after_proposed.get(ref,{}).get("startdate","") != b["startdate"] or
                      after_proposed.get(ref,{}).get("enddate","") != b["enddate"])
res_changed = sum(1 for ref,b in before_proposed.items()
                  if after_proposed.get(ref,{}).get("resources","") != b["resources"])

print(f"  proposed 工单: {len(before_proposed)} → {len(after_proposed)}")
print(f"    时间被调整: {time_changed}")
print(f"    设备被重分配: {res_changed}")

# 最晚完工时间
all_ends_before = sorted([v["enddate"] for v in before_proposed.values() if v["enddate"]])
all_ends_after = sorted([v["enddate"] for v in after_proposed.values() if v["enddate"]])
if all_ends_before and all_ends_after:
    print(f"  最晚结束: {all_ends_before[-1][-8:]} → {all_ends_after[-1][-8:]}")

print()
print("=" * 60)
print("模拟完成")
print("=" * 60)
