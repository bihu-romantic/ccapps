#!/usr/bin/env bash
# 构建 ccapps-schedule:slim(ccapps-app,8010->8001)。
# 上下文 = 仓库根,Dockerfile = deploy/docker/Dockerfile.slim(ubuntu:24.04 + 预编译引擎)。
#
# 本机没装 docker 时自动走 175 远程构建：rsync 仓库过去再 docker build。
# 构建前请确认 bin/libccAPPS.so 已是最新引擎。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE="${IMAGE:-ccapps-schedule:slim}"
REMOTE="${REMOTE:-175}"
REMOTE_DIR="${REMOTE_DIR:-/root/ccapps-build/app}"
DOCKERFILE="deploy/docker/Dockerfile.slim"

if [[ ! -f bin/libccAPPS.so ]]; then
  echo "ERROR: missing bin/libccAPPS.so — rebuild the engine first" >&2
  exit 1
fi

RSYNC_EXCLUDES=(
  --exclude='.git/'
  --exclude='.venv/'
  --exclude='venv/'
  --exclude='pgdata/'
  --exclude='logs/'
  --exclude='log/'
  --exclude='node_modules/'
  --exclude='build/'
  --exclude='build*/'
  --exclude='data/'
  --exclude='*.zip'
  --exclude='.idea/'
  --exclude='.vscode/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
)

if command -v docker >/dev/null 2>&1; then
  WHERE="local"
  echo ">>> 本地构建 $IMAGE ( $DOCKERFILE )"
  DOCKER_BUILDKIT=0 docker build -f "$DOCKERFILE" -t "$IMAGE" .
else
  WHERE="$REMOTE"
  echo ">>> 本机无 docker，改为在 $REMOTE 上构建"
  echo ">>> rsync -> $REMOTE:$REMOTE_DIR  (lib mtime: $(stat -c '%y' bin/libccAPPS.so))"
  ssh "$REMOTE" "mkdir -p $REMOTE_DIR"
  rsync -az --delete "${RSYNC_EXCLUDES[@]}" "$ROOT/" "$REMOTE:$REMOTE_DIR/"
  ssh "$REMOTE" "DOCKER_BUILDKIT=0 docker build -f $REMOTE_DIR/$DOCKERFILE -t $IMAGE $REMOTE_DIR"
fi

echo
echo "✅ 构建完成:"
if [[ "$WHERE" == "local" ]]; then
  docker images "$IMAGE" --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'
else
  ssh "$REMOTE" "docker images '$IMAGE' --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'"
fi
