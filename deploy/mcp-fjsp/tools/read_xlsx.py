import zipfile, xml.etree.ElementTree as ET, re, sys

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
NSMAP = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def col_to_idx(col):
    idx = 0
    for c in col:
        idx = idx * 26 + (ord(c) - 64)
    return idx - 1

def read_xlsx(path):
    z = zipfile.ZipFile(path)
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.findall('main:si', NSMAP):
            txt = ''.join(t.text or '' for t in si.iter(NS + 't'))
            shared.append(txt)
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    sheets = []
    for s in wb.findall('main:sheets/main:sheet', NSMAP):
        sheets.append((s.get('name'), s.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    rel_map = {}
    for r in rels:
        rel_map[r.get('Id')] = r.get('Target')
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
                ref = c.get('r')
                col = re.match(r'[A-Z]+', ref).group()
                ci = col_to_idx(col)
                maxc = max(maxc, ci)
                t = c.get('t')
                v = c.find('main:v', NSMAP)
                val = v.text if v is not None else ''
                if t == 's':
                    val = shared[int(val)] if val else ''
                elif t == 'inlineStr':
                    is_el = c.find('main:is', NSMAP)
                    if is_el is not None:
                        val = ''.join(x.text or '' for x in is_el.iter(NS + 't'))
                cells[ci] = val
            rowlist = [cells.get(i, '') for i in range(maxc + 1)]
            rows_out.append(rowlist)
        result[name] = rows_out
    return result

path = sys.argv[1] if len(sys.argv) > 1 else 'fjsp_results.xlsx'
data = read_xlsx(path)
print('Sheets:', list(data.keys()))
for sn, rows in data.items():
    print('\n=== %s (%d rows) ===' % (sn, len(rows)))
    for i, r in enumerate(rows[:8]):
        print(r)
