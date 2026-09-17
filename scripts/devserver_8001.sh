#!/usr/bin/env bash
# 一键管理 8001 端口的 Django 开发服务器。
# 关键防坑：脚本内健康检查一律绕过代理（--noproxy），避免被 Clash/7897
# 之类的代理拦截导致「假超时」；并用无缓冲输出，日志实时可见。
set -euo pipefail

APP_DIR="/home/c/ccapps"
VENV_DIR="${APP_DIR}/venv"
PORT="8001"
LOG_FILE="/tmp/ccapps-runserver-${PORT}.log"
PID_FILE="/tmp/ccapps-runserver-${PORT}.pid"

# runserver 参数：默认 --noreload（与现状一致，省一个 reloader 进程）
RUN_ARGS="runserver 0.0.0.0:${PORT} --noreload"
if [[ "${CCAPPS_RELOAD:-0}" == "1" ]]; then
  RUN_ARGS="runserver 0.0.0.0:${PORT}"
fi

# 绕过代理的 curl：本地请求绝不走系统代理
curl_np() { curl --noproxy '*' "$@"; }

usage() {
  cat <<EOF
Usage: scripts/devserver_${PORT}.sh <command>

  start     启动开发服务器（默认 --noreload；CCAPPS_RELOAD=1 开热重载）
  stop      停止
  restart   重启
  status    查看进程 / 端口 / 健康检查（直连，绕过代理）+ 日志尾部
  probe     打印 login 页响应头
  proxy     诊断代理：对比「走代理」与「直连」访问本地的耗时
  logs      实时跟踪日志（Ctrl-C 退出）

防坑提示：
  - 本机若开了代理（http_proxy=...7897 之类），浏览器访问 127.0.0.1:${PORT} 可能极慢。
    解决任选其一：
      1) 地址栏改用 http://localhost:${PORT}/...        （浏览器对 localhost 直连）
      2) 代理软件（Clash/V2Ray）里把 127.0.0.1、localhost 设为 DIRECT
      3) 临时关闭代理软件的「系统代理」
  - 本脚本内部的 status/probe 均绕过代理，反映的是服务真实状态。
EOF
}

is_listening() {
  ss -ltnp 2>/dev/null | grep -qE "(0\.0\.0\.0|127\.0\.0\.1|\[::\]):${PORT} "
}

# 取监听该端口的进程 pid
listener_pid() {
  ss -ltnp 2>/dev/null | grep -E ":${PORT} " | grep -oP 'pid=\K[0-9]+' | head -1 || true
}

# 健康检查（直连），打印 HTTP 码与耗时
health() {
  curl_np --max-time 6 -s -o /dev/null \
    -w "HTTP %{http_code} | 耗时 %{time_total}s | 首字节 %{time_starttransfer}s" \
    "http://127.0.0.1:${PORT}/data/login/" 2>/dev/null || echo "(无响应/超时)"
}

status() {
  echo "== 进程 =="
  ps -ef | grep -nE "ccAPPSctl.py runserver .*${PORT}" | grep -v grep || echo "(无)"
  echo
  echo "== 端口 ${PORT} =="
  ss -ltnp | grep -nE ":${PORT} " || echo "(未监听)"
  echo
  echo "== 健康检查（直连，绕过代理）=="
  health
  echo
  echo
  echo "== 日志尾部 =="
  tail -n 20 "$LOG_FILE" 2>/dev/null || true
}

