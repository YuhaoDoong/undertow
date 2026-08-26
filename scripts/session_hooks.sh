#!/bin/zsh
# 盘中例行钩子 —— 由 launchd 在几个固定时点唤醒，脚本自己按【美东时间】判断该做什么。
#
#   ET 09:00–09:15  盘前简报  —— 仅当【有待执行计划】或【今日有高影响事件】才跑
#   ET 09:40–09:55  持仓体检  —— 仅当【有持仓】才跑（开盘10分钟后，避开最宽点差）
#
# 为什么时点判断放在脚本里而不是 plist：plist 的 StartCalendarInterval 用的是本机
# 本地时间，美国夏令时切换后会整体漂 1 小时。故 plist 多挂几个 SGT 时点覆盖两种
# 夏令时状态，由脚本用真正的 ET 时间决定是否执行，不在窗口内就静默退出。
#
# 幂等：同一天同一任务已产出文件就跳过，重复唤醒无副作用。
# ⚠️ 输出含账户持仓，一律落 data/account/（已 gitignore），绝不入库。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$PWD"
PY="${PYTHON:-python3}"

ET_DATE=$(TZ=America/New_York date +%F)
ET_DOW=$(TZ=America/New_York date +%u)          # 1=周一 … 7=周日
ET_MIN=$(( 10#$(TZ=America/New_York date +%H) * 60 + 10#$(TZ=America/New_York date +%M) ))
(( ET_DOW >= 6 )) && exit 0                      # 周末不跑（节假日由行情为空自然兜底）

OUT="data/account/live"; mkdir -p "$OUT"

notify() {  # $1=标题 $2=正文
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Glass\"" 2>/dev/null || true
}

# ── ① 盘前简报（ET 09:00–09:15）：仅在有计划或有大事件时 ──
if (( ET_MIN >= 540 && ET_MIN <= 555 )); then
  F="$OUT/${ET_DATE}_premarket.md"
  if [[ ! -f "$F" ]]; then
    REASON=$("$PY" - <<'PYEOF'
import sys; sys.path.insert(0, ".")
# ⚠️ 不吞异常：检查失败必须与「没事发生」区分开，否则静默失效时看起来像一切正常。
# 任一检查出错就当作【需要触发】并把错误写进原因里 —— 宁可多跑一次，不可静默漏掉。
bits = []
try:
    from undertow.soul.plan import load_plans
    pend = [p for p in load_plans() if p.status in ("draft", "ready")]
    if pend:
        bits.append(f"{len(pend)} 个待执行计划（{', '.join(p.id for p in pend[:3])}）")
except Exception as e:
    bits.append(f"⚠️计划检查失败:{type(e).__name__}")
try:
    from undertow.core.calendar import load_events, merge
    from undertow.core.clock import market_today
    from undertow.collect.faireconomy_cal import FairEconomyCalSource
    ev = merge(load_events(), FairEconomyCalSource().fetch_events(use_cache=True))
    hi = [e for e in ev if e.date == market_today() and e.importance == "high"]
    if hi:
        bits.append("🔴高影响事件：" + " / ".join(e.name for e in hi[:3]))
except Exception as e:
    bits.append(f"⚠️事件检查失败:{type(e).__name__}")
print(" ｜ ".join(bits))
PYEOF
)
    if [[ -n "${REASON// /}" ]]; then
      echo "# 盘前简报 $ET_DATE（ET $(TZ=America/New_York date +%H:%M)）" > "$F"
      echo "" >> "$F"
      echo "**触发原因**：$REASON" >> "$F"
      echo "" >> "$F"
      "$PY" -m undertow.cli plan >> "$F" 2>&1
      echo "" >> "$F"
      "$PY" -m undertow.cli calendar --within 3 >> "$F" 2>&1
      echo "" >> "$F"
      "$PY" -m undertow.cli live >> "$F" 2>&1
      notify "📋 盘前简报" "$REASON"
      echo "[盘前] $REASON → $F"
    else
      echo "[盘前] 无待执行计划、无高影响事件 —— 跳过"
    fi
  fi
fi

# ── ② 开盘后持仓体检（ET 09:40–09:55）：仅在有持仓时 ──
if (( ET_MIN >= 580 && ET_MIN <= 595 )); then
  F="$OUT/${ET_DATE}_live.md"
  if [[ ! -f "$F" ]]; then
    RES=$("$PY" -m undertow.cli live 2>&1)
    if [[ "$RES" == *"当前无持仓"* ]]; then
      echo "[体检] 无持仓 —— 跳过"
    else
      printf '# 开盘后持仓体检 %s（ET %s）\n\n%s\n' \
        "$ET_DATE" "$(TZ=America/New_York date +%H:%M)" "$RES" > "$F"
      # 告警行（⚠️/✅ 开头的项）直接推送，其余静默落盘
      ALERT=$(printf '%s\n' "$RES" | grep -E '⚠️|✅ 已达' | head -2 | tr '\n' ' ')
      SUM=$(printf '%s\n' "$RES" | grep -E '^\*\*总敞口' | head -1)
      notify "🩺 持仓体检" "${ALERT:-$SUM}"
      echo "[体检] → $F"
    fi
  fi
fi
