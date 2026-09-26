#!/bin/zsh
# 事件影响自动捕捉 —— 只针对【🔴高影响】事件，三个时点各捕一次快照。
#
#   ① 数据前 ~30 分钟  → 基准（期货实时价）
#   ② 数据后 ~10 分钟  → 期货已反应；ETF 盘前也有值（期权 IV 此时仍陈旧）
#   ③ 开盘后 ~30 分钟  → IV 终于更新，能看到真实的 vol crush
#
# 设计：本脚本每 10 分钟被 launchd 唤醒一次，自己判断"现在是否落在某个捕捉窗口"，
# 是则捕、否则静默退出。幂等——同一 (事件, 阶段) 已有快照就跳过，不会重复捕。
#
# 限制：launchd 只在本机醒着时触发。电脑睡眠/关机则错过——这种情况用对话里手动
# `undertow event <label>` 补捕即可。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY="${PYTHON:-python3}"
OSASCRIPT="${OSASCRIPT:-/usr/bin/osascript}"   # 测试可替换，避免演练时真弹通知
export PYTHONPATH="$PWD"
LOG="data/history/events/watch.log"
mkdir -p "$(dirname "$LOG")"

source scripts/lib_publish.sh           # publish_dirs：只提交本次运行产物（Codex 008 G10 / 009 N01）
PUBLISH_PENDING="data/logs/.publish_pending_auto"; export PUBLISH_PENDING   # 所有自动化任务共用
publish_begin data/history/events       # 记下运行前已有的未提交改动（他人的不发布）
trap 'publish_record data/history/events' EXIT   # 任何退出路径都记下本次写过的文件（含 watch.log）

notify() { $OSASCRIPT -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" 2>/dev/null; true; }

# 任务发现：scripts/event_discover.py（Codex 008 G05）。结构化状态写 .status_event_watch.json（gitignore），
# 每次唤醒都重写 → 它的 mtime 即心跳；状态分 failed / partial / tasks / unchanged。
# 判成败只看退出码与状态文件，不 grep 人读文案（AGENTS.md 静默失败第 2 条）。
ST="data/logs/.status_event_watch.json"
ET_DATE=$(TZ=America/New_York date +%F)
TASKS=$("$PY" scripts/event_discover.py --status-file "$ST" 2>>"$LOG"); DISCOVER_RC=$?
STATUS=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['status'])" "$ST" 2>/dev/null)
[[ -z "$STATUS" ]] && STATUS="crashed"          # 状态文件缺失/损坏 ≠ 跑完但失败

if (( DISCOVER_RC != 0 )) || [[ "$STATUS" == "failed" || "$STATUS" == "crashed" ]]; then
  echo "[$(date '+%F %T')] ❌ 任务发现失败（rc=$DISCOVER_RC，状态 $STATUS）—— 这不是「没有事件」" >> "$LOG"
  MK="data/logs/.event_fail_${ET_DATE}"
  if [[ ! -f "$MK" ]]; then                      # 每天告警一次；每次唤醒都会重试
    notify "⚠️ undertow 事件捕捉" "事件日历两个来源都失败（状态 $STATUS），见 watch.log"
    : > "$MK"
  fi
  exit 1
fi
if [[ "$STATUS" == "partial" ]]; then
  MK="data/logs/.event_partial_${ET_DATE}"
  if [[ ! -f "$MK" ]]; then
    echo "[$(date '+%F %T')] ⚠️ 事件来源不完整（一个来源失败或 feed 未覆盖今天）：仍按可用来源捕捉" >> "$LOG"
    notify "⚠️ undertow 事件捕捉" "事件日历只有一个来源可用，今日可能漏事件，见 watch.log"
    : > "$MK"
  fi
fi

[[ -z "${TASKS// /}" ]] && exit 0

echo "$TASKS" | while IFS='|' read -r LABEL EVNAME PHASE; do
  [[ -z "$LABEL" ]] && continue
  echo "[$(date '+%F %T')] 捕捉 $LABEL （$EVNAME / $PHASE）" >> "$LOG"
  # ⚠️ 必须检查退出码 + 确认快照文件真的落盘，才通知成功。
  # 旧写法无论 CLI 成功与否都弹"快照已捕"——导入错误/行情失败会被显示成捕捉成功。
  # （codex review 2026-08-26）
  if "$PY" -m undertow.cli event "$LABEL" gold silver qqq \
        --event "$EVNAME" --phase "$PHASE" >> "$LOG" 2>&1 \
     && [[ -s "data/history/events/$(TZ=America/New_York date +%F)_${LABEL}.json" ]]; then
    $OSASCRIPT -e "display notification \"$EVNAME · $PHASE 快照已捕\" with title \"📸 undertow 事件捕捉\" sound name \"Glass\"" 2>/dev/null || true
  else
    echo "[$(date '+%F %T')] ❌ $LABEL 捕捉失败（退出码或文件缺失）" >> "$LOG"
    $OSASCRIPT -e "display notification \"$EVNAME · $PHASE 捕捉失败，见 watch.log\" with title \"⚠️ undertow 事件捕捉\" sound name \"Basso\"" 2>/dev/null || true
  fi
done

# 快照入 git（市场数据、非个人）—— 只提交 data/history/events 下本次的新增/修改；
# 索引里有他人已暂存的文件 → 停止发布、产物保留、下次唤醒再发（不 stash / reset 他人工作）
PUBLISH_MANIFEST="data/logs/.event_publish_manifest"
export PUBLISH_MANIFEST
PUB=$(publish_dir "data: 事件影响快照 $(date '+%F %H:%M') （自动捕捉）

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" data/history/events); PUB_RC=$?
case $PUB_RC in
  0) ;;
  3) echo "[$(date '+%F %T')] ⏸ $PUB" >> "$LOG" ;;
  4) echo "[$(date '+%F %T')] ⚠️ 已提交但推送失败（下次随其它提交一起推）" >> "$LOG" ;;
  *) echo "[$(date '+%F %T')] ❌ 发布失败 rc=$PUB_RC" >> "$LOG" ;;
esac
exit 0
