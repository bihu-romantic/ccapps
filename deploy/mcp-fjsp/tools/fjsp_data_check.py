#!/usr/bin/env python3
"""
FJSP 车间排产基础数据检查工具（标准化版）

用法:
    python3 fjsp_data_check.py <xlsx文件路径> [--json] [--verbose]

功能:
    1. 13 张表完整性检查
    2. 各表数据行数统计
    3. 外键完整性校验（13 条引用关系）
    4. 必填字段检查
    5. 交期日期范围与排程可行性
    6. 数据类型校验（数量/日期是否合法）

不依赖 openpyxl，纯标准库实现（zipfile + xml）。
退出码: 0 = 全部通过, 1 = 有错误
"""

import argparse
import datetime
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

# ============================================================
# 常量定义
# ============================================================

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
RNS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
ns = {'main': NS}

# 13 张表定义: (sheet名, 是否必填)
EXPECTED_SHEETS = [
    ('地点', True),
    ('日历', True),
    ('日历-排班表', True),
    ('客户', True),
    ('对象', True),
    ('库存', False),
    ('技能', True),
    ('资源', True),
    ('资源技能', True),
    ('操作', True),
    ('操作-物料', True),
    ('操作-资源', True),
    ('销售订单', True),
]

# 外键关系定义: (子表, 子表列名, 父表, 父表列名)
FK_RELATIONS = [
    ('销售订单', '对象', '对象', '名称'),
    ('销售订单', '客户', '客户', '名称'),
    ('销售订单', '地点', '地点', '名称'),
    ('资源', '地点', '地点', '名称'),
    ('资源', '可用', '日历', '名称'),
    ('资源技能', '资源', '资源', '名称'),
    ('资源技能', '技能', '技能', '名称'),
    ('操作', '地点', '地点', '名称'),
    ('操作', '对象', '对象', '名称'),
    ('操作-物料', '操作', '操作', '名称'),
    ('操作-物料', '对象', '对象', '名称'),
    ('操作-资源', '操作', '操作', '名称'),
    ('操作-资源', '技能', '技能', '名称'),
]

# 必填字段定义: {表名: [列名, ...]}
REQUIRED_FIELDS = {
    '销售订单': ['对象', '数量', '交期'],
    '资源': ['名称', '地点'],
    '操作': ['名称', '类型', '地点'],
    '操作-物料': ['操作', '对象', '数量'],
    '操作-资源': ['操作', '技能', '数量'],
}

# 推荐填写字段（空值给 WARN 而非 FAIL）: {表名: [列名, ...]}
RECOMMENDED_FIELDS = {
    '资源': ['可用'],  # 日历引用，可继承父节点，但建议显式填写
}

# 数值型字段: {表名: [列名, ...]}
NUMERIC_FIELDS = {
    '销售订单': ['数量'],
    '操作-物料': ['数量'],
    '操作-资源': ['数量'],
    '库存': ['现有库存量'],
    '资源': ['最大'],
}


# ============================================================
# XLSX 读取器（纯标准库，不依赖 openpyxl）
# ============================================================

