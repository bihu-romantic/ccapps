# 构建脚本(本地打镜像)

三个镜像各自一个脚本,集中放此目录。只做本地 `docker build`,不上传。

| 脚本 | 镜像 | 容器(端口) | 构建上下文 |
|------|------|------------|-----------|
| `build_app.sh` | `ccapps:slim` | ccapps-app(8010→8001) | 仓库根 / `deploy/docker/Dockerfile.slim` |
| `build_mcp.sh` | `ccapps-mcp:latest` | ccapps-mcp(9001→9000) | `deploy/mcp/`(17 工具) |
| `build_mcp_aidt.sh` | `ccapps-mcp-aidt:latest` | aidt_ccappss_mrp_mcp(9005→9000) | `deploy/mcp-aidt/`(12 工具) |
| `build_mcp_fjsp.sh` | `ccapps-mcp-fjsp:latest` | ccapps-mcp-fjsp(9006→9000) | `deploy/mcp-fjsp/`(6 工具,描述文件驱动) |

```bash
./scripts/deploy/build_app.sh          # ccapps:slim
./scripts/deploy/build_mcp.sh          # ccapps-mcp:latest(9001)
./scripts/deploy/build_mcp_aidt.sh     # ccapps-mcp-aidt:latest(9005,独立 tag)
./scripts/deploy/build_mcp_fjsp.sh     # ccapps-mcp-fjsp:latest(9006,本机无 docker 时自动到 175 构建)
```

## ccapps-mcp-fjsp(9006)

把 `tools/` 下的脚本按**描述文件**暴露成 MCP 工具。加一个工具 = 丢一个脚本 +
一个同名 `.tool.yaml` + 重启容器，**不用改 server.py**。

- 对外地址:`http://175.27.132.107/cc-apps-fjsp-mcp/mcp`(nginx `location ^~ /cc-apps-fjsp-mcp/` → `10.206.0.9:9006`)
- 认证:**必须** `-e MCP_AUTH_TOKEN=<值>`。`BearerMiddleware` 对空 token 是「放行」,
  忘了传等于裸奔(现有 9001/9005 两个容器就都没设,即当前无认证)
- 与 9001/9005 的差别:那两个是**写死的工具函数**;这个是从 yaml 扫出来的

### 加一个新工具

1. 往 `deploy/mcp-fjsp/tools/` 放脚本 `my_tool.py`
2. 同目录放 `my_tool.tool.yaml`:

```yaml
name: my_tool
description: >- 给 LLM 看的说明,写清楚参数是什么、返回什么
script: my_tool.py
timeout_sec: 120          # 必须 < 340(nginx proxy_read_timeout 是 360s)
params:
  - {name: xlsx_base64, type: string, required: true, description: xlsx 内容的 base64,
     file: {as: input.xlsx}}
  - {name: device, type: string, description: 设备名}
argv:
  - "${file:xlsx_base64}"      # 上传的文件路径
  - "${device?}"               # 有值才传,缺省整条省略
  # - {value: scenario, flag: "--scenario"}   有值→"--scenario X"
  # - {bool: verbose, flag: "--verbose"}      真→"--verbose"
```

3. 重新构建 + 重启容器

### 已知限制(重要)

- `tools/` 下 6 个脚本是**原样**放进去的,没做参数化。因此:
  - `fast_rolling_sim` / `import_data` 的目标地址、场景号写死在脚本里,
    工具上**没有参数**可改(`import_data` 还有个写死的待导入文件路径,
    靠 `stage` 把你上传的内容落到那个路径上)
  - `analyze_schedule --scenario` 的下载地址是内网 `192.168.50.125:30002`,
    容器里连不上 → 这个模式在服务器上跑不通,请用 `file_base64` 上传
- **两个 xlsx 解析 bug(未修,当前原样带入镜像)**。对 Excel/WPS 产出的工作簿会出错:
  - `fjsp_data_check.py`: 解析 `xl/_rels/workbook.xml.rels` 时对相对路径不补 `xl/`
    前缀 → 所有表读不到,报「缺少必填表」(拿真实 Excel 文件实测过)
  - `check_resource_calendar.py`: `_parse_xlsx` 只认 `inlineStr`,不解析
    `t="s"` 的 sharedStrings → 表头变成列序号 `['0','1','2','3']` 且数据错位一行

  两者修法一致,同一个包里的 `read_xlsx.py` / `analyze_schedule.py` 的读取器
  是写对的(有 `xl/` 前缀 + namelist 回退 + sharedStrings),照抄即可。
  openpyxl 产出的文件不受影响,所以只用 openpyxl 造的测试数据会**假绿**。


## 要点
- **9005 独立镜像**:`ccapps-mcp-aidt:latest`,与 9001 的 `ccapps-mcp:latest` 分开,互不覆盖。
- **只构建不上传**:需要上传到 mc 时手动执行:
  `docker save <镜像> | gzip | ssh mc 'gunzip | docker load'`
  (mc sshd 不稳,大镜像建议先 `docker save|gzip > x.tar.gz` 再 rsync 断点续传)
