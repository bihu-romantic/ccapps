# AIDT 服务：硬件要求、依赖安装与部署

## 1. 汇总表

| 项 | 内容 |
|---|---|
| 服务名称 | AIDT ccAPPS 全栈（Web + Worker + PostGIS + MRP MCP） |
| Git 地址 | https://github.com/bihu-romantic/ccapps |
| SSH 克隆 | `git@github.com:bihu-romantic/ccapps.git` |
| 稳定 Release 分支 | `release-aidt` |
| 代码目录 | `deploy/aidt-stack/` |
| 开发分支 | `fc_aps`（业务开发）/ `main` |
| MCP 镜像 | `ghcr.io/bihu-romantic/ccapps-mcp-aidt` |
| App / Worker 镜像 | `ghcr.io/bihu-romantic/ccapps-aidt-app` / `ccapps-aidt-worker`（需 Promote 工作流提升） |
| 默认业务端口 | Web `8011`，MCP `9005` |
| 对照环境 | 175：`aidt_ccapps_*` / `aidt_ccappss_mrp_mcp` |

发版与部署一律以 **`release-aidt`** 分支为准；编排与 MCP 位于 `deploy/aidt-stack/`。

---

## 2. 硬件运行要求

按 175 现网配置（PostgreSQL `max_connections=40`、`shared_buffers=128MB`）给出最低与推荐规格。

| 资源 | 最低（联调/演示） | 推荐（日常计划） |
|---|---|---|
| CPU | 4 vCPU | 8 vCPU |
| 内存 | 8 GB | 16 GB |
| 系统盘 | 40 GB SSD | 100 GB SSD |
| 数据盘 | 20 GB（库与日志） | 100 GB+（视场景数据量） |
| 网络 | 可访问镜像仓库与 HTTPS 反代 | 同左；MCP/Web 分端口或同域反代 |
| OS | Linux x86_64（Ubuntu 22.04/24.04 或兼容发行版） | 同左 |

组件粗算（镜像层，未含业务数据）：

| 组件 | 约占用 |
|---|---|
| `ccapps-aidt:app` / worker | ~1.1 GB / 镜像 |
| `ccapps-mcp-aidt` | ~225 MB |
| `postgis/postgis:18-3.6-alpine` | ~500 MB |
| 运行中库数据（参考 175） | 约 1–数 GB 起 |

约束提示：

- 数据库默认 `max_connections=40`；连接打满会出现 `sorry, too many clients already`，可用 MCP 工具 `cleanup_db_resources` 清理。
- Worker 跑 MRP/FAS 时 CPU/内存会脉冲升高，推荐规格更稳妥。
- 不建议在无容器运行时的 Windows 本机直接跑全栈；可用 WSL2 + Docker，但生产请用 Linux 服务器。

---

## 3. 依赖安装

### 3.1 主机软件

```bash
# Docker Engine + Compose 插件
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
# 按 Docker 官方文档安装 docker-ce / docker-compose-plugin
docker version
docker compose version

# 可选：拉取私有 GHCR 镜像
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
```

### 3.2 仓库与配置

```bash
git clone git@github.com:bihu-romantic/ccapps.git
cd ccapps
git checkout release-aidt
git pull --ff-only origin release-aidt
# 编排与 MCP 在 deploy/aidt-stack/

cp .env.example .env
# 必填：POSTGRES_PASSWORD、DJANGO_SECRET_KEY、CCAPPS_API_TOKEN
# 建议：MCP_AUTH_TOKEN（客户端 Bearer）
```

### 3.3 镜像依赖

| 镜像 | 来源 |
|---|---|
| `postgis/postgis:18-3.6-alpine` | Docker Hub |
| `ccapps-mcp-aidt:latest` | 本仓 CI 构建，或 `compose` 本地 `build: ../mcp-aidt` |
| `ccapps-aidt:app` / `worker` | 主仓/跳板机已有镜像，经 **Promote app image** 推到 GHCR 后拉取 |

`.env` 中可写：

```bash
CCAPPS_IMAGE=ghcr.io/bihu-romantic/ccapps-aidt-app:latest
CCAPPS_WORKER_IMAGE=ghcr.io/bihu-romantic/ccapps-aidt-worker:latest
```

### 3.4 MCP Python 依赖（仅本地调试 MCP 时）

见 [`mcp-aidt/requirements.txt`](../mcp-aidt/requirements.txt)：`fastmcp[server]`、`httpx`。容器构建时自动安装，主机一般不必单独装。

---

## 4. 部署步骤

### 4.1 启动全栈

```bash
cd compose
docker compose --env-file ../.env up -d --build
docker compose --env-file ../.env ps
```

服务与端口：

| 服务 | 容器名 | 宿主机端口 |
|---|---|---|
| db | `aidt_ccapps_db` | 不映射（仅 `aidt-net`） |
| app | `aidt_ccapps_app` | `8011→8001` |
| worker | `aidt_ccapps_worker_scenario1` | 无 |
| mrp_mcp | `aidt_ccappss_mrp_mcp` | `9005→9000` |

### 4.2 HTTPS 反代（可选）

参考 [`nginx/cc-apps-mrp-mcp.conf.example`](../nginx/cc-apps-mrp-mcp.conf.example)，将 `/cc-apps-mrp-mcp/` 代理到 `127.0.0.1:9005`，并转发 `Authorization`，关闭缓冲。

### 4.3 验收

```bash
curl -sS -o /dev/null -w "app %{http_code}\n" http://127.0.0.1:8011/
curl -sS -o /dev/null -w "mcp %{http_code}\n" \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  http://127.0.0.1:9005/mcp
docker logs aidt_ccapps_worker_scenario1 --tail 80
```

客户端 MCP 配置：

```json
{
  "key": "mrp_ccapps",
  "transport": "streamable_http",
  "url": "https://你的域名/cc-apps-mrp-mcp/mcp",
  "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
}
```

### 4.4 升级（跟 `release`）

```bash
cd /path/to/ccapps-aidt
git fetch origin
git checkout release-aidt
git pull --ff-only origin release-aidt
cd compose
docker compose --env-file ../.env pull
docker compose --env-file ../.env up -d --build
```

GitHub Actions 中 **Deploy (placeholder)** 在配置 `DEPLOY_*` secrets 且 `enable_deploy=true` 后，会检出 `release-aidt` 再执行同等 compose 升级；当前阶段默认不开启。

### 4.5 回滚

```bash
git checkout <previous-release-sha>
cd compose && docker compose --env-file ../.env up -d
# 或指定旧镜像 tag：
# CCAPPS_IMAGE=ghcr.io/.../ccapps-aidt-app:<old> docker compose up -d
```

---

## 5. CI/CD 说明

| Workflow | 触发 | 说明 |
|---|---|---|
| Build MCP image | `push`/`PR`（`mcp-aidt/**`）或手动 | 推送 `ghcr.io/.../ccapps-mcp-aidt` |
| Promote app image | 手动 | 将已有 app/worker 镜像提升到 GHCR（不编引擎） |
| Deploy (placeholder) | 手动 | 默认不部署；需显式 `enable_deploy=true` |

稳定产物以 **`release-aidt` 分支** 构建的镜像与编排为准。