class XlsxReader:
    """读取 xlsx 文件，返回 {sheet名: (headers, rows)}"""

    def __init__(self, filepath):
        self.filepath = filepath
        self._z = None
        self._sheets_info = []
        self._rel_map = {}
        self._shared_strings = []
        self._cache = {}
        self._open()

    def _open(self):
        if not os.path.exists(self.filepath):
            raise FileNotFoundError("文件不存在: %s" % self.filepath)
        self._z = zipfile.ZipFile(self.filepath)

        # workbook.xml -> sheet names
        wb_xml = ET.fromstring(self._z.read('xl/workbook.xml'))
        for s in wb_xml.findall('.//main:sheet', ns):
            self._sheets_info.append({
                'name': s.attrib['name'],
                'r_id': s.attrib.get('{%s}id' % RNS)
            })

        # rels -> file paths
        rels_xml = ET.fromstring(self._z.read('xl/_rels/workbook.xml.rels'))
        for rel in rels_xml:
            self._rel_map[rel.attrib['Id']] = rel.attrib['Target'].lstrip('/')

        # sharedStrings (may not exist)
        try:
            ss_xml = ET.fromstring(self._z.read('xl/sharedStrings.xml'))
            for si in ss_xml.findall('.//main:si', ns):
                texts = [t.text for t in si.findall('.//main:t', ns) if t.text]
                self._shared_strings.append(''.join(texts))
        except KeyError:
            pass

    def sheet_names(self):
        return [s['name'] for s in self._sheets_info]

    def read(self, sheet_name):
        """返回 (headers, data_rows)，data_rows 是 list of dict {col_idx: value}"""
        if sheet_name in self._cache:
            return self._cache[sheet_name]

        info = None
        for s in self._sheets_info:
            if s['name'] == sheet_name:
                info = s
                break
        if not info:
            self._cache[sheet_name] = (None, None)
            return (None, None)

        target = self._rel_map.get(info['r_id'])
        if not target:
            self._cache[sheet_name] = (None, None)
            return (None, None)

        try:
            sheet_xml = ET.fromstring(self._z.read(target))
        except Exception:
            self._cache[sheet_name] = (None, None)
            return (None, None)

        rows = sheet_xml.findall('.//main:row', ns)
        all_rows = []
        for row in rows:
            cells = {}
            for c in row.findall('main:c', ns):
                ref = c.attrib.get('r', '')
                col = ''
                for ch in ref:
                    if ch.isalpha():
                        col += ch
                    else:
                        break
                col_idx = self._col_letter_to_idx(col) if col else 0
                t = c.attrib.get('t', '')
                val = None
                v_elem = c.find('main:v', ns)
                if v_elem is not None:
                    if t == 's':
                        idx = int(v_elem.text)
                        val = self._shared_strings[idx] if idx < len(self._shared_strings) else None
                    else:
                        val = v_elem.text
                if t == 'inlineStr':
                    is_elem = c.find('main:is', ns)
                    if is_elem is not None:
                        texts = [t2.text for t2 in is_elem.findall('.//main:t', ns) if t2.text]
                        val = ''.join(texts)
                cells[col_idx] = val
            all_rows.append(cells)

        if not all_rows:
            self._cache[sheet_name] = ([], [])
            return ([], [])

        max_col = max(max(r.keys()) for r in all_rows if r)
        headers = [all_rows[0].get(i, '') for i in range(1, max_col + 1)]
        data_rows = all_rows[1:]
        self._cache[sheet_name] = (headers, data_rows)
        return (headers, data_rows)

    @staticmethod
    def _col_letter_to_idx(col):
        idx = 0
        for ch in col:
            idx = idx * 26 + (ord(ch) - ord('A') + 1)
        return idx

    @staticmethod
    def get_val(row, headers, col_name):
        """从一行数据中按列名取值"""
        if col_name not in headers:
            return None
        idx = headers.index(col_name) + 1
        return row.get(idx)

    def close(self):
        if self._z:
            self._z.close()


# ============================================================
# 日期工具
# ============================================================

def excel_serial_to_date(val):
    """Excel 日期序列号 -> datetime"""
    if not val:
        return None
    try:
        return datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(val))
    except (ValueError, TypeError):
        return None


# ============================================================
# 检查器
# ============================================================

