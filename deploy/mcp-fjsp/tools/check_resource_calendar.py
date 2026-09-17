#!/usr/bin/env python3
"""
查找 xlsx 数据文件中指定设备的日历/维修信息。

用法:
    python3 check_resource_calendar.py <xlsx路径> [设备名]
    python3 check_resource_calendar.py data.xlsx M058
    python3 check_resource_calendar.py data.xlsx          # 不传设备名则列出所有有自定义日历的设备

纯标准库实现，不依赖 openpyxl。
"""

import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ── xlsx 解析（inline string 兼容）──────────────────────────

NS = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
NS_R = {'r': 'http://schemas.openxmlformats.org/package/2006/relationships'}
NS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _excel_date(serial):
    """Excel 序列号 → 日期字符串"""
    try:
        serial = float(serial)
    except (ValueError, TypeError):
        return str(serial)
    # Excel 日期基准: 1900-01-01 = 1 (含闰年 bug)
    base = datetime(1899, 12, 30)
    dt = base + timedelta(days=serial)
    # 如果有小数部分，包含时间
    if serial != int(serial):
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    return dt.strftime('%Y-%m-%d')


def _parse_xlsx(path):
    """解析 xlsx，返回 {sheet_name: (headers, rows)}"""
    z = zipfile.ZipFile(path)

    # sheet 名 → rId
    wb_tree = ET.parse(z.open('xl/workbook.xml'))
    sheet_map = {}
    for s in wb_tree.findall('.//main:sheet', NS):
        sheet_map[s.get('name')] = s.get(f'{{{NS_REL}}}id')

    # rId → 文件路径
    rels_tree = ET.parse(z.open('xl/_rels/workbook.xml.rels'))
    rel_map = {}
    for rel in rels_tree.findall('.//r:Relationship', NS_R):
        rel_map[rel.get('Id')] = rel.get('Target')

    result = {}
    for sheet_name, rid in sheet_map.items():
        target = rel_map.get(rid, '')
        if not target.startswith('/'):
            target = 'xl/' + target
        else:
            target = target.lstrip('/')
        try:
            tree = ET.parse(z.open(target))
        except KeyError:
            continue

        rows = []
        for row in tree.findall('.//main:row', NS):
            cells = {}
            max_col = 0
            for c in row.findall('main:c', NS):
                ref = c.get('r', '')
                col = 0
                for ch in ref:
                    if ch.isalpha():
                        col = col * 26 + (ord(ch.upper()) - ord('A') + 1)
                max_col = max(max_col, col)
                t = c.get('t', '')
                val = ''
                if t == 'inlineStr':
                    is_el = c.find('main:is', NS)
                    if is_el is not None:
                        texts = is_el.findall('.//main:t', NS)
                        val = ''.join(x.text or '' for x in texts)
                else:
                    v = c.find('main:v', NS)
                    val = v.text if v is not None else ''
                cells[col] = val
            rows.append([cells.get(i + 1, '') for i in range(max_col)])

        if rows:
            result[sheet_name] = (rows[0], rows[1:])
        else:
            result[sheet_name] = ([], [])

    return result


# ── 业务逻辑 ────────────────────────────────────────────────

def find_resource(sheets, resource_name):
    """在资源表中查找指定设备，返回字段字典"""
    if '资源' not in sheets:
        return None
    headers, rows = sheets['资源']
    for row in rows:
        if row and row[0] == resource_name:
            return {h: v for h, v in zip(headers, row) if v}
    return None


def find_all_custom_calendars(sheets):
    """列出所有有自定义[可用]日历的设备"""
    if '资源' not in sheets:
        return []
    headers, rows = sheets['资源']
    avail_idx = None
    name_idx = 0
    for i, h in enumerate(headers):
        if h == '可用':
            avail_idx = i
        if h == '名称':
            name_idx = i

    if avail_idx is None:
        return []

    result = []
    for row in rows:
        if len(row) > avail_idx and row[avail_idx] and row[avail_idx] != 'None':
            result.append((row[name_idx], row[avail_idx]))
    return result


def find_calendar(sheets, cal_name):
    """在日历表中查找指定日历，返回字段字典"""
    if '日历' not in sheets:
        return None
    headers, rows = sheets['日历']
    for row in rows:
        if row and row[0] == cal_name:
            return {h: v for h, v in zip(headers, row) if v}
    return None


def find_calendar_buckets(sheets, cal_name):
    """在日历-排班表中查找指定日历的所有排班记录"""
    if '日历-排班表' not in sheets:
        return []
    headers, rows = sheets['日历-排班表']
    cal_idx = 0
    for i, h in enumerate(headers):
        if h == 'Calendar':
            cal_idx = i
            break

    buckets = []
    for row in rows:
        if len(row) > cal_idx and row[cal_idx] == cal_name:
            buckets.append({h: v for h, v in zip(headers, row) if v})
    return buckets


