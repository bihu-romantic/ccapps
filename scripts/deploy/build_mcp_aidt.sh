#!/usr/bin/env bash
# 构建 ccapps-mcp-aidt:latest(aidt_ccappss_mrp_mcp,9005->9000)。
# 上下文 = deploy/mcp-aidt/(12 工具,MRP/FAS 排程分析专用,无报价)。
# 9005 用独立 tag,与 9001 的 ccapps-mcp:latest 分开,互不覆盖。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE=ccapps-mcp-aidt:latest
echo ">>> 构建 $IMAGE ( deploy/mcp-aidt/ )"
docker build -t "$IMAGE" deploy/mcp-aidt/

echo
echo "✅ 构建完成:"
docker images "$IMAGE" --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'
