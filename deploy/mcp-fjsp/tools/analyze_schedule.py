#!/usr/bin/env python3
"""
通用排程结果分析工具
======================
支持所有场景（scenario2/5/6…），可从 API 下载结果或直接分析本地 xlsx。

用法:
  # 方式1: 从公网 API 下载并分析（指定场景号）
  python3 analyze_schedule.py --scenario 2
  python3 analyze_schedule.py --scenario 5

  # 方式2: 分析本地 xlsx 文件
  python3 analyze_schedule.py --file fjsp_results.xlsx

  # 方式3: 检查特定设备（如 M058 是否被排了工单）
  python3 analyze_schedule.py --scenario 2 --device M058

  # 方式4: 只看简要摘要
  python3 analyze_schedule.py --file fjsp_results.xlsx --brief

  # 方式5: 输出 JSON 格式（方便其他脚本调用）
  python3 analyze_schedule.py --file fjsp_results.xlsx --json

依赖: 纯标准库，无需安装任何第三方包
"""
import argparse, json, ssl, sys, zipfile, re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

# ============================================================
#  xlsx 读取（纯标准库，不依赖 openpyxl）
# ============================================================

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
NSMAP = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RNS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _col_to_idx(col):
    """A→0, B→1, Z→25, AA→26 …"""
    idx = 0
    for c in col:
        idx = idx * 26 + (ord(c) - 64)
    return idx - 1


def read_xlsx(path):
    """读取 xlsx，返回 {sheet_name: [[row0], [row1], ...]}"""
    z = zipfile.ZipFile(path)
    # 共享字符串
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.findall('main:si', NSMAP):
            txt = ''.join(t.text or '' for t in si.iter(NS + 't'))
            shared.append(txt)
    # 工作表名 → 文件路径映射
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    sheets = []
    for s in wb.findall('main:sheets/main:sheet', NSMAP):
        sheets.append((s.get('name'), s.get('{%s}id' % RNS)))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    rel_map = {r.get('Id'): r.get('Target') for r in rels}
    # 逐表读取
    result = {}
    for name, rid in sheets:
        target = rel_map.get(rid, '')
        spath = 'xl/' + target if not target.startswith('/') else target.lstrip('/')
        if spath not in z.namelist():
            spath = target
        if spath not in z.namelist():
            continue
        root = ET.fromstring(z.read(spath))
        rows_out = []
        for row in root.iter(NS + 'row'):
            cells = {}
            maxc = 0
            for c in row.findall('main:c', NSMAP):
                ref = c.get('r', '')
                m = re.match(r'[A-Z]+', ref)
                ci = _col_to_idx(m.group()) if m else 0
                maxc = max(maxc, ci)
                t = c.get('t')
                v = c.find('main:v', NSMAP)
                val = v.text if v is not None else ''
                if t == 's' and val != '':
                    val = shared[int(val)]
                elif t == 'inlineStr':
                    is_el = c.find('main:is', NSMAP)
                    if is_el is not None:
                        val = ''.join(x.text or '' for x in is_el.iter(NS + 't'))
                cells[ci] = val
            rows_out.append([cells.get(i, '') for i in range(maxc + 1)])
        result[name] = rows_out
    z.close()
    return result


def excel_to_dt(serial):
    """Excel 序列号 → datetime"""
    try:
        s = float(serial)
    except (ValueError, TypeError):
        return None
    if s == 0 or s < 40000:  # 跳过 0 和哨兵值
        return None
    return datetime(1899, 12, 30) + timedelta(days=s)


# ============================================================
#  API 下载
# ============================================================

def download_results(scenario, artifact='results.xlsx'):
    """从公网 API 下载结果文件，返回本地临时文件路径"""
    base = f'http://192.168.50.125:30002/ccapps/scenario{scenario}'
    url = f'{base}/execute/fjsp/artifacts/{artifact}'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = Request(url)
    import base64
    auth = base64.b64encode(b'admin:admin').decode()
    req.add_header('Authorization', f'Basic {auth}')
    local = f'fjsp_results_s{scenario}.xlsx' if artifact == 'results.xlsx' else f'fjsp_visual_s{scenario}.html'
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            with open(local, 'wb') as f:
                f.write(resp.read())
        return local
    except URLError as e:
        print(f'❌ 下载失败: {e}', file=sys.stderr)
        return None


# ============================================================
#  分析逻辑
# ============================================================

