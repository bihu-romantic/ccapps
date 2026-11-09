#!/usr/bin/env bash
# 把 demo_data 里的一套样例导入指定场景。
# 用法：demo_data/import.sh <套件> <场景>
# 例：  demo_data/import.sh manufacturing_demo scenario10
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DATASETS=(demo manufacturing_demo distribution_demo jobshop flow_line job_schedule)

usage() {
  cat <<EOF
用法：demo_data/import.sh <套件> <场景>

可导入数据库的套件：
  demo                两个工厂的小例子
  manufacturing_demo  家具厂
  distribution_demo   网球用品分销
  jobshop             机加工车间
  flow_line           流水线
  job_schedule        柔性作业车间

家具厂的三份 xml 在 demo_data/furniture/，不是数据库样例，不能用本脚本导入。

导入是追加。目标场景里已有同名主数据时会冲突。名字里带 demo 的三套，导入后交期会拨到今天。
EOF
}

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

name="$1"
scenario="$2"

case "$name" in
  furniture|furniture/*)
    echo "furniture 是计划文件，不是可导入的数据库样例。" >&2
    echo "模型：demo_data/furniture/model.xml" >&2
    echo "排程结果：demo_data/furniture/plan_constrained.xml 、plan_unconstrained.xml" >&2
    exit 1
    ;;
esac

ok=0
for item in "${DATASETS[@]}"; do
  if [[ "$item" == "$name" ]]; then
    ok=1
  fi
done
if [[ "$ok" -ne 1 ]]; then
  echo "未知套件：$name" >&2
  usage >&2
  exit 1
fi

exec ./venv/bin/python ccAPPSctl.py loaddata "$name" --database "$scenario"