def format_bucket(b):
    """格式化排班表记录"""
    parts = []
    if '开始日期' in b:
        parts.append(f"日期: {_excel_date(b['开始日期'])} ~ {_excel_date(b.get('结束日期', b['开始日期']))}")
    if '值' in b:
        val = b['值']
        meaning = '可用' if val == '1' else '不可用(停机)' if val == '0' else f'值={val}'
        parts.append(f"值: {val} ({meaning})")
    if '优先级' in b:
        parts.append(f"优先级: {b['优先级']}")
    if '开始时间' in b and b.get('开始时间'):
        parts.append(f"每日时段: {b.get('开始时间', '')} ~ {b.get('结束时间', '')}")
    weekdays = []
    for wd in ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']:
        if b.get(wd) == '1':
            weekdays.append(wd.replace('星期', ''))
    if weekdays:
        parts.append(f"生效日: {','.join(weekdays)}")
    return ' | '.join(parts)


# ── 主流程 ──────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    xlsx_path = sys.argv[1]
    resource_name = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"文件: {xlsx_path}")
    print(f"设备: {resource_name or '(全部有自定义日历的设备)'}")
    print("=" * 70)

    sheets = _parse_xlsx(xlsx_path)

    if resource_name:
        # 查找指定设备
        res = find_resource(sheets, resource_name)
        if not res:
            print(f"❌ 资源表中未找到设备: {resource_name}")
            sys.exit(1)

        print(f"\n【资源信息】")
        for k, v in res.items():
            print(f"  {k}: {v}")

        avail_cal = res.get('可用', '')
        if not avail_cal or avail_cal == 'None':
            print(f"\n⚠️  设备 {resource_name} 未设置自定义[可用]日历（继承父节点）")
        else:
            print(f"\n【可用日历: {avail_cal}】")
            cal = find_calendar(sheets, avail_cal)
            if cal:
                for k, v in cal.items():
                    print(f"  {k}: {v}")
            else:
                print(f"  ⚠️  日历表中未找到: {avail_cal}")

            buckets = find_calendar_buckets(sheets, avail_cal)
            if buckets:
                print(f"\n【排班表记录 ({len(buckets)} 条)】")
                for i, b in enumerate(buckets, 1):
                    print(f"  [{i}] {format_bucket(b)}")
            else:
                print(f"\n  排班表中无记录（使用日历默认值）")

            # 如果默认值=0 且无排班表记录，说明设备全程不可用
            if cal and cal.get('默认值') == '0' and not buckets:
                print(f"\n  ⚠️  日历默认值=0 且无排班表覆盖 → 设备全程不可用！")
            elif cal and cal.get('默认值') == '0' and buckets:
                # 检查是否有值=1的排班记录（恢复可用的时间段）
                has_recovery = any(b.get('值') == '1' for b in buckets)
                if not has_recovery:
                    print(f"\n  ⚠️  日历默认值=0 且排班表无[值=1]的恢复记录 → 设备全程不可用！")

        # 额外：查找该设备关联的其他日历（如 M058_HALF）
        if '日历' in sheets:
            cal_headers, cal_rows = sheets['日历']
            related = []
            for row in cal_rows:
                if row and row[0] and resource_name in row[0]:
                    related.append(row[0])
            if related:
                print(f"\n【设备相关的其他日历】")
                for cal_name in related:
                    cal = find_calendar(sheets, cal_name)
                    buckets = find_calendar_buckets(sheets, cal_name)
                    print(f"\n  日历: {cal_name}")
                    if cal:
                        print(f"  默认值: {cal.get('默认值', '?')}")
                    if buckets:
                        for b in buckets:
                            print(f"    {format_bucket(b)}")
    else:
        # 列出所有有自定义日历的设备
        devices = find_all_custom_calendars(sheets)
        if not devices:
            print("没有设备设置自定义[可用]日历")
        else:
            print(f"\n【有自定义日历的设备 ({len(devices)} 台)】")
            print(f"{'设备':<10} {'可用日历':<35} 日历默认值  排班记录数")
            print("-" * 80)
            for dev, cal_name in devices:
                cal = find_calendar(sheets, cal_name)
                default_val = cal.get('默认值', '?') if cal else '?'
                buckets = find_calendar_buckets(sheets, cal_name)
                print(f"{dev:<10} {cal_name:<35} {default_val:<12} {len(buckets)} 条")


if __name__ == '__main__':
    main()