def _find_col(header, *candidates):
    """在表头中查找列索引，支持多个候选名"""
    for i, h in enumerate(header):
        for cand in candidates:
            if cand in str(h):
                return i
    return None


def analyze(data, device=None):
    """
    分析排程结果，返回 dict。
    data: read_xlsx() 的返回值
    device: 可选，指定设备名做专项检查
    """
    result = {}

    # ---- 排程总览 ----
    overview = data.get('排程总览', [])
    if overview and len(overview) > 1:
        hdr = overview[0]
        rows = overview[1:]

        idx_type = _find_col(hdr, '类型')
        idx_status = _find_col(hdr, '状态')
        idx_start = _find_col(hdr, '开始时间')
        idx_end = _find_col(hdr, '结束时间')
        idx_delay = _find_col(hdr, '延迟')
        idx_name = _find_col(hdr, '名称')
        idx_parent = _find_col(hdr, '父工单')

        type_count = Counter()
        status_count = Counter()
        delays = []
        start_serials = []
        end_serials = []
        max_end_serial = None
        max_end_row = None

        for r in rows:
            if idx_type is not None and idx_type < len(r):
                type_count[r[idx_type]] += 1
            if idx_status is not None and idx_status < len(r):
                status_count[r[idx_status]] += 1
            if idx_start is not None and idx_start < len(r):
                try:
                    s = float(r[idx_start])
                    if s > 40000:
                        start_serials.append(s)
                except (ValueError, TypeError):
                    pass
            if idx_end is not None and idx_end < len(r):
                try:
                    e = float(r[idx_end])
                    if e > 40000:
                        end_serials.append(e)
                        if max_end_serial is None or e > max_end_serial:
                            max_end_serial = e
                            max_end_row = r
                except (ValueError, TypeError):
                    pass
            if idx_delay is not None and idx_delay < len(r):
                try:
                    delays.append(float(r[idx_delay]))
                except (ValueError, TypeError):
                    pass

        late = [d for d in delays if d > 0]
        on_time = [d for d in delays if d <= 0]
        early = [d for d in delays if d < 0]

        min_start = min(start_serials) if start_serials else None
        max_end = max(end_serials) if end_serials else None

        result['overview'] = {
            'total_rows': len(rows),
            'type_distribution': dict(type_count),
            'status_distribution': dict(status_count),
            'earliest_start': excel_to_dt(min_start).strftime('%Y-%m-%d %H:%M:%S') if min_start else None,
            'latest_end': excel_to_dt(max_end).strftime('%Y-%m-%d %H:%M:%S') if max_end else None,
            'makespan_hours': round((excel_to_dt(max_end) - excel_to_dt(min_start)).total_seconds() / 3600, 2) if min_start and max_end else None,
            'latest_end_workorder': max_end_row[idx_name] if max_end_row and idx_name is not None and idx_name < len(max_end_row) else None,
            'delay': {
                'total_with_delay_record': len(delays),
                'on_time': len(on_time),
                'early': len(early),
                'late': len(late),
                'max_late_days': round(max(late), 4) if late else 0,
                'max_late_minutes': round(max(late) * 24 * 60, 1) if late else 0,
                'max_early_days': round(abs(min(early)), 4) if early else 0,
            },
        }

    # ---- 设备占用 ----
    equip = data.get('设备占用', [])
    if equip and len(equip) > 1:
        ehdr = equip[0]
        erows = equip[1:]

        eidx_dev = _find_col(ehdr, '设备', 'resource')
        eidx_start = _find_col(ehdr, '开始时间')
        eidx_end = _find_col(ehdr, '结束时间')
        eidx_status = _find_col(ehdr, '工单状态', '状态')

        dev_load = Counter()
        dev_intervals = defaultdict(list)

        for r in erows:
            dev = r[eidx_dev] if eidx_dev is not None and eidx_dev < len(r) else ''
            if dev:
                dev_load[dev] += 1
            if eidx_start is not None and eidx_end is not None:
                try:
                    s = float(r[eidx_start]) if eidx_start < len(r) else 0
                    e = float(r[eidx_end]) if eidx_end < len(r) else 0
                    if s > 40000 and e > 40000:
                        wo = r[0] if len(r) > 0 else ''
                        st = r[eidx_status] if eidx_status is not None and eidx_status < len(r) else ''
                        dev_intervals[dev].append((s, e, wo, st))
                except (ValueError, TypeError):
                    pass

        # 重叠检查
        overlap_count = 0
        overlap_examples = []
        for dev, ivs in dev_intervals.items():
            ivs.sort()
            for i in range(len(ivs)):
                for j in range(i + 1, len(ivs)):
                    if ivs[i][1] > ivs[j][0] + 1e-9:
                        overlap_count += 1
                        if len(overlap_examples) < 5:
                            overlap_examples.append({
                                'device': dev,
                                'wo1': ivs[i][2],
                                'wo1_time': f'{excel_to_dt(ivs[i][0])} → {excel_to_dt(ivs[i][1])}',
                                'wo2': ivs[j][2],
                                'wo2_time': f'{excel_to_dt(ivs[j][0])} → {excel_to_dt(ivs[j][1])}',
                            })

        sorted_devs = dev_load.most_common()

        result['equipment'] = {
            'total_records': len(erows),
            'device_count': len(dev_load),
            'top_busy': sorted_devs[:10],
            'top_idle': sorted_devs[-10:][::-1] if len(sorted_devs) > 10 else [],
            'overlap_count': overlap_count,
            'overlap_examples': overlap_examples,
        }

        # 特定设备检查
        if device:
            dev_count = dev_load.get(device, 0)
            result['device_check'] = {
                'device': device,
                'workorder_count': dev_count,
                'status': '✅ 停机日历生效，排程器未给该设备排工单' if dev_count == 0 else f'⚠️ 该设备仍有 {dev_count} 个工单，停机日历可能未生效',
            }

    # ---- 物料流 ----
    material = data.get('物料流', [])
    if material and len(material) > 1:
        mhdr = material[0]
        mrows = material[1:]
        midx_status = _find_col(mhdr, '状态')
        mat_status = Counter()
        for r in mrows:
            if midx_status is not None and midx_status < len(r):
                mat_status[r[midx_status]] += 1
        result['material'] = {
            'total_records': len(mrows),
            'status_distribution': dict(mat_status),
        }

    # ---- 问题列表 ----（暂时注释掉，需要时取消注释）
    # problems = data.get('问题列表', [])
    # if problems and len(problems) > 1:
    #     phdr = problems[0]
    #     prows = problems[1:]
    #     pidx_type = _find_col(phdr, '类型', 'problem', '问题')
    #     prob_types = Counter()
    #     for r in prows:
    #         if pidx_type is not None and pidx_type < len(r):
    #             prob_types[r[pidx_type]] += 1
    #     result['problems'] = {
    #         'total': len(prows),
    #         'type_distribution': dict(prob_types.most_common()),
    #         'header': phdr,
    #     }

    return result