class DataChecker:
    """对 xlsx 执行标准化检查，返回结构化结果"""

    def __init__(self, reader):
        self.reader = reader
        self.results = []
        self.error_count = 0
        self.warn_count = 0

    def _add(self, category, check_name, status, detail, errors=None):
        """status: PASS / FAIL / WARN / INFO"""
        entry = {
            'category': category,
            'check': check_name,
            'status': status,
            'detail': detail,
        }
        if errors:
            entry['errors'] = errors
        self.results.append(entry)
        if status == 'FAIL':
            self.error_count += 1
        elif status == 'WARN':
            self.warn_count += 1

    def run_all(self):
        self.check_table_completeness()
        self.check_row_counts()
        self.check_foreign_keys()
        self.check_required_fields()
        self.check_recommended_fields()
        self.check_numeric_fields()
        self.check_due_dates()
        return self.results

    # --- 检查 1: 表完整性 ---
    def check_table_completeness(self):
        actual = set(self.reader.sheet_names())
        missing = []
        for name, required in EXPECTED_SHEETS:
            if name not in actual:
                if required:
                    missing.append(name)
                else:
                    self._add('表完整性', name, 'WARN',
                              '可选表缺失: %s' % name)
        if missing:
            self._add('表完整性', '13张表', 'FAIL',
                      '缺少必填表: %s' % ', '.join(missing), missing)
        else:
            self._add('表完整性', '13张表', 'PASS',
                      '13张表全部齐全')

    # --- 检查 2: 各表行数 ---
    def check_row_counts(self):
        details = []
        empty_required = []
        for name, required in EXPECTED_SHEETS:
            headers, rows = self.reader.read(name)
            count = len(rows) if rows else 0
            details.append('%s: %d行' % (name, count))
            if required and count == 0:
                empty_required.append(name)
        if empty_required:
            self._add('数据行数', '必填表空表', 'FAIL',
                      '以下必填表无数据: %s' % ', '.join(empty_required),
                      empty_required)
        else:
            self._add('数据行数', '各表行数', 'PASS',
                      ' | '.join(details))

    # --- 检查 3: 外键完整性 ---
    def check_foreign_keys(self):
        all_pass = True
        error_details = []

        for child_table, child_col, parent_table, parent_col in FK_RELATIONS:
            ch_headers, ch_rows = self.reader.read(child_table)
            pa_headers, pa_rows = self.reader.read(parent_table)

            if ch_headers is None or pa_headers is None:
                continue

            # 收集父表主键集合
            parent_keys = set()
            for row in pa_rows:
                v = XlsxReader.get_val(row, pa_headers, parent_col)
                if v:
                    parent_keys.add(str(v).strip())

            # 检查子表外键
            bad = 0
            bad_samples = []
            for row in ch_rows:
                v = XlsxReader.get_val(row, ch_headers, child_col)
                if v and str(v).strip() not in parent_keys:
                    bad += 1
                    if len(bad_samples) < 3:
                        bad_samples.append(str(v).strip())

            relation = '%s.%s -> %s.%s' % (child_table, child_col, parent_table, parent_col)
            if bad > 0:
                all_pass = False
                error_details.append('%s: %d个无效引用 (示例: %s)' % (relation, bad, ', '.join(bad_samples)))
                self._add('外键完整性', relation, 'FAIL',
                          '%d个无效引用' % bad, bad_samples)
            else:
                self._add('外键完整性', relation, 'PASS', '0个无效引用')

        if all_pass:
            self._add('外键完整性', '全部外键', 'PASS',
                      '13条外键关系全部通过')

    # --- 检查 4: 必填字段 ---
    def check_required_fields(self):
        for table, fields in REQUIRED_FIELDS.items():
            headers, rows = self.reader.read(table)
            if headers is None:
                continue
            missing_fields = [f for f in fields if f not in headers]
            if missing_fields:
                self._add('必填字段', table, 'FAIL',
                          '表头缺少必填列: %s' % ', '.join(missing_fields),
                          missing_fields)
                continue

            empty_counts = {}
            for field in fields:
                empty = 0
                for row in rows:
                    v = XlsxReader.get_val(row, headers, field)
                    if not v or str(v).strip() == '':
                        empty += 1
                if empty > 0:
                    empty_counts[field] = empty

            if empty_counts:
                detail = ', '.join('%s为空%d行' % (k, v) for k, v in empty_counts.items())
                self._add('必填字段', table, 'FAIL', detail, empty_counts)
            else:
                self._add('必填字段', table, 'PASS',
                          '%s: %d行, 必填字段全部有值' % (table, len(rows)))

    # --- 检查 4b: 推荐字段 ---
    def check_recommended_fields(self):
        for table, fields in RECOMMENDED_FIELDS.items():
            headers, rows = self.reader.read(table)
            if headers is None:
                continue
            for field in fields:
                if field not in headers:
                    continue
                empty = 0
                for row in rows:
                    v = XlsxReader.get_val(row, headers, field)
                    if not v or str(v).strip() == '':
                        empty += 1
                if empty > 0:
                    self._add('推荐字段', '%s.%s' % (table, field), 'WARN',
                              '%s: %d行未填写%s（可继承父节点，但建议显式填写）' % (table, empty, field))
                else:
                    self._add('推荐字段', '%s.%s' % (table, field), 'PASS',
                              '%s: %s全部已填写' % (table, field))

    # --- 检查 5: 数值型字段 ---
    def check_numeric_fields(self):
        for table, fields in NUMERIC_FIELDS.items():
            headers, rows = self.reader.read(table)
            if headers is None:
                continue
            for field in fields:
                if field not in headers:
                    continue
                bad = 0
                for row in rows:
                    v = XlsxReader.get_val(row, headers, field)
                    if v is not None and str(v).strip() != '':
                        try:
                            float(v)
                        except ValueError:
                            bad += 1
                if bad > 0:
                    self._add('数据类型', '%s.%s' % (table, field), 'FAIL',
                              '%d行不是有效数值' % bad)
                else:
                    self._add('数据类型', '%s.%s' % (table, field), 'PASS',
                              '全部为有效数值')

    # --- 检查 6: 交期日期 ---
    def check_due_dates(self):
        headers, rows = self.reader.read('销售订单')
        if headers is None:
            return

        dues = []
        bad_dates = 0
        for row in rows:
            v = XlsxReader.get_val(row, headers, '交期')
            if not v:
                continue
            d = excel_serial_to_date(v)
            if d:
                dues.append(d)
            else:
                bad_dates += 1

        if bad_dates > 0:
            self._add('交期日期', '日期格式', 'FAIL',
                      '%d个交期无法解析为日期' % bad_dates)
        elif dues:
            min_due = min(dues)
            max_due = max(dues)
            now = datetime.datetime.now()
            if max_due < now:
                self._add('交期日期', '交期范围', 'WARN',
                          '%s ~ %s (%d个订单) — 交期已过，排程可能产生延期' % (
                              min_due.strftime('%Y-%m-%d'),
                              max_due.strftime('%Y-%m-%d'),
                              len(dues)))
            else:
                self._add('交期日期', '交期范围', 'PASS',
                          '%s ~ %s (%d个订单)' % (
                              min_due.strftime('%Y-%m-%d'),
                              max_due.strftime('%Y-%m-%d'),
                              len(dues)))
        else:
            self._add('交期日期', '交期范围', 'FAIL', '无有效交期数据')