start() {
  if is_listening; then
    echo "端口 ${PORT} 已被占用，如需重启请用 restart："
    status
    return 0
  fi

  : > "$LOG_FILE"
  (
    {
      # 兼容 GLM/Anthropic assistant：从 .bashrc 继承 LLM key
      eval "$(
        grep -E '^export (ANTHROPIC_API_KEY|ANTHROPIC_BASE_URL|ANTHROPIC_DEFAULT_HAIKU_MODEL|ANTHROPIC_DEFAULT_SONNET_MODEL)=' "$HOME/.bashrc" 2>/dev/null || true
      )"
      source "$VENV_DIR/bin/activate"
      cd "$APP_DIR"
      export DJANGO_SETTINGS_MODULE=ccAPPSdb.settings
      export PYTHONUNBUFFERED=1          # 日志实时刷出，不再「日志为空」
      # 本地 PostgreSQL 18.6（系统包，端口 5432）：库名沿用线上的命名规则(前缀 ccapps)。
      # 连接身份固定为 ccapps（与 175 一致，来源见 db-backup/ccapps-175-20260911/MANIFEST.md）。
      # 注意：必须走 TCP 127.0.0.1，不能用 unix socket —— pg_hba 里 local 那条是 peer
      # 认证，要求 OS 用户名 == 角色名，而本机 OS 用户是 c，用 ccapps 走 socket 会
      # 直接 FATAL: Peer authentication failed。走 TCP 则命中 scram-sha-256，
      # 口令取自 settings 默认的 frepple。
      export POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
      export POSTGRES_USER="${POSTGRES_USER:-ccapps}"
      export POSTGRES_DBNAME="${POSTGRES_DBNAME:-ccapps}"
      # libccAPPS.so 需要 libxerces-c-3.2.so + ICU 74（Ubuntu 26.04 源里没有，
      # 已从 175 的 ccapps-app 容器拷到该目录，见 db-backup 的 MANIFEST）。
      export LD_LIBRARY_PATH="/home/c/.local/lib/ccapps${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
      nohup setsid python -u ccAPPSctl.py $RUN_ARGS >>"$LOG_FILE" 2>&1 < /dev/null &
      echo $! > "$PID_FILE"
    } >>"$LOG_FILE" 2>&1
  )

  # 轮询等待监听就绪（最多 ~10s）
  for _ in $(seq 1 20); do
    is_listening && break
    sleep 0.5
  done

  if is_listening; then
    echo "✓ 已启动：0.0.0.0:${PORT}  （日志：$LOG_FILE）"
    echo
    echo "== 健康检查（直连）=="
    health
    echo
    echo
    proxy_hint
  else
    echo "✗ 启动失败，日志如下："
    tail -n 100 "$LOG_FILE" || true
    return 1
  fi
}

stop() {
  # 优先用 PID 文件，再 pkill 兜底（覆盖 0.0.0.0 / 127.0.0.1 两种写法）
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
  fi
  pkill -f "ccAPPSctl.py runserver .*${PORT}" 2>/dev/null || true
  for _ in $(seq 1 10); do
    is_listening || break
    sleep 0.5
  done
  if is_listening; then
    local pid; pid=$(listener_pid)
    [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
  if is_listening; then
    echo "✗ 端口 ${PORT} 仍被占用："
    ss -ltnp | grep -nE ":${PORT} " || true
    return 1
  fi
  echo "✓ 已停止"
}

probe() {
  curl_np --max-time 6 -i -s "http://127.0.0.1:${PORT}/data/login/" | sed -n '1,20p'
}

# 诊断代理：对比「走代理（模拟浏览器）」与「直连」访问本地服务的耗时
proxy_diag() {
  echo "== 当前代理环境 =="
  env | grep -iE '^(http|https|all)_proxy=' || echo "(无)"
  echo
  if ! is_listening; then
    echo "服务未运行，请先 start。"
    return 0
  fi
  echo "== 对比访问 http://127.0.0.1:${PORT}/data/login/ =="
  printf "  走代理（模拟浏览器）："
  curl --max-time 8 -s -o /dev/null -w "HTTP %{http_code} | %{time_total}s\n" \
    "http://127.0.0.1:${PORT}/data/login/" 2>/dev/null || echo "超时/失败"
  printf "  直连（绕过代理）：    "
  curl_np --max-time 8 -s -o /dev/null -w "HTTP %{http_code} | %{time_total}s\n" \
    "http://127.0.0.1:${PORT}/data/login/" 2>/dev/null || echo "超时/失败"
  echo
  proxy_hint
}

# 若检测到代理，打印浏览器侧的解决提示
proxy_hint() {
  local p="${http_proxy:-${https_proxy:-${HTTP_PROXY:-${HTTPS_PROXY:-}}}}"
  [[ -z "$p" ]] && return 0
  cat <<EOF
⚠ 检测到代理（$p）。
  浏览器访问 127.0.0.1:${PORT} 很慢 = 浏览器走了系统代理。任选其一解决：
    1) 地址栏改用 http://localhost:${PORT}/scenario4/execute/planningboard/   （localhost 直连）
    2) 代理软件（Clash/V2Ray）里把 127.0.0.1、localhost 设为 DIRECT
    3) 临时关闭代理软件的「系统代理」
EOF
}

logs() {
  tail -n 50 -f "$LOG_FILE"
}

cmd="${1:-}"
case "$cmd" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  probe)   probe ;;
  proxy)   proxy_diag ;;
  logs)    logs ;;
  *)       usage; exit 1 ;;
esac
