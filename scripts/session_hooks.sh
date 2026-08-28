#!/bin/zsh
# 盘中例行钩子 —— 由 launchd 在几个固定时点唤醒，脚本自己按【美东时间】判断该做什么。
#
#   ET 09:00–09:15  盘前简报  —— 仅当【有待执行计划】或【今日有高影响事件】才跑
#   ET 09:40–09:55  持仓体检  —— 仅当【有持仓】才跑（开盘10分钟后，避开最宽点差）
#   ET 10:10–10:25  事件后复核 —— 仅当【今日有高影响事件】且【有持仓】才跑。
#                    美国宏观数据/讲话多在 ET 10:00 落地，事件后 IV 与价格会骤变，
#                    而止损阈值是按【真实可平仓价】定的，事件前的读数当场作废。
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
# 周末退出也留痕，否则「周末不跑」与「没被唤醒」在日志上仍无法区分

OUT="data/account/live"; mkdir -p "$OUT"

# ── 心跳日志 ──────────────────────────────────────────────────────
# 起因（用户 2026-08-28 问「时点生效了吗？live 捕获到没有」）：
# 这个脚本成功时写 md、跳过时【什么都不留】，于是「launchd 没唤醒」和
# 「唤醒了但判断为不该跑」在事后完全无法区分——我当时答不上来。
# 每次唤醒都记一行，才能事后核对。
# ⚠️ 只记【决策】不记【持仓内容】：日志会入库，账户数据一律留在 data/account/。
LOG_DIR="data/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/session_$(TZ=America/New_York date +%Y-%m).log"
hb() {  # $1=一句话结果
  printf '%s ET %s | 周%s | %s\n' "$ET_DATE" \
    "$(TZ=America/New_York date +%H:%M:%S)" "$ET_DOW" "$1" >> "$LOG"
}

if (( ET_DOW >= 6 )); then hb "周末，不跑"; exit 0; fi

notify() {  # $1=标题 $2=正文
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Glass\"" 2>/dev/null || true
}

