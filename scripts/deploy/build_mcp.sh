#!/usr/bin/env bash
# 构建 ccapps-mcp:latest(ccapps-mcp,9001->9000)。
# 上下文 = deploy/mcp/(含 write_order 写入销售/报价订单)。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE=ccapps-mcp:latest
echo ">>> 构建 $IMAGE ( deploy/mcp/ )"
docker build -t "$IMAGE" deploy/mcp/

echo
echo "✅ 构建完成:"
docker images "$IMAGE" --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'
