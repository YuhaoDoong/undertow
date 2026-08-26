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
export PYTHONPATH="$PWD"
LOG="data/history/events/watch.log"
mkdir -p "$(dirname "$LOG")"

# 交给 Python 判断窗口并输出待捕任务（label|事件名|阶段），无任务则无输出
# ⚠️ 发现阶段也必须检查退出码：导入错误/日历异常时脚本会得到空 TASKS，
# 随后按"没有任务"成功退出——静默漏捕，且看起来一切正常。（codex review 2026-08-27）
TASKS=$("$PY" - <<'PYEOF' 2>>"$LOG"
import sys, json, pathlib, datetime as dt
sys.path.insert(0, ".")
from undertow.core.calendar import load_events, merge
from undertow.core.clock import market_today
from undertow.collect.faireconomy_cal import FairEconomyCalSource

# 三个时点（相对事件时间，分钟）与窗口宽度
PHASES = [("before", -30), ("after", +10), ("postopen", None)]  # postopen = 开盘后30分
WINDOW = 12          # 分钟：|now - target| <= WINDOW 即触发（配合每10分钟唤醒）
OPEN_ET = (9, 30)    # 美股开盘

def et_now():
    # ET = UTC-4（夏令时）/-5（冬令时）；用 zoneinfo 精确处理
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))

now = et_now()
today = market_today()
# 合并：手维护锚点 + FairEconomy 实时 feed（PCE/非农等的精确时间只在 feed 里）
try:
    manual = load_events()
except Exception:
    manual = []
try:
    events = merge(manual, FairEconomyCalSource().fetch_events(use_cache=True))
except Exception:
    events = manual
# 只要今天的【高影响】事件
highs = [e for e in events if e.date == today and e.importance == "high"]
if not highs:
    raise SystemExit

out = []
snapdir = pathlib.Path("data/history/events")
# 同一时点可能撞多个高影响事件（如 PCE 与 GDP 同为 08:30）→ 按 (时点,阶段) 去重，只捕一次
buckets = {}
for e in highs:
    hhmm = (e.time_et or "").strip()
    try:
        hh, mm = (int(x) for x in hhmm.split(":")[:2])
    except Exception:
        hh, mm = 8, 30
    ev_t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    open_t = now.replace(hour=OPEN_ET[0], minute=OPEN_ET[1], second=0, microsecond=0)
    for phase, off in PHASES:
        target = (open_t + dt.timedelta(minutes=30)) if off is None else (ev_t + dt.timedelta(minutes=off))
        # ⚠️ 必须【到点之后】才捕，不能提前。旧版用 |now-target|<=WINDOW，于是落进窗口的
        # 第一个唤醒点就触发——2026-08-26 实测 after 目标 ET08:40 却在 ET08:31 触发，
        # 数据落地才 1 分钟，等于什么反应都没捕到。改为只接受 [target, target+WINDOW]。
        lag_min = (now - target).total_seconds() / 60.0
        if lag_min < 0 or lag_min > WINDOW:
            continue
        key = (target.strftime("%H%M"), phase)
        buckets.setdefault(key, []).append(e.name)

for (hhmm, phase), names in sorted(buckets.items()):
    token = "".join(ch for ch in names[0] if ch.isalnum())[:14] or "EVENT"
    if len(names) > 1:
        token += f"+{len(names)-1}"
    label = f"{token}-{phase}"
    if (snapdir / f"{today.isoformat()}_{label}.json").exists():
        continue                      # 幂等：已捕过
    out.append(f"{label}|{' / '.join(names)}|{phase}")
print("\n".join(out))
PYEOF
)

DISCOVER_RC=$?
if (( DISCOVER_RC != 0 )); then
  echo "[$(date '+%F %T')] ❌ 任务发现阶段失败（退出码 $DISCOVER_RC）—— 这不是「没有事件」" >> "$LOG"
  /usr/bin/osascript -e "display notification \"事件发现失败，见 watch.log\" with title \"⚠️ undertow 事件捕捉\" sound name \"Basso\"" 2>/dev/null || true
  exit 1
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
    /usr/bin/osascript -e "display notification \"$EVNAME · $PHASE 快照已捕\" with title \"📸 undertow 事件捕捉\" sound name \"Glass\"" 2>/dev/null || true
  else
    echo "[$(date '+%F %T')] ❌ $LABEL 捕捉失败（退出码或文件缺失）" >> "$LOG"
    /usr/bin/osascript -e "display notification \"$EVNAME · $PHASE 捕捉失败，见 watch.log\" with title \"⚠️ undertow 事件捕捉\" sound name \"Basso\"" 2>/dev/null || true
  fi
done

# 快照入 git（市场数据、非个人）
if [[ -n "$(git status --porcelain data/history/events 2>/dev/null)" ]]; then
  git add data/history/events >/dev/null 2>&1
  git commit -q -m "data: 事件影响快照 $(date '+%F %H:%M') （自动捕捉）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" >/dev/null 2>&1 && \
  git push -q origin main >/dev/null 2>&1
fi
