#!/usr/bin/env bash
# 构建 ccapps:slim(ccapps-app,8010->8001)。
# 上下文 = 仓库根,Dockerfile = deploy/docker/Dockerfile.slim(ubuntu:24.04 + 预编译引擎)。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE=ccapps:slim
echo ">>> 构建 $IMAGE ( deploy/docker/Dockerfile.slim )"
DOCKER_BUILDKIT=0 docker build -f deploy/docker/Dockerfile.slim -t "$IMAGE" .

echo
echo "✅ 构建完成:"
docker images "$IMAGE" --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'
