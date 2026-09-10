#!/usr/bin/env python3
"""将 CCPL protocol 2.0 JSON 转为可直接请求 ccAPPS 的标准数据集。

唯一输入来源是 CCPL 下发的数据包，例如 ``long_2A.json``：

* ``scheduleMaterials`` -> items、buffers（物料主数据、库位、现有库存）。
* ``workSchedules`` -> resources、skills、resourceSkills、calendars、calendarBuckets。
* ``craftProcessNodes`` -> operations、operationMaterials、operationResources。
* ``demandOrders`` -> customers、demands。

脚本构造的排产数据
==================
* 对 CCPL 数据中出现的地点去重，生成地点记录。
* 当物料库存地点与消耗/产出工序地点不同时，构造 ItemDistribution 调拨关系。
* ``planStart``（或 ``plan_options.currentdate``）是可复现排产必须的计划
  起点。它由 CCPL 请求传入，并作为计划日历的起点，而不是服务器的当前时间。
* ``ccpl-item-*``、``ccpl-operation-*`` 和 source ID 是稳定业务标识，不是 Excel 列。

用法：
  venv/bin/python scripts/build_battary_factory_standard.py \
    --input long_2A.json \
    --output long_2A_standard.json
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


# 旧 CCPL 数据包没有明确的计划开始时间字段时的兼容默认值。新请求应在
# long_2A.json 顶层传 ``planStart`` 或 ``plan_options.currentdate``。
PLAN_START = "2026-06-24 08:00:00"


def planning_controls(envelope):
    """Read optional planning controls from a protocol-2.0 CCPL envelope.

    Both camelCase (the external CCPL convention) and snake_case (the
    standard ccAPPS dataset convention) are accepted.  Keep this mapping in
    the converter: the JSON request, rather than process environment, is the
    source of truth for a planning run.
    """
    request = envelope.get("request") or {}
    supplied = envelope.get("plan_options") or envelope.get("planOptions") or {}
    supplied = {**request.get("plan_options", {}), **request.get("planOptions", {}), **supplied}

    def value(snake, camel=None, default=None):
        return supplied.get(snake, supplied.get(camel or snake, envelope.get(snake, envelope.get(camel or snake, default))))

    solver_option = value("solver", default="fas")
    # The preferred public shape is ``solver: {type: ..., objectiveProfile:
    # ...}``. Accept the former string during the transition.
    if isinstance(solver_option, dict):
        solver_name = solver_option.get("type", solver_option.get("name", "fas"))
    else:
        solver_name = solver_option
    plan_start = value("currentdate", "planStart", request.get("planStart", PLAN_START))
    controls = {
        "constraint": value("constraint", default="capa,mfg_lt,po_lt"),
        "plantype": value("plantype", "planType", 1),
        "solver": solver_name,
        "currentdate": plan_start or PLAN_START,
    }
    if controls["solver"] == "ga_multi_obj":
        # Keep the external request vocabulary explicit while the persisted
        # engine parameter uses the C++ solver's established name.
        controls["solver"] = "ga"
    # These are standard ccAPPS Parameters.  Copy only explicit values so
    # global defaults remain available to legacy payloads.
    parameter_names = (
        (("iterationmax", "iterationMax"), "plan.iterationmax"),
        (("resourceiterationmax", "resourceIterationMax"), "plan.resourceiterationmax"),
        ("minimumdelay", "plan.minimumdelay"),
        ("rotateResources", "plan.rotateResources"),
        ("administrativeLeadtime", "plan.administrativeLeadtime"),
        ("autoFenceOperations", "plan.autoFenceOperations"),
    )
    for externals, parameter in parameter_names:
        val = None
        for external in externals if isinstance(externals, tuple) else (externals,):
            val = value(external, external, None)
            if val is not None:
                break
        if val is not None:
            controls[parameter] = val

    # GA is an optional second-stage optimizer.  Keep its controls in a
    # namespace so a client can use the very same planOptions envelope for
    # mrp, fas and ga without leaking GA tuning into regular planning calls.
    ga = supplied.get("ga") or request.get("ga") or {}
    if isinstance(solver_option, dict):
        # Solver-owned settings are clearer for clients.  Keep the optional
        # ga block as a backwards-compatible alias.
        ga = {**ga, **{k: v for k, v in solver_option.items() if k not in ("type", "name")}}
    if not isinstance(ga, dict):
        raise ValueError("planOptions.ga must be a JSON object")
    ga_parameters = (
        ("direct", "ga.direct"),
        ("seed", "ga.seed"),
        ("objectiveProfile", "ga.objective_profile"),
        ("materialMode", "ga.material_mode"),
    )
    for external, parameter in ga_parameters:
        if external in ga:
            controls[parameter] = ga[external]
    return controls

# 工艺工序表中“裸卷芯”至“氦检合格电芯”的物料 ID 为空。以下是由工艺
# 顺序补录的稳定在制品编码；它们不是 Excel 单元格值，必须显式维护。
WIP_BY_NAME = {
    "裸卷芯": "MAT-WIP-001-A42592CF", "预热卷芯": "MAT-WIP-002-0DFD2424",
    "热压卷芯": "MAT-WIP-003-9FB5273F", "合格卷芯": "MAT-WIP-004-872D845F",
    "预焊卷芯": "MAT-WIP-005-06D7D593", "终焊卷芯": "MAT-WIP-006-63C199B4",
    "转接片焊接芯": "MAT-WIP-007-7061777B", "包胶卷芯": "MAT-WIP-008-23D6BB52",
    "入壳电芯": "MAT-WIP-009-D8C2C6D7", "激光焊接电芯": "MAT-WIP-010-CBBDED83",
    "氦检合格电芯": "MAT-WIP-011-54F4869F",
}

# 工作排期表未列出的设备。工艺工序表给出设备名称和所需技能，脚本据此
# 构造默认产能为 1 的受限资源；卷绕和 XRay 已在工作排期表中，无需补录。
MISSING_RESOURCE_BY_OPERATION = {
    "OP-01": ("卷绕机", "卷绕"),
    "OP-02": ("预热隧道炉-01", "通用"), "OP-03": ("全自动热压机-01", "热压"),
    "OP-05": ("超声波预焊机-01", "焊接"), "OP-06": ("超声波终焊机-01", "焊接"),
    "OP-07": ("转接片激光焊机-01", "激光设备操作"), "OP-08": ("全自动包胶机-01", "包胶"),
    "OP-09": ("入壳预点焊一体机-01", "通用"), "OP-11": ("真空氦检仪", "通用"),
    "OP-12": ("电芯真空烘烤箱-01", "通用"),
}

# 工序表的 OP-10 所需技能为“激光设备操作”，而工作排期表中顶盖激光焊接机
# 标为“激光焊接”。为保证资源技能匹配，按工序要求补充该资源技能。
RESOURCE_SKILL_OVERRIDES = {"OP-10": "激光设备操作"}


def build_from_long_json(document):
    """将 CCPL protocol 2.0 的 long_2A 数据转换为内部统一模型。"""
    envelope = document
    controls = planning_controls(envelope)
    raw = envelope.get("data", envelope)
    dataset_id = envelope.get("datasetId") or envelope.get("requestId") or "ccpl_dataset"
    line_id = envelope.get("productionLineId", "")
    stamp = "2026-06-25 00:00:00"
    metadata = {"createdUserId": "ccpl_import", "datasetId": dataset_id,
                "gmtCreated": stamp, "gmtUpdated": stamp, "tenantId": "ccpl"}
    materials = []
    material_by_name = {}
    for row in raw.get("scheduleMaterials", []):
        mid = "mat_" + str(row.get("id") or row.get("materialCode") or len(materials))
        item = {**metadata, "id": mid, "materialCode": row.get("materialCode", row.get("id", mid)),
                "materialName": row.get("materialName", mid), "materialType": row.get("materialType", "WIP"),
                "unitCost": float(row.get("unitCost") or 0), "locationName": row.get("locationName") or "电池工厂二维产线",
                "currentStock": float(row.get("currentStock") or 0), "unitName": row.get("unitName") or "个",
                "remark": row.get("remark", "")}
        materials.append(item); material_by_name[item["materialName"]] = item
    for node in raw.get("craftProcessNodes", []):
        for name in re.split(r"[,，]", "%s,%s" % (node.get("nodeInput", ""), node.get("nodeOutput", ""))):
            name = name.strip()
            if name and name not in material_by_name:
                item = {**metadata, "id": "mat_auto_" + str(len(materials)), "materialCode": name,
                        "materialName": name, "materialType": "WIP", "unitCost": 0,
                        "locationName": "线边库", "currentStock": 0, "unitName": "个", "remark": "CCPL工艺节点补录"}
                materials.append(item); material_by_name[name] = item
    resources, resource_by_id, schedule_by_resource = [], {}, {}
    for row in raw.get("workSchedules", []):
        rid = "res_" + str(row.get("id") or row.get("identifier") or len(resources))
        resource = {**metadata, "id": rid, "resourceCode": row.get("identifier", rid),
                    "resourceName": row.get("resourceName") or row.get("deviceName") or rid,
                    "bindDeviceName": row.get("deviceName", ""), "resourceType": row.get("resourceType", "default"),
                    "skillName": row.get("skillName", "通用"), "locationName": "电池工厂二维产线",
                    "capacity": 1, "efficiency": 1, "isEnabled": 1}
        resources.append(resource); resource_by_id[str(row.get("id") or row.get("identifier"))] = resource
        schedule_by_resource[rid] = row.get("scheduleData") or {}
    operations, operation_materials = [], []
    for seq, node in enumerate(raw.get("craftProcessNodes", []), 1):
        code = node.get("description") or node.get("name") or "OP-%02d" % seq
        resource = resource_by_id.get(str(node.get("equipmentBinding")))
        # Some source workbooks contain a stale equipmentBinding while the
        # process workStation and workScheduleId still agree.  The latter is
        # the schedule actually attached to the process, so use it only when
        # it unambiguously matches the declared workstation.  This keeps an
        # explicit binding authoritative in all normal cases, while repairing
        # the OP-05/OP-06 swap in long_2A without changing the source JSON.
        scheduled_resource = resource_by_id.get(str(node.get("workScheduleId")))
        workstation = node.get("workStation")
        if (scheduled_resource and workstation and
                scheduled_resource.get("resourceName") == workstation and
                resource is not scheduled_resource):
            resource = scheduled_resource
        if resource is None:
            resource = {**metadata, "id": "res_auto_" + str(seq), "resourceCode": node.get("workStation", "resource-%d" % seq),
                        "resourceName": node.get("workStation", "resource-%d" % seq), "resourceType": "default",
                        "skillName": "通用", "locationName": "电池工厂二维产线", "capacity": 1, "efficiency": 1, "isEnabled": 1}
            resources.append(resource)
        duration = node.get("minProcessInterval", "")
        duration_text = str(duration)
        minute_match = re.search(r"([0-9.]+)\s*min", duration_text)
        second_match = re.search(r"([0-9.]+)\s*(?:s|sec|secs|second|seconds)", duration_text, re.I)
        # Keep fractional seconds: e.g. 0.04 min/pcs is 2.4 seconds, not 2.
        seconds = (
            float(minute_match.group(1)) * 60
            if minute_match
            else float(second_match.group(1)) if second_match else 0
        )
        op_id = "op_" + str(node.get("nodeId") or node.get("id") or seq)
        # equipmentCount is the number of parallel machines, not the number a
        # single operation requires. Keep the operation load at one machine.
        resource["capacity"] = max(resource.get("capacity", 1), node.get("equipmentCount") or 1)
        operations.append({**metadata, "id": op_id, "operationCode": code, "operationName": node.get("name", code),
                           "operationType": node.get("productionType", "批次"), "fixedDurationSeconds": 0,
                           "unitDurationSeconds": seconds, "priority": seq, "sequenceNo": seq,
                           "resourceId": resource["id"], "resourceQuantity": 1,
                           "requiredSkillName": resource.get("skillName", "通用")})
        inputs = [x.strip() for x in re.split(r"[,，]", node.get("nodeInput", "")) if x.strip()]
        outputs = [x.strip() for x in re.split(r"[,，]", node.get("nodeOutput", "")) if x.strip()]
        for role, names in (("consume", inputs), ("produce", outputs)):
            for name in names:
                operation_materials.append({**metadata, "id": "%s_%s_%s" % (op_id, role, len(operation_materials)),
                    "operationId": op_id, "materialId": material_by_name[name]["id"], "materialRole": role,
                    "issueTiming": "end" if role == "produce" else "start", "quantity": 1,
                    "priority": 1, "remark": "%s %s" % (role, name)})
    demands = [{**metadata, "id": str(r.get("id") or r.get("demandCode")), "demandCode": r.get("demandCode"),
                "materialId": material_by_name.get(r.get("materialName"), {}).get("id"), "customerName": r.get("customerName"),
                "deliveryLocationName": "暂存仓2", "dueTime": str(r.get("dueDate", "")) + " 18:00:00",
                "quantity": float(r.get("demandQuantity") or 0), "priority": r.get("demandPriority", 10), "status": "OPEN"}
               for r in raw.get("demandOrders", [])]
    calendars = {}
    for resource in resources:
        rows = []
        for key, value in schedule_by_resource.get(resource["id"], {}).items():
            match = re.match(r"date_(\d+)$", str(key))
            if not match:
                continue
            day = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).date().isoformat()
            working = int(str(value) not in ("0", "false", "False", ""))
            rows.append({"calendarDate": day, "isWorkingDay": working,
                         "shiftStartTime": "08:00:00", "shiftEndTime": "18:00:00", "workCapacity": working})
        calendars[resource["id"]] = rows
    return {"scenario": "scenario5", **controls,
            "data": {"dataset": {**metadata, "id": dataset_id}, "defaultLocationName": "电池工厂二维产线",
                    "materials": materials, "resources": resources, "calendarsByResourceId": calendars,
                    "operations": operations, "operationMaterials": operation_materials, "demands": demands}}


def enrich_missing_process_data(data):
    """补齐 Excel 空物料 ID 和缺失设备，形成可闭合的 12 道工序链。"""
    metadata = {"createdUserId": "xlsx_import", "datasetId": data["dataset"]["id"], "gmtCreated": "2026-06-25 00:00:00", "gmtUpdated": "2026-06-25 00:00:00", "tenantId": "battery"}
    data["materials"] = [row for row in data["materials"] if row["id"] != "mat_"]
    material_by_name = {row["materialName"]: row for row in data["materials"]}
    for name, code in WIP_BY_NAME.items():
        if name not in material_by_name:
            row = {**metadata, "id": "mat_" + code.lower().replace("-", "_"), "materialCode": code, "materialName": name, "materialType": "WIP", "unitCost": 0, "locationName": "线边库", "currentStock": 0, "unitName": "个", "remark": "工艺工序表空物料ID补录"}
            data["materials"].append(row)
            material_by_name[name] = row
        # 在制品在连续产线的线边库流转，而非默认生产地点。
        material_by_name[name]["locationName"] = "线边库"
        material_by_name[name]["materialType"] = "WIP"
    for row in data["operationMaterials"]:
        name = row["remark"].rsplit(" ", 1)[-1]
        if name in material_by_name:
            row["materialId"] = material_by_name[name]["id"]

    resources = {row["resourceName"]: row for row in data["resources"]}
    resources_by_id = {row["id"]: row for row in data["resources"]}
    data["resources"] = [row for row in data["resources"] if row["id"] != "res_01"]
    resources = {row["resourceName"]: row for row in data["resources"]}
    for op in data["operations"]:
        addition = MISSING_RESOURCE_BY_OPERATION.get(op["operationCode"])
        # A CCPL JSON node already provides an equipment binding. Preserve it;
        # the fallback table is only for Excel rows with no resolved resource.
        if op.get("resourceId") in resources_by_id:
            continue
        if not addition:
            continue
        name, skill = addition
        resource = resources.get(name)
        if resource is None:
            resource = {**metadata, "id": "res_auto_" + op["operationCode"].lower().replace("-", "_"), "resourceCode": name, "resourceName": name, "bindDeviceName": "", "resourceType": "default", "skillName": skill, "locationName": "电池工厂二维产线", "capacity": 1, "efficiency": 1, "isEnabled": 1, "remark": "工艺工序表自动补齐"}
            data["resources"].append(resource)
            resources[name] = resource
        op["resourceId"] = resource["id"]

    # CCPL 工艺节点没有可靠的在制品转移批量信息，保持默认 start/end
    # 物料流；避免将所有中间品强制拆为单件 transfer_batch。
    flows = data["operationMaterials"]
    for op in data["operations"]:
        # This CCPL line measures all WIP in whole pieces.  The shared supply
        # expander otherwise time-slices a continuous operation at fractional
        # quantities (eg. 127.777...), while the downstream batch process
        # rightfully consumes whole pieces.  Preserve the physical unit by
        # making every executable lot integral; it doesn't force a common
        # batch size and still permits a final partial lot.
        op.setdefault("sizeMultiple", 1)
        if op["operationCode"] == "OP-02":
            # 30 分钟处理 50 件，归一为每件 36 秒，并保留批量约束。
            # 50 倍数是预热隧道炉的业务硬约束，不可放开。
            op["unitDurationSeconds"] = op.get("unitDurationSeconds", 0) / 50
            op["fixedDurationSeconds"] = 0
            op.update(sizeMinimum=50, sizeMultiple=50)
            op.pop("sizeMaximum", None)
        if op["operationCode"] == "OP-12":
            # 烘烤时长按 128 件批量归一为每件时长，批量由 size 字段表达。
            op["unitDurationSeconds"] = op.get("unitDurationSeconds", 0) / 128
            op["fixedDurationSeconds"] = 0
            # 最后一批允许不足 128 件，仅限制单批最大装载量。
            op.pop("sizeMinimum", None)
            op["sizeMultiple"] = 1
            op["sizeMaximum"] = 128
            for row in flows:
                if row["operationId"] == op["id"]:
                    row["quantity"] = 1


def build_standard(ccpl):
    """将 CCPL JSON 统一模型转换为 ccAPPS 原生模型记录。"""
    enrich_missing_process_data(ccpl["data"])
    data = ccpl["data"]
    # Excel 使用“产成品”，旧 CCPL 解析器只识别“成品”；统一为标准 FG 类型。
    for material in data["materials"]:
        if material.get("materialType") == "产成品" or material.get("materialCode") == "MAT-PROD-01":
            material["materialType"] = "FG"
    source = data["dataset"]["id"]
    default_location = data["defaultLocationName"]
    item_names = data.get("itemNames", {})
    resource_names = data.get("resourceNames", {})

    # The CCPL identifier is a long opaque hash.  It is useful as a source
    # key, but unusable as a Gantt row label.  Prefix the physical resource
    # with the process code(s) that use it, keeping the human device name in
    # the same stable planner identifier.
    operation_codes_by_resource = {}
    for operation in data["operations"]:
        resource_id = operation.get("resourceId")
        if resource_id:
            operation_codes_by_resource.setdefault(resource_id, []).append(
                operation["operationCode"]
            )

    def item_name(row):
        return item_names.get(row["id"], "ccpl-item-%s" % row["materialCode"])

    def resource_name(row):
        configured = resource_names.get(row["id"])
        if configured:
            return configured
        codes = sorted(set(operation_codes_by_resource.get(row["id"], [])))
        prefix = "/".join(codes) if codes else "RESOURCE"
        return "%s | %s" % (prefix, row.get("resourceName") or row["resourceCode"])

    def operation_name(row):
        return "ccpl-operation-%s" % row["operationCode"]

    materials = {row["id"]: row for row in data["materials"]}
    # 只保留被工序引用的资源：剔除「工作排期」表里工艺未使用的操作工(EMP-*:
    # 张工/李工/…)和多余设备(如 NTH2-ZJPHJ-01 激光焊接机,工序 OP-10 实际
    # 用的是顶盖激光焊接机 NTH2-DGGH-01)。否则它们会作为游离 resource 入库,
    # 还白白占用一份工作日历。
    used_resource_ids = {op.get("resourceId") for op in data["operations"] if op.get("resourceId")}
    data["resources"] = [r for r in data["resources"] if r["id"] in used_resource_ids]
    resources_by_id = {row["id"]: row for row in data["resources"]}
    operation_by_id = {row["id"]: row for row in data["operations"]}
    locations = {}

    def add_location(name):
        name = name or default_location
        locations.setdefault(name, {"name": name, "description": name, "source": source})
        return name

    add_location(default_location)

    # 来源：工作排期。日历名称和星期标记为 ccAPPS 存储字段；工作簿提供资源每日班次值。
    calendars, calendar_buckets = [], []
    calendar_rows = data.get("calendarsByResourceId", {})
    # 当前基准仅为工作排期中正式编码的设备/人员建立日历；工艺补充的
    # 卷绕资源和 XRay 资源按默认可用资源处理，不额外绑定工作日历。
    calendar_resource_ids = {
        row["id"] for row in data["resources"]
        if row["id"] not in {"res_01", "res_xray_01"}
    }
    for resource_id, rows in calendar_rows.items():
        if resource_id not in calendar_resource_ids:
            continue
        resource = next(row for row in data["resources"] if row["id"] == resource_id)
        calendar = "ccpl-calendar-%s" % resource_name(resource)
        calendars.append({"name": calendar, "description": "Resource calendar %s" % resource_id, "defaultvalue": 0, "source": source})
        for priority, row in enumerate(rows):
            if not row.get("calendarDate"):
                continue
            calendar_buckets.append({
                "calendar": calendar, "startdate": row["calendarDate"], "enddate": row["calendarDate"],
                "starttime": row.get("shiftStartTime", "00:00:00"), "endtime": row.get("shiftEndTime", "23:59:59"),
                "value": row.get("workCapacity", 1), "priority": priority,
                "monday": True, "tuesday": True, "wednesday": True, "thursday": True,
                "friday": True, "saturday": True, "sunday": True, "source": source,
            })
    calendar_names = {row["name"] for row in calendars}
    # 工序引用但「工作排期」未排班的设备(工艺工序表补充的卷绕资源、热压机、
    # 焊机、XRay、烘烤箱等)沿用产线标准白班,否则它们会被当作 24/7 可用、
    # 排程偏乐观。班次模板取自任一已排班资源(产线班次一致)。
    line_shift_template = next(
        (rows for rows in calendar_rows.values()
         if any(b.get("calendarDate") for b in rows)),
        [],
    )
    for row in data["resources"]:
        cal = "ccpl-calendar-%s" % resource_name(row)
        if cal not in calendar_names and line_shift_template:
            calendars.append({"name": cal, "description": "产线标准班次(08:00-18:00)", "defaultvalue": 0, "source": source})
            for priority, b in enumerate(x for x in line_shift_template if x.get("calendarDate")):
                calendar_buckets.append({
                    "calendar": cal, "startdate": b["calendarDate"], "enddate": b["calendarDate"],
                    "starttime": b.get("shiftStartTime", "08:00:00"), "endtime": b.get("shiftEndTime", "18:00:00"),
                    "value": b.get("workCapacity", 1), "priority": priority,
                    "monday": True, "tuesday": True, "wednesday": True, "thursday": True,
                    "friday": True, "saturday": True, "sunday": True, "source": source,
                })
            calendar_names.add(cal)

    # 来源：物料表，以及解析工艺工序时补出的在制品物料。
    items, buffers = [], []
    for row in data["materials"]:
        location = add_location(row.get("locationName"))
        name = item_name(row)
        items.append({"name": name, "description": row["materialName"], "category": row["materialType"], "cost": row.get("unitCost", 0), "uom": row.get("unitName"), "source": source})
        buffers.append({"description": "%s @ %s" % (name, location), "item": name, "location": location, "batch": "", "onhand": row.get("currentStock", 0), "source": source})

    # 来源：工作排期，以及工艺工序引用但排期表缺失时补出的资源。
    skill_names = {row.get("skillName") for row in data["resources"] if row.get("skillName")}
    skill_names.update(row.get("requiredSkillName") for row in data["operations"] if row.get("requiredSkillName"))
    skills = [{"name": name, "source": source} for name in sorted(skill_names)]
    standard_resources, resource_skills = [], []
    for row in data["resources"]:
        location = add_location(row.get("locationName"))
        name = resource_name(row)
        calendar = "ccpl-calendar-%s" % name
        # CCPL resourceType (eg. "processor") is a business classification,
        # not a ccAPPS resource implementation type.  Keep it as a skill
        # through resourceSkills, while importing the physical capacity as a
        # standard finite resource that the planning engine recognizes.
        standard_resources.append({"name": name, "description": row["resourceName"], "type": "default", "maximum": row.get("capacity", 1), "efficiency": row.get("efficiency", 1) * 100, "constrained": bool(row.get("isEnabled", 1)), "location": location, "available": calendar if calendar in calendar_names else None, "source": source})
        if row.get("skillName"):
            resource_skills.append({"resource": name, "skill": row["skillName"], "priority": 1, "effective_start": "1971-01-01T00:00:00", "effective_end": "2030-12-31T00:00:00", "source": source})

    # 来源：工艺工序。工序的产出物料决定该工序的计划物料和地点。
    produced = {row["operationId"]: row["materialId"] for row in data["operationMaterials"] if row["materialRole"] == "produce"}
    operations, operation_resources = [], []
    for row in data["operations"]:
        output_id = produced.get(row["id"])
        resource = resources_by_id.get(row.get("resourceId"))
        location = add_location(materials[output_id]["locationName"] if output_id else resource.get("locationName") if resource else default_location)
        operations.append({"name": operation_name(row), "type": "time_per", "description": row["operationName"], "item": item_name(materials[output_id]) if output_id else None, "location": location, "priority": row.get("sequenceNo") or row.get("priority", 1), "duration": row.get("fixedDurationSeconds") or None, "duration_per": row.get("unitDurationSeconds") or None, "sizeminimum": row.get("sizeMinimum", 1), "sizemultiple": row.get("sizeMultiple"), "sizemaximum": row.get("sizeMaximum"), "source": source})
        if resource:
            skill = RESOURCE_SKILL_OVERRIDES.get(row["operationCode"], row.get("requiredSkillName"))
            operation_resources.append({"operation": operation_name(row), "resource": resource_name(resource), "skill": skill, "quantity": row.get("resourceQuantity", 1), "priority": 1, "effective_start": "1971-01-01T00:00:00", "effective_end": "2030-12-31T00:00:00", "source": source})
            if skill and skill not in skill_names:
                skill_names.add(skill)
                skills.append({"name": skill, "source": source})
            if skill and not any(x["resource"] == resource_name(resource) and x["skill"] == skill for x in resource_skills):
                resource_skills.append({"resource": resource_name(resource), "skill": skill, "priority": 1, "effective_start": "1971-01-01T00:00:00", "effective_end": "2030-12-31T00:00:00", "source": source})
    operation_locations = {row["name"]: row["location"] for row in operations}

    operation_materials, distributions = [], {}
    for row in data["operationMaterials"]:
        operation = operation_by_id[row["operationId"]]
        material = materials[row["materialId"]]
        operation_location = operation_locations[operation_name(operation)]
        stock_location = add_location(material.get("locationName"))
        quantity = float(row.get("quantity", 1))
        if row["materialRole"] == "consume":
            quantity = -quantity
        # Continuous-line WIP stays at the line-side buffer.  A downstream
        # operation can physically be located elsewhere (eg. final storage)
        # but its real-time input is consumed directly from the line, rather
        # than creating a proposed DO for every fresh upstream batch.  DOs
        # remain appropriate for externally supplied raw material and for
        # explicitly imported historical/in-transit inventory.
        flow_location = (
            stock_location
            if quantity < 0 and material.get("materialType") == "WIP"
            else operation_location
        )
        operation_materials.append({"operation": operation_name(operation), "item": item_name(material), "location": flow_location, "quantity": quantity, "type": row.get("issueTiming", "start"), "transferbatch": row.get("transferBatch"), "offset": row.get("offsetSeconds"), "priority": row.get("priority", 1), "effective_start": "1971-01-01T00:00:00", "effective_end": "2030-12-31T00:00:00", "source": source})
        # 构造：将库存调入工序地点，或将工序产出调回库存地点。
        if stock_location != flow_location:
            origin, destination = (stock_location, operation_location) if quantity < 0 else (operation_location, stock_location)
            distributions[(item_name(material), origin, destination)] = {"item": item_name(material), "origin": origin, "location": destination, "leadtime": 0, "cost": 0, "priority": 1, "source": source}

    # 来源：订单需求表；交付地点“暂存仓2”是当前产线的建模默认值。
    customers = {}
    demands = []
    for row in data["demands"]:
        customer = row.get("customerName") or "ccpl-customer"
        customers.setdefault(customer, {"name": customer, "description": customer, "source": source})
        location = add_location(row.get("deliveryLocationName") or materials[row["materialId"]]["locationName"])
        demands.append({"name": row.get("demandCode") or row["id"], "description": row.get("remark"), "customer": customer, "item": item_name(materials[row["materialId"]]), "location": location, "due": row["dueTime"], "status": row.get("status", "open").lower(), "quantity": row["quantity"], "priority": row.get("priority", 10), "source": source})

    parameters = [
        {"name": "currentdate", "value": ccpl.get("currentdate", PLAN_START)},
        {"name": "plan.solver", "value": ccpl.get("solver", "fas")},
    ]
    for name in (
        "plan.iterationmax", "plan.resourceiterationmax", "plan.minimumdelay",
        "plan.rotateResources", "plan.administrativeLeadtime",
        "plan.autoFenceOperations", "ga.direct", "ga.seed",
        "ga.objective_profile", "ga.material_mode",
    ):
        if name in ccpl:
            parameters.append({"name": name, "value": str(ccpl[name])})
    return {"version": "1.0", "scenario": ccpl["scenario"], "source": source, "mode": "replace_source", "clean_all": True, "parameters": parameters, "plan_options": {"constraint": ccpl["constraint"], "plantype": ccpl["plantype"], "solver": ccpl.get("solver", "fas")}, "data": {"locations": list(locations.values()), "calendars": calendars, "calendarBuckets": calendar_buckets, "items": items, "buffers": buffers, "skills": skills, "resources": standard_resources, "resourceSkills": resource_skills, "operations": operations, "operationResources": operation_resources, "operationMaterials": operation_materials, "itemDistributions": list(distributions.values()), "customers": list(customers.values()), "demands": demands}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="long_2A.json")
    parser.add_argument("--output", default="long_2A_standard.json")
    args = parser.parse_args()
    if Path(args.input).suffix.lower() != ".json":
        parser.error("--input must be a CCPL JSON data package")
    document = build_standard(
        build_from_long_json(json.loads(Path(args.input).read_text(encoding="utf-8")))
    )
    Path(args.output).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote %s" % args.output)


if __name__ == "__main__":
    main()
