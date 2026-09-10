#!/usr/bin/env bash
set -euo pipefail

task_root=$(cd "$(dirname "$0")/.." && pwd)
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT

cat > "$fixture/demands.csv" <<'EOF'
id,item,quantity,due_seconds,priority,frozen
Order 01,product 01-01,1,0,1,0
EOF
cat > "$fixture/resources.csv" <<'EOF'
id,capacity
Machine A,1
EOF
cat > "$fixture/operations.csv" <<'EOF'
id,output_item,duration_seconds,minimum,multiple,maximum,allow_cross_shift
01 step 01,product 01-01,10800,1,1,1,1
EOF
cat > "$fixture/operation_resources.csv" <<'EOF'
operation,resource
01 step 01,Machine A
EOF
cat > "$fixture/operation_materials.csv" <<'EOF'
operation,item,quantity
01 step 01,product 01-01,1
EOF
cat > "$fixture/inventory.csv" <<'EOF'
item,quantity
EOF
cat > "$fixture/purchasable_items.csv" <<'EOF'
item,leadtime_seconds,minimum,multiple
EOF
cat > "$fixture/resource_calendars.csv" <<'EOF'
resource,start_seconds,end_seconds,weekday,date
Machine A,0,7200,0,
Machine A,0,14400,1,
EOF

cmake --build "$task_root/cpp/build" >/dev/null
"$task_root/cpp/build/chaozhenyun_scheduler" "$fixture" "$fixture/result.csv" mrp >/dev/null
# The three-hour task consumes two Monday hours and one Tuesday hour.
test "$(awk -F, 'NR==2 {print $5","$6}' "$fixture/result.csv")" = "0,90000"
printf 'calendar regression passed\n'

# A missing purchase rule reports a structured problem but still creates a
# constrained production task for capacity and impact analysis.
cat > "$fixture/operation_materials.csv" <<'EOF'
operation,item,quantity
01 step 01,raw material,-1
01 step 01,product 01-01,1
EOF
"$task_root/cpp/build/chaozhenyun_scheduler" "$fixture" "$fixture/shortage.csv" mrp >/dev/null
test "$(awk -F, 'NR==2 {print $7}' "$fixture/shortage.csv")" = "material_shortage"
grep -q '^material_shortage,warning,Order 01,01 step 01,raw material,1,0,create purchase or manufacturing supply$' "$fixture/shortage.csv.problems.csv"
printf 'shortage diagnostic regression passed\n'
