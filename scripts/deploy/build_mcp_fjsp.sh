#!/usr/bin/env bash
# 构建 ccapps-mcp-fjsp:latest(ccapps-mcp-fjsp,9006->9000)。
# 上下文 = deploy/mcp-fjsp/(6 工具,描述文件驱动,加工具不用改 server.py)。
#
# 本机没装 docker，所以默认走 175 远程构建：rsync 上下文过去再 docker build。
# 想本地构建就装好 docker，脚本会自动识别。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE="${IMAGE:-ccapps-mcp-fjsp:latest}"
REMOTE="${REMOTE:-175}"
REMOTE_DIR="${REMOTE_DIR:-/root/ccapps-build/mcp-fjsp}"
CTX="deploy/mcp-fjsp"

if command -v docker >/dev/null 2>&1; then
  WHERE="local"
  echo ">>> 本地构建 $IMAGE ( $CTX/ )"
  docker build -t "$IMAGE" "$CTX"
else
  WHERE="$REMOTE"
  echo ">>> 本机无 docker，改为在 $REMOTE 上构建"
  ssh "$REMOTE" "mkdir -p $REMOTE_DIR"
  # --delete 保证远端不残留上一次构建的旧文件
  rsync -az --delete "$CTX/" "$REMOTE:$REMOTE_DIR/"
  ssh "$REMOTE" "docker build -t $IMAGE $REMOTE_DIR"
fi

echo
echo "✅ 构建完成:"
if [[ "$WHERE" == "local" ]]; then
  docker images "$IMAGE" --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'
else
  ssh "$REMOTE" "docker images '$IMAGE' --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'"
fi