# ============================================================
#  报告打印
# ============================================================

def print_report(result, brief=False):
    """格式化打印分析结果"""
    lines = []
    w = lines.append

    w('=' * 64)
    w('  排程结果分析报告')
    w('=' * 64)

    # 排程总览
    ov = result.get('overview')
    if ov:
        w('')
        w('┌─ 排程总览 ─────────────────────────────────────────────')
        w(f'│  工单总数: {ov["total_rows"]}')
        w(f'│  类型分布: {ov["type_distribution"]}')
        w(f'│  状态分布: {ov["status_distribution"]}')
        if ov['earliest_start']:
            w(f'│  最早开始: {ov["earliest_start"]}')
        if ov['latest_end']:
            w(f'│  最晚结束: {ov["latest_end"]}  (工单: {ov.get("latest_end_workorder", "?")})')
        if ov['makespan_hours'] is not None:
            w(f'│  排程跨度: {ov["makespan_hours"]} 小时')
        d = ov['delay']
        w(f'│  延迟记录: {d["total_with_delay_record"]} 条')
        w(f'│    按时完成: {d["on_time"]} (其中提前 {d["early"]})')
        w(f'│    延期工单: {d["late"]}')
        if d['late'] > 0:
            w(f'│    最大延期: {d["max_late_days"]} 天 ({d["max_late_minutes"]} 分钟)')
        if d['max_early_days'] > 0:
            w(f'│    最大提前: {d["max_early_days"]} 天')
        w('└' + '─' * 58)

    if brief:
        w('')
        w('(简要模式，省略设备/物料/问题详情)')
        print('\n'.join(lines))
        return

    # 设备占用
    eq = result.get('equipment')
    if eq:
        w('')
        w('┌─ 设备占用 ─────────────────────────────────────────────')
        w(f'│  占用记录: {eq["total_records"]} 条')
        w(f'│  参与设备: {eq["device_count"]} 台')
        w(f'│  重叠数:   {eq["overlap_count"]}')
        if eq['overlap_count'] > 0:
            w('│  重叠示例:')
            for ex in eq['overlap_examples']:
                w(f'│    {ex["device"]}: {ex["wo1"]} ({ex["wo1_time"]}) ↔ {ex["wo2"]} ({ex["wo2_time"]})')
        w('│')
        w('│  最忙设备 TOP 10:')
        for dev, cnt in eq['top_busy']:
            bar = '█' * min(cnt, 30)
            w(f'│    {dev:>8s}: {cnt:>4d} {bar}')
        if eq['top_idle']:
            w('│  最闲设备 TOP 10:')
            for dev, cnt in eq['top_idle']:
                w(f'│    {dev:>8s}: {cnt:>4d}')
        w('└' + '─' * 58)

    # 特定设备检查
    dc = result.get('device_check')
    if dc:
        w('')
        w('┌─ 设备专项检查 ─────────────────────────────────────────')
        w(f'│  设备: {dc["device"]}')
        w(f'│  工单数: {dc["workorder_count"]}')
        w(f'│  结论: {dc["status"]}')
        w('└' + '─' * 58)

    # 物料流
    mat = result.get('material')
    if mat:
        w('')
        w('┌─ 物料流 ───────────────────────────────────────────────')
        w(f'│  记录数: {mat["total_records"]}')
        w(f'│  状态分布: {mat["status_distribution"]}')
        w('└' + '─' * 58)

    # 问题列表（暂时注释掉，需要时取消注释）
    # prob = result.get('problems')
    # if prob:
    #     w('')
    #     w('┌─ 问题列表 ─────────────────────────────────────────────')
    #     w(f'│  问题总数: {prob["total"]}')
    #     if prob['type_distribution']:
    #         w('│  类型分布:')
    #         for ptype, cnt in prob['type_distribution'].items():
    #             w(f'│    {ptype}: {cnt}')
    #     else:
    #         w('│  (无问题)')
    #     w('└' + '─' * 58)

    # 总结
    w('')
    w('=' * 64)
    if ov and eq:
        d = ov['delay']
        if d['late'] == 0 and eq['overlap_count'] == 0:
            w('  ✅ 全部按时完成，无延期、无重叠')
        else:
            parts = []
            if d['late'] > 0:
                parts.append(f'延期 {d["late"]}')
            if eq['overlap_count'] > 0:
                parts.append(f'重叠 {eq["overlap_count"]}')
            w(f'  ⚠️ 存在: {" / ".join(parts)}')
    w('=' * 64)

    print('\n'.join(lines))


