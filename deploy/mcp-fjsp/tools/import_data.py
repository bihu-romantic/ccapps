import requests
import warnings
import re
warnings.filterwarnings('ignore')

BASE = "https://omni-oss.czy3d.com:18370"
AUTH = ('admin', 'admin')
XLSX = '/aidt2/aidt_data/workspaces/1784454581596815d42/media/c0d9011da22648c7973aa4542adbf51e_default_data_13tables.xlsx'

s = requests.Session()
s.auth = AUTH
s.verify = False

# 1. 获取 CSRF token
r = s.get(f"{BASE}/ccapps/scenario2/execute/launch/")
print(f"GET launch page: {r.status_code}")
csrf = s.cookies.get('csrftoken', '')
print(f"CSRF: {csrf[:20]}...")

# 2. 导入 workbook
with open(XLSX, 'rb') as f:
    r = s.post(
        f"{BASE}/ccapps/scenario2/execute/launch/importworkbook/",
        files={'file': ('data.xlsx', f, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
        data={'csrfmiddlewaretoken': csrf},
        headers={'Referer': f"{BASE}/ccapps/scenario2/execute/launch/"},
        allow_redirects=True
    )
print(f"POST importworkbook: {r.status_code}")
print(f"Response size: {len(r.text)} bytes")
print(f"Final URL: {r.url}")

# 检查是否有错误信息
text_lower = r.text.lower()
if 'error' in text_lower or 'fail' in text_lower:
    errors = re.findall(r'class="[^"]*error[^"]*"[^>]*>(.*?)</', r.text, re.DOTALL)
    for e in errors[:5]:
        print(f"ERROR: {e.strip()[:200]}")

# 检查是否有成功消息
if 'success' in text_lower or 'import' in text_lower:
    msgs = re.findall(r'class="[^"]*success[^"]*"[^>]*>(.*?)</', r.text, re.DOTALL)
    for m in msgs[:5]:
        print(f"SUCCESS: {m.strip()[:200]}")

# 3. 验证数据是否导入
r = s.get(f"{BASE}/ccapps/scenario2/api/input/demand/?format=json&limit=5")
print(f"\nDemand count: {len(r.json())}")
r = s.get(f"{BASE}/ccapps/scenario2/api/input/operation/?format=json&limit=5")
print(f"Operation count: {len(r.json())}")
r = s.get(f"{BASE}/ccapps/scenario2/api/input/resource/?format=json&limit=5")
print(f"Resource count: {len(r.json())}")