# ── ① 盘前简报（ET 09:00–09:15）：仅在有计划或有大事件时 ──
if (( ET_MIN >= 540 && ET_MIN <= 555 )); then
  F="$OUT/${ET_DATE}_premarket.md"
  if [[ -f "$F" ]]; then hb "①盘前：今日已出，跳过"; fi
  if [[ ! -f "$F" ]]; then
    REASON=$("$PY" - <<'PYEOF'
import sys; sys.path.insert(0, ".")
# ⚠️ 不吞异常：检查失败必须与「没事发生」区分开，否则静默失效时看起来像一切正常。
# 任一检查出错就当作【需要触发】并把错误写进原因里 —— 宁可多跑一次，不可静默漏掉。
bits = []
try:
    from undertow.soul.plan import load_plans
    # ⚠️ TradePlan 的标准状态是 waiting/active/done/cancelled（见 soul/plan.py）。
    # 早先这里写的是 draft/ready —— 那两个值根本不在标准集合里，导致「有待执行计划」
    # 这个触发条件永远不成立。draft/ready 保留只为兼容我手工写过的非标准值。
    # （codex review 2026-08-27）
    pend = [p for p in load_plans() if p.status in ("waiting", "draft", "ready")]
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
      # ⚠️ 先写临时文件、逐条检查退出码，全部成功才原子改名。
      # 旧写法一上来就创建目标文件，任何一步失败也会留下"成功"文件，
      # 幂等检查随后永久阻止当日重试——一次短暂故障就让当天彻底没有简报。
      # （codex review 2026-08-26）
      # 并发互斥：同一时点多次唤醒可能重叠。用 mkdir 做原子锁，临时文件带 PID。
      LOCK="${OUT}/.lock_premarket_${ET_DATE}"
      if ! mkdir "$LOCK" 2>/dev/null; then
        hb "①盘前：撞锁，跳过"; echo "[盘前] 另一实例正在运行 —— 跳过"; exit 0
      fi
      trap 'rmdir "$LOCK" 2>/dev/null' EXIT
      TMP="${F}.partial.$$"
      { echo "# 盘前简报 $ET_DATE（ET $(TZ=America/New_York date +%H:%M)）"; echo
        echo "**触发原因**：$REASON"; echo; } > "$TMP"
      OK=1
      for CMD in "plan" "calendar --within 3" "live"; do
        if ! "$PY" -m undertow.cli ${=CMD} >> "$TMP" 2>&1; then
          echo "[盘前] 子命令失败：$CMD —— 保留 $TMP，等下一次唤醒重试" >&2
          OK=0; break
        fi
        echo "" >> "$TMP"
      done
      if (( OK )); then
        mv "$TMP" "$F"
        notify "📋 盘前简报" "$REASON"
        hb "①盘前：✅ 已出简报"; echo "[盘前] $REASON → $F"
      else
        hb "①盘前：❌ 子命令失败"; notify "⚠️ 盘前简报失败" "子命令出错，见 ${TMP}"
      fi
    else
      hb "①盘前：无计划无大事件，跳过"; echo "[盘前] 无待执行计划、无高影响事件 —— 跳过"
    fi
  fi
fi

# ── ② 开盘后持仓体检（ET 09:40–09:55）：仅在有持仓时 ──
if (( ET_MIN >= 580 && ET_MIN <= 595 )); then
  F="$OUT/${ET_DATE}_live.md"
  if [[ -f "$F" ]]; then hb "②体检：今日已出，跳过"; fi
  if [[ ! -f "$F" ]]; then
    # 同上：live 失败不得写成"成功"文件，否则当日不再重试
    LOCK2="${OUT}/.lock_live_${ET_DATE}"
    if ! mkdir "$LOCK2" 2>/dev/null; then
      hb "②体检：撞锁，跳过"; echo "[体检] 另一实例正在运行 —— 跳过"; exit 0
    fi
    trap 'rmdir "$LOCK2" 2>/dev/null' EXIT
    if ! RES=$("$PY" -m undertow.cli live 2>&1); then
      hb "②体检：❌ live 失败，等下次重试"; echo "[体检] live 执行失败 —— 不落盘，等下一次唤醒重试" >&2
      notify "⚠️ 持仓体检失败" "$(printf '%s' "$RES" | tail -1)"
      exit 0
    fi
    if [[ "$RES" == *"当前无持仓"* ]]; then
      hb "②体检：无持仓，跳过"; echo "[体检] 无持仓 —— 跳过"
    else
      printf '# 开盘后持仓体检 %s（ET %s）\n\n%s\n' \
        "$ET_DATE" "$(TZ=America/New_York date +%H:%M)" "$RES" > "$F"
      # 告警行（⚠️/✅ 开头的项）直接推送，其余静默落盘
      ALERT=$(printf '%s\n' "$RES" | grep -E '⚠️|✅ 已达' | head -2 | tr '\n' ' ')
      SUM=$(printf '%s\n' "$RES" | grep -E '^\*\*总敞口' | head -1)
      notify "🩺 持仓体检" "${ALERT:-$SUM}"
      hb "②体检：✅ 已出体检"; echo "[体检] → $F"
    fi
  fi
fi

# ── ③ 事件后复核（ET 10:10–10:25）：仅当今日有高影响事件 且 有持仓 ──
# 美国宏观数据/美联储讲话多在 ET 10:00 落地。事件后 IV 与价格骤变，
# 事件前那次体检的「距止损 X%」当场作废 —— 必须重新核一次真实可平仓价。
if (( ET_MIN >= 610 && ET_MIN <= 625 )); then
  F3="$OUT/${ET_DATE}_postevent.md"
  if [[ -f "$F3" ]]; then hb "③事件后：今日已出，跳过"; fi
  if [[ ! -f "$F3" ]]; then
    HI=$("$PY" - <<'PYEOF'
import sys; sys.path.insert(0, ".")
# 只在今日确有 🔴 高影响事件时才跑；检查出错一律当作"要跑"（宁可多跑，不可静默漏掉）
try:
    from undertow.core.calendar import load_events, merge
    from undertow.core.clock import market_today
    from undertow.collect.faireconomy_cal import FairEconomyCalSource
    ev = merge(load_events(), FairEconomyCalSource().fetch_events(use_cache=True))
    hi = [e for e in ev if e.date == market_today() and e.importance == "high"]
    print(" / ".join(e.name for e in hi[:3]) if hi else "")
except Exception as e:
    print(f"⚠️事件检查失败({type(e).__name__})——按有事件处理")
PYEOF
)
    if [[ -n "${HI// /}" ]]; then
      LOCK3="${OUT}/.lock_postevent_${ET_DATE}"
      if ! mkdir "$LOCK3" 2>/dev/null; then
        hb "③事件后：撞锁，跳过"; echo "[事件后] 另一实例正在运行 —— 跳过"; exit 0
      fi
      trap 'rmdir "$LOCK3" 2>/dev/null' EXIT
      if ! RES3=$("$PY" -m undertow.cli live 2>&1); then
        hb "③事件后：❌ live 失败，等下次重试"; echo "[事件后] live 失败 —— 不落盘，等下一次唤醒重试" >&2
        notify "⚠️ 事件后复核失败" "$(printf '%s' "$RES3" | tail -1)"
        exit 0
      fi
      if [[ "$RES3" == *"当前无持仓"* ]]; then
        hb "③事件后：无持仓，跳过"; echo "[事件后] 无持仓 —— 跳过"
      else
        printf '# 事件后持仓复核 %s（ET %s）

**今日高影响事件**：%s

%s
'           "$ET_DATE" "$(TZ=America/New_York date +%H:%M)" "$HI" "$RES3" > "$F3"
        # 距止损 <20% 的持仓单独拎出来告警：止损是手动的，必须当面提醒
        NEAR=$(printf '%s
' "$RES3" | awk -F'|' '
          /距止损/ {next}
          NF>7 {gsub(/[ %]/,"",$9); if ($9 != "" && $9+0 > 0 && $9+0 < 20) print "⚠️距止损仅" $9 "%"}' | head -2 | tr "\n" " ")
        SUM3=$(printf '%s
' "$RES3" | grep -E "^\*\*总敞口" | head -1)
        notify "🔔 事件后复核（$HI）" "${NEAR:-$SUM3}"
        hb "③事件后：✅ 已出复核"; echo "[事件后] $HI → $F3"
      fi
    else
      hb "③事件后：今日无🔴事件，跳过"; echo "[事件后] 今日无高影响事件 —— 跳过"
    fi
  fi
fi

# ── 不在任何窗口：也要留痕 ────────────────────────────────────────
# 没有这一行，「launchd 根本没唤醒」和「唤醒了但不在窗口」看起来一模一样。
if ! (( (ET_MIN >= 540 && ET_MIN <= 555) || (ET_MIN >= 580 && ET_MIN <= 595) \
     || (ET_MIN >= 610 && ET_MIN <= 625) )); then
  hb "唤醒但不在任何窗口（ET_MIN=$ET_MIN），静默退出"
fi