# ============================================================
#  主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='通用排程结果分析工具（支持所有场景）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python3 analyze_schedule.py --scenario 2          # 从场景2下载并分析
  python3 analyze_schedule.py --file results.xlsx   # 分析本地文件
  python3 analyze_schedule.py --scenario 2 --device M058  # 检查M058是否被排工单
  python3 analyze_schedule.py --file results.xlsx --json   # JSON输出
        ''',
    )
    parser.add_argument('--scenario', type=str, help='场景号（如 2, 5, 6），从公网API下载结果')
    parser.add_argument('--file', type=str, help='本地 xlsx 文件路径')
    parser.add_argument('--device', type=str, default=None, help='专项检查某设备（如 M058）')
    parser.add_argument('--brief', action='store_true', help='只输出简要摘要')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式（方便其他脚本调用）')
    args = parser.parse_args()

    # 确定数据来源
    if args.file:
        xlsx_path = args.file
    elif args.scenario:
        print(f'正在从场景{args.scenario}下载结果文件…')
        xlsx_path = download_results(args.scenario)
        if not xlsx_path:
            sys.exit(1)
        print(f'已下载: {xlsx_path}')
    else:
        # 默认尝试当前目录的 fjsp_results.xlsx
        import os
        if os.path.exists('fjsp_results.xlsx'):
            xlsx_path = 'fjsp_results.xlsx'
        else:
            parser.error('请指定 --scenario 或 --file，或将文件命名为 fjsp_results.xlsx')

    # 读取并分析
    data = read_xlsx(xlsx_path)
    result = analyze(data, device=args.device)

    # 输出
    if args.json:
        # JSON 模式需要把 Counter/datetime 等序列化友好化
        def clean(obj):
            if isinstance(obj, dict):
                return {k: clean(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [clean(v) for v in obj]
            return obj
        print(json.dumps(clean(result), ensure_ascii=False, indent=2, default=str))
    else:
        print_report(result, brief=args.brief)


if __name__ == '__main__':
    main()
