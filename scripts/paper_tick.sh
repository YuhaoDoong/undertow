#!/bin/zsh
# 模拟仓独立调度（Codex 032 O1，2026-10-02）。
#
# 为什么从 session_hooks.sh 拆出来：9/30 入场窗口 ET 10:00–10:30 里，session 的 paper_tick 只跑了 3 次
# （10:01、10:16、10:28），10:07/10:12/10:22 的唤醒被同一进程里长耗时的开盘窗/盘中采样占住。
# 模拟仓的入场、盯市、结算只看规格里的日期与时刻，不需要交易日历，也不该排在长任务后面。
#
# 调度：launchd StartInterval=60（scripts/launchd/com.yuhaodoong.undertow.paper.plist）。
# 切换：session_hooks.sh 不再调用 paper_tick（同一提交），只有本任务主控；即使短暂重叠，
#       journal 写入由 paper_trades.py 的 flock 互斥、按状态机幂等推进，不会重复入场。
# 互斥：lockf(1) 内核锁；上一轮未结束（取报价慢）→ 本轮记「busy」直接退出，不排队堆积。
# 留痕：每次唤醒原子写 data/paper/.status_paper.json（wake/start/end/rc/sched）；有动作或失败才写月志（私有）；
#       周末只 touch 心跳。失败：连续 3 次通知 + 写 data/reports/FAILURE_<ET日期>.txt（AGENTS 静默失败第 1 条）。
# 告警（Codex 047 C）：tick 另写 typed 结果 data/paper/.tick_result.json（schema paper-tick-result-v1），按其中 alerts
#       升级 —— 配置错误 / 在场估值未决 / 跟踪观察缺失，同轮 mark 成功也照报；不靠 grep 人读文案。
#       通知之外总落兜底：公开 FAILURE 文件只写类别计数，详情在私有 data/paper/alerts.jsonl。结果文件缺失/损坏 = crashed，同样兜底。
# 只读行情、只写私有文件；从不下单。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$PWD"
PY="${PYTHON:-python3}"
SCHED="paper-launchd-v1"
LOG_DIR="data/logs"; mkdir -p "$LOG_DIR"
WAKE=$(TZ=America/New_York date +%FT%T%z)
ET_DATE=$(TZ=America/New_York date +%F)
ET_DOW=$(TZ=America/New_York date +%u)
# 日志与状态含仓位 id（即交易判断）→ 放私有 data/paper/（gitignore），不进会入库的 data/logs/；心跳只是空文件，留 data/logs/
PDIR="data/paper"; mkdir -p "$PDIR"
LOG="$PDIR/paper_tick_$(TZ=America/New_York date +%Y-%m).log"
: > "$LOG_DIR/.paper_alive"
(( ET_DOW >= 6 )) && exit 0

