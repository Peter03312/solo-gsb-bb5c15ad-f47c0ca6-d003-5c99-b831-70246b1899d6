#!/usr/bin/env bash
# 排班可用性查询（本轮新增）端到端示例：
# 三台仪器各自登记校准/禁用 -> 查询整组在时间范围内何时全部具备放行条件。
# 依赖：curl（jq 可选，没有时用 python -m json.tool）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }

# ---- 背景数据（如已存在重复事件号/区间会报 409，可忽略后继续） ----
# HPLC-01：全年通过校准；3 月有一段禁用
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2027-01-01T00:00:00Z",
  "passed": true
}' >/dev/null || true
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "event_no": "SCHED-DIS-HPLC",
  "instrument": "HPLC-01",
  "disabled_from": "2026-03-01T00:00:00Z",
  "disabled_to":   "2026-04-01T00:00:00Z",
  "reason": "进样阀故障待修"
}' >/dev/null || true

# GC-02：上半年通过、6 月起当前校准结论为未通过（开放区间）
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-02",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2026-06-01T00:00:00Z",
  "passed": true
}' >/dev/null || true
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-02",
  "valid_from": "2026-06-01T00:00:00Z",
  "valid_to":   null,
  "passed": false
}' >/dev/null || true

# 注意：不登记 MS-03 的任何校准 —— 用于演示“无当前校准不被其他仪器掩盖”。

echo "== 1. 查询整组 [HPLC-01, GC-02, MS-03] 在 2026 上半年的可放行时段 =="
curl -sS -X POST "$BASE/scheduling/availability" \
  -H 'Content-Type: application/json' \
  -d '{
        "instruments": ["HPLC-01", "GC-02", "MS-03"],
        "from": "2026-01-01T00:00:00Z",
        "to":   "2026-09-01T00:00:00Z"
      }' | pp

echo "== 2. 只查两台已覆盖仪器的可放行窗口 -> all_releasable=true =="
curl -sS -X POST "$BASE/scheduling/availability" \
  -H 'Content-Type: application/json' \
  -d '{
        "instruments": ["HPLC-01", "GC-02"],
        "from": "2026-04-15T00:00:00+08:00",
        "to":   "2026-05-15T00:00:00+08:00"
      }' | pp

echo "== 3. 非法输入：重复仪器 -> 422 DUPLICATE_INSTRUMENT =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/scheduling/availability" \
  -H 'Content-Type: application/json' \
  -d '{"instruments": ["HPLC-01", "HPLC-01"], "from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}'

echo "== 4. 非法输入：时间不带时区 -> 422 TIMEZONE_REQUIRED =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/scheduling/availability" \
  -H 'Content-Type: application/json' \
  -d '{"instruments": ["HPLC-01"], "from": "2026-01-01T00:00:00", "to": "2026-02-01T00:00:00"}'

echo
echo "提示：结果 planning_only=true 仅供规划，不预占未来放行资格；"
echo "      正式报告仍以提交 POST /reports 时的已提交状态判定。"
