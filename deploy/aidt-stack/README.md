# ccapps-aidt

AIDT 全栈编排与 `mrp_ccapps` MCP 独立仓库。

## 汇总

| 项 | 内容 |
|---|---|
| Git 地址 | https://github.com/bihu-romantic/ccapps |
| SSH | `git@github.com:bihu-romantic/ccapps.git` |
| 稳定 Release 分支 | `release-aidt` |
| 开发分支 | `fc_aps`（业务开发）/ `main` |
| 硬件 / 依赖 / 部署 | 见 [docs/deployment.md](docs/deployment.md) |

发版与目标机部署请使用 **`release-aidt`** 分支；代码目录 `deploy/aidt-stack/`。

## 175 对照

| 容器 | 作用 | 端口 |
|---|---|---|
| `aidt_ccapps_db` | PostGIS | 内网 5432 |
| `aidt_ccapps_app` | ccAPPS Web/API | 8011→8001 |
| `aidt_ccapps_worker_scenario1` | `runworker --database=scenario1` | — |
| `aidt_ccappss_mrp_mcp` | MRP/FAS MCP | 9005→9000 |

## 目录

```
mcp-aidt/           # MCP 源码（CI 构建镜像）
compose/            # docker compose 全栈编排
nginx/              # 反代示例
docs/deployment.md  # 硬件要求、依赖安装与部署
.env.example        # 环境变量模板（无真实密钥）
.github/workflows/  # CI/CD
```

## 本仓职责

- **构建并推送** `ghcr.io/bihu-romantic/ccapps-mcp-aidt`
- 提供整套 `docker compose` 编排
- 部署工作流仅 `workflow_dispatch` 占位，**默认不自动部署**

`app` / `worker` 运行镜像依赖完整 ccAPPS monorepo（含预编译引擎），不在本仓从零编译。通过 **Promote app image** 工作流把已有镜像提升到 GHCR。

## 快速开始

详见 [docs/deployment.md](docs/deployment.md)。摘要：

```bash
git clone git@github.com:bihu-romantic/ccapps.git
cd ccapps && git checkout release-aidt
# 进入 deploy/aidt-stack/
cp .env.example .env   # 填密钥
cd compose && docker compose --env-file ../.env up -d --build
```

## CI 工作流

| Workflow | 触发 | 作用 |
|---|---|---|
| `Build MCP image` | push/PR（`mcp-aidt/**`）或手动 | 构建推送 MCP 镜像 |
| `Promote app image` | 手动 | 将已有 app/worker 镜像 tag 推到 GHCR |
| `Deploy (placeholder)` | 手动 | SSH 部署占位；跟 `release`，默认不执行 |

## 镜像名约定

- `ghcr.io/bihu-romantic/ccapps-mcp-aidt:<sha\|latest>`
- `ghcr.io/bihu-romantic/ccapps-aidt-app:<tag>`
- `ghcr.io/bihu-romantic/ccapps-aidt-worker:<tag>`