# ============================================================
# 报告输出
# ============================================================

def print_text_report(filepath, results, error_count, warn_count, verbose=False):
    """打印文本格式报告"""
    print()
    print('=' * 64)
    print('  FJSP 车间排产基础数据检查报告')
    print('  文件: %s' % os.path.basename(filepath))
    print('=' * 64)
    print()

    current_category = None
    for r in results:
        if r['category'] != current_category:
            current_category = r['category']
            print('【%s】' % current_category)

        icon = {'PASS': '✅', 'FAIL': '❌', 'WARN': '⚠️ ', 'INFO': 'ℹ️ '}.get(r['status'], '  ')
        print('  %s %s' % (icon, r['detail']))
        if verbose and 'errors' in r:
            for e in r['errors'][:5]:
                print('       → %s' % e)

    print()
    print('-' * 64)
    total = len(results)
    passed = total - error_count - warn_count
    print('  检查项: %d | 通过: %d | 警告: %d | 失败: %d' % (
        total, passed, warn_count, error_count))
    if error_count == 0:
        print('  ✅ 数据检查通过，可以上传排产')
    else:
        print('  ❌ 发现 %d 个错误，需要修正后再上传' % error_count)
    print('-' * 64)
    print()


def print_json_report(filepath, results, error_count, warn_count):
    """打印 JSON 格式报告"""
    output = {
        'file': os.path.basename(filepath),
        'summary': {
            'total_checks': len(results),
            'passed': len(results) - error_count - warn_count,
            'warnings': warn_count,
            'errors': error_count,
            'verdict': 'PASS' if error_count == 0 else 'FAIL',
        },
        'checks': results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='FJSP 车间排产基础数据检查工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例:\n  python3 fjsp_data_check.py data.xlsx\n  python3 fjsp_data_check.py data.xlsx --json\n  python3 fjsp_data_check.py data.xlsx -v'
    )
    parser.add_argument('file', help='xlsx 文件路径')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式')
    parser.add_argument('-v', '--verbose', action='store_true', help='显示错误详情')
    args = parser.parse_args()

    try:
        reader = XlsxReader(args.file)
    except FileNotFoundError as e:
        print('错误: %s' % e, file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print('错误: 无法读取文件: %s' % e, file=sys.stderr)
        sys.exit(2)

    checker = DataChecker(reader)
    results = checker.run_all()

    if args.json:
        print_json_report(args.file, results, checker.error_count, checker.warn_count)
    else:
        print_text_report(args.file, results, checker.error_count, checker.warn_count, args.verbose)

    reader.close()
    sys.exit(0 if checker.error_count == 0 else 1)


if __name__ == '__main__':
    main()