notify() {  # $1=标题 $2=正文
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Glass\"" 2>/dev/null || true
}
clip() { "$PY" -c 'import sys; print(sys.stdin.read().replace("\n", " ").strip()[:int(sys.argv[1])])' "$1"; }
dispatch_alerts() {  # $1=typed 结果文件：crashed / alert 都通知并落兜底；ok 什么都不做
  local sm st n kinds
  sm=$("$PY" scripts/paper_trades.py alert-summary "$1" 2>/dev/null)
  st=$(printf '%s' "$sm" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo crashed)
  [[ "$st" == "ok" ]] && return 0
  if [[ "$st" == "alert" ]]; then
    n=$(printf '%s' "$sm" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["n"])')
    kinds=$(printf '%s' "$sm" | "$PY" -c 'import json,sys; print("，".join(f"{k}×{v}" for k,v in sorted(json.load(sys.stdin)["kinds"].items())))')
    notify "⚠️ 模拟仓需关注 ${n} 条" "$kinds"
    printf '%s | ET %s | ⚠️ 模拟仓需关注事件 %s 条（%s），详见私有 data/paper/alerts.jsonl\n' \
      "$(date '+%F %H:%M %z')" "$WAKE" "$n" "$kinds" >> "data/reports/FAILURE_${ET_DATE}.txt"
  else
    notify "⚠️ 模拟仓结果文件缺失或损坏" "tick 返回 0 但 typed 结果不可读"
    printf '%s | ET %s | ⚠️ 模拟仓 tick 结果文件缺失或损坏（crashed），详见私有 data/paper/paper_tick_*.log\n' \
      "$(date '+%F %H:%M %z')" "$WAKE" >> "data/reports/FAILURE_${ET_DATE}.txt"
  fi
}
status() {  # $1=rc $2=start $3=end $4=summary —— 原子写（同目录临时文件 + mv）
  local tmp="$PDIR/.status_paper.json.$$"
  "$PY" - "$1" "$2" "$3" "$4" "$WAKE" "$SCHED" > "$tmp" <<'EOF' && mv -f "$tmp" "$PDIR/.status_paper.json"
import json, sys
rc, start, end, summ, wake, sched = sys.argv[1:7]
print(json.dumps({"schema": 1, "command": "paper_tick", "sched": sched, "wake": wake, "start": start or None,
                  "end": end or None, "rc": int(rc), "summary": summ[:300]}, ensure_ascii=False))
EOF
}

START=$(TZ=America/New_York date +%FT%T%z)
TRES="$PDIR/.tick_result.json"; rm -f "$TRES"
RES=$(PAPER_SCHED="$SCHED" lockf -t 0 "$LOG_DIR/.paper_tick.lock" "$PY" scripts/paper_trades.py tick --status-file "$TRES" 2>&1); RC=$?
END=$(TZ=America/New_York date +%FT%T%z)
if (( RC == 75 )); then                          # 上一轮仍持锁：不是失败，也不排队
  status 75 "" "" "busy（上一轮仍在运行）"
  printf '%s | busy：上一轮仍在运行\n' "$WAKE" >> "$LOG"
  exit 0
fi
LAST=$(printf '%s' "$RES" | tail -1 | clip 300)
status "$RC" "$START" "$END" "$LAST"
FAILF="$LOG_DIR/.paper_fail_${ET_DATE}"
if (( RC != 0 )); then
  printf '%s | start %s end %s | ⚠️ rc=%s %s\n' "$WAKE" "$START" "$END" "$RC" "$LAST" >> "$LOG"
  [[ -f "$TRES" ]] && dispatch_alerts "$TRES"   # rc=1（某仓位取数异常）时别的仓位的新告警已进发件箱，这里照发；崩溃无结果文件走下面的连续失败兜底
  printf 'x' >> "$FAILF"
  if [[ $(wc -c < "$FAILF") -eq 3 ]]; then
    notify "⚠️ 模拟仓调度连续失败" "$LAST"
    # 兜底文件会入库：只写「失败了、看哪里」，不写仓位内容
    printf '%s | ET %s | ⚠️ 模拟仓调度连续 3 次失败（rc=%s），详见私有 data/paper/paper_tick_*.log\n' \
      "$(date '+%F %H:%M %z')" "$WAKE" "$RC" >> "data/reports/FAILURE_${ET_DATE}.txt"
  fi
  exit 0
fi
rm -f "$FAILF"
dispatch_alerts "$TRES"
if [[ "$RES" != *"无到点动作"* ]]; then
  printf '%s | start %s end %s | %s\n' "$WAKE" "$START" "$END" "$LAST" >> "$LOG"
  if printf '%s' "$RES" | grep -qE ':(enter|skipped|missed|invalid_spec|stop|settle|settlement_pending|error|take_profit|time_exit|manual_close|hold_to_expiry|review_prompt)'; then
    notify "📒 模拟仓" "$(printf '%s' "$LAST" | clip 160)"
  fi
fi
exit 0
