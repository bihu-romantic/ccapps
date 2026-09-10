#!/usr/bin/env bash
set -euo pipefail

task_root=$(cd "$(dirname "$0")/.." && pwd)
case_name=${1:-job_schedule}
case_dir="$task_root/case_data/$case_name"

if [[ ! -d "$case_dir" ]]; then
  echo "case not found: $case_dir" >&2
  exit 2
fi

echo "case=$case_name"
echo "case_dir=$case_dir"

python3 "$task_root/tools/schedule_monitor.py" \
  --case "$case_name" \
  --mode mrp \
  --reset \
  --once

python3 "$task_root/tools/write_device_update.py" \
  --case "$case_name" \
  --device-id FAN-01 \
  --status fault \
  --available 0 \
  --capacity-factor 0 \
  --risk-level high \
  --write-seconds 28800

python3 "$task_root/tools/schedule_monitor.py" \
  --case "$case_name" \
  --mode mrp \
  --once

python3 "$task_root/tools/build_table_gantt.py" --case "$case_name"
python3 "$task_root/tools/inspect_runtime_tables.py" --case "$case_name"

echo "outputs:"
echo "  $case_dir/planning_runtime.sqlite"
echo "  $case_dir/table_gantt.html"
