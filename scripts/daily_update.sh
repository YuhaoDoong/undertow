#!/bin/zsh
# undertow 每日自动更新（launchd 定时触发，无 LLM 参与）：
#   快照当日期权链 → 有新持仓数据才出报告 → commit + push（= 备份）
# 时窗守卫：只在 ET 凌晨 1:00–8:59 运行（OCC 隔夜 OI 已更新、美股未开盘），
# 错过窗口（如合盖补跑落到美盘时段）宁可跳过也不落脏数据。
# 多时点重试：plist 在 ET02:05 主跑 + ET07:00/08:00/08:45 重试。OCC 隔夜 OI
# 的发布时刻有波动（实测 ET02:27 常未结算、ET08:xx 已结算），太早的时点抓到的
# OI 与上一交易日逐行相同 → chain_fingerprint 判为无新持仓 → 不落盘、不出报告，
# 交给后续时点在 OCC 发布后再抓。本脚本幂等：当日报告一旦提交，后续时点即跳过。
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

cd /Users/yhdong/Trading

ET_NOW=$(TZ=America/New_York date '+%F %H:%M')
ET_DATE=$(TZ=America/New_York date +%F)
ET_HOUR=$((10#$(TZ=America/New_York date +%H)))
# 告警 = 弹通知 + 落兜底文件。
# ⚠️ 只弹通知不够：launchd 环境下 osascript 未必能弹（用户可能关了通知、
# 或不在 GUI 会话），而我们现在恰恰在修"静默失败"。兜底文件纳入 git 一并备份，
# 事后一定查得到。文件用 append，同一天多次失败都留痕。
alert() {  # $1=标题 $2=正文
  local f="data/reports/FAILURE_${ET_DATE}.txt"
  printf '%s | ET %s | %s\n  %s\n' "$(date '+%F %H:%M %Z')" "$ET_NOW" "$1" "$2" >> "$f"
  echo "[告警] $1 — $2" >&2
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" 2>/dev/null || true
}
notify() { alert "$@"; }   # 兼容旧调用名

# —— 运行日志归档进仓库 ——
# 用户 2026-08-28 提出：日志要能核对。原先只写 ~/Library/Logs/undertow-daily.log，
# 在仓库外、不进 git、换机器就没了 —— 而今天排查「ET02:00 到底成没成功」
# 正是靠翻它才查清的。现在每次运行的完整输出同时追加到 data/logs/，随每日提交备份。
RUNLOG="data/logs/daily_$(TZ=America/New_York date +%Y-%m).log"
mkdir -p data/logs
exec > >(tee -a "$RUNLOG") 2>&1
echo "==== $(date '+%F %H:%M %Z') | ET $ET_NOW ===="
if (( ET_HOUR < 1 || ET_HOUR >= 9 )); then
    echo "[跳过] ET ${ET_HOUR}时 不在快照窗口(1:00–8:59)——避免旧OI/盘中脏数据"
    exit 0
fi

# —— 末班车兜底：最后一个重试点（ET08:45）跑完若仍缺当日快照，必须当场告警 ——
# 这是"整天没数据"的最后一道防线。前面每个时点失败都会推送，但如果全天所有时点
# 都因 OCC 未结算而静默跳过（这是【正常】行为，不推送），到收盘前就没人知道
# 当天缺数据了。整条交易流程依赖每日研报，缺一天必须让人当场知道。
# —— 末班车识别：不能写死 ET 时刻 ——
# ⚠️ codex review 2026-08-28 指出：plist 时点是【本地时间】，ET 随夏令时漂 1 小时。
# 夏令时 本地20:45→ET08:45；冬令时 本地20:45→ET07:45。
# 若判据写死 "ET_MIN>=08:30"，**冬令时半年内永远不成立** ——
# 修静默失败的代码自己会静默失效，正是我们要消灭的那类 bug。
# 改为：直接读 plist 的本地触发时刻，判断"本次是否为当日最后一个时点"。
LAST_LOCAL=$(python3 - <<'PYEOF'
import plistlib, pathlib
try:
    d = plistlib.loads((pathlib.Path.home() /
        "Library/LaunchAgents/com.yuhaodoong.undertow.daily.plist").read_bytes())
    pts = [(x.get("Hour", 0), x.get("Minute", 0)) for x in d.get("StartCalendarInterval", [])]
    print(max(h * 60 + m for h, m in pts) if pts else -1)
except Exception:
    print(-1)          # 读不到就退化为"不是末班车"，宁可不报也不误报
PYEOF
)
LOCAL_MIN=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
IS_LAST_SLOT=0
# 允许 launchd 迟到几分钟：落在最后时点之后即算末班车
if (( LAST_LOCAL >= 0 && LOCAL_MIN >= LAST_LOCAL )); then IS_LAST_SLOT=1; fi

# 幂等守卫：当日报告已提交（早前时点已成功）→ 后续重试点直接跳过，省掉重复抓取/出报告
# ⚠️ 幂等守卫必须看【全部期权品种是否都已有当日快照】，不能只看 gold 的报告。
# 旧写法：`git cat-file -e HEAD:data/reports/gold_$DATE.html` —— 一旦 gold 先结算
# 并提交，后续所有重试点直接 exit 0，那些 OCC 结算较晚的品种当天就再也拿不到链。
# 2026-08-27 实测暴露：ET01:06 时 gold/wti/qqq/tqqq 已结算，而 silver/tlt/spy/iwm
# 的 OI 仍与上一交易日逐行相同 —— 若此时提交，这四个品种当天的链就永久缺失。
# 链不可再生，缺一天就是永久少一天。
EXPECTED=$(python3 - <<'PYEOF'
import sys; sys.path.insert(0, ".")
from undertow.core.config import load_config
print(" ".join(v.options.symbol for v in load_config().instruments.values() if v.options))
PYEOF
)
MISSING=""
for SYM in ${=EXPECTED}; do
    [[ -f "data/snapshots/options/${SYM}/${ET_DATE}.json.gz" ]] || MISSING="$MISSING $SYM"
done
if [[ -z "${MISSING// /}" ]]; then
    echo "[跳过] 全部品种(${EXPECTED})均已有 ${ET_DATE} 快照，本时点无需重复运行"
    exit 0
fi
echo "[待补] 尚缺当日快照：${MISSING}"
if (( IS_LAST_SLOT )); then
    # 已是末班车还缺 → 今天大概率就补不上了，当场告警（不 exit，仍尝试抓一次）
    alert "🚨 末班车仍缺当日快照" "仍缺:${MISSING}。这是当日最后一个重试点，缺则当天无数据。"
fi


# ⚠️ 快照失败必须【当场推送】，不能只写进日志。
# 2026-08-28 复盘发现：8/21 ET02:07、8/22 ET02:09 两次全部品种「网络错误
# nodename nor servname」→「没有保存任何快照」，**只写进了日志文件**。
# 用户不会去翻日志，若当天后续重试点也失败，他会以为一切正常而实际当天无数据。
# 整条交易流程依赖每日研报，静默失败是最危险的失败方式。
# ⚠️ 判成败只读【机器可读状态 JSON】，绝不 grep 人读文案。
# codex review 2026-08-28：靠 grep 中文串（'快照失败'/'没有保存任何快照'）是脆弱耦合，
# 改一句提示文案告警就静默失效 —— 而我们恰恰在修"静默失败"。
# 另：`$(...) || true` 会把原始退出码永远变成 0（实测），进程崩溃会被当成正常跑完。
SNAP_ST="data/logs/.status_snapshot_${ET_DATE}.json"
rm -f "$SNAP_ST"
set +e
python3 -m undertow snapshot --status-file "$SNAP_ST"
SNAP_RC=$?
set -e
# 状态文件缺失/损坏 = crashed，与"跑完但失败"必须区分开
SNAP_JSON=$(python3 - "$SNAP_ST" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    if d.get("schema") != 1:
        raise ValueError("schema")
    bad = ",".join(i["instrument"] for i in d.get("items", []) if i.get("status") == "failed")
    print(f"{d.get('overall','crashed')}|{d.get('n_saved',0)}|{d.get('n_failed',0)}|{bad}")
except Exception:
    print("crashed|0|0|")
PYEOF
)
SNAP_OVERALL="${SNAP_JSON%%|*}"; _R="${SNAP_JSON#*|}"
SNAP_SAVED="${_R%%|*}"; _R="${_R#*|}"
SNAP_NFAIL="${_R%%|*}"; SNAP_BAD="${_R#*|}"
echo "[状态] snapshot overall=$SNAP_OVERALL saved=$SNAP_SAVED failed=$SNAP_NFAIL rc=$SNAP_RC"
case "$SNAP_OVERALL" in
  crashed)
    alert "🚨 快照进程异常（ET $ET_NOW）" "rc=$SNAP_RC 且状态文件缺失/损坏——无法确认当日是否有数据"
    exit 1 ;;
  failed)
    alert "🚨 快照全部失败（ET $ET_NOW）" "${SNAP_NFAIL} 个品种抓取失败：${SNAP_BAD}。请检查网络/接口。"
    exit 1 ;;
  partial)
    alert "⚠️ 部分品种快照失败（ET $ET_NOW）" "${SNAP_NFAIL} 个失败：${SNAP_BAD}（另 ${SNAP_SAVED} 个成功）" ;;
  unchanged)
    # 全部因「与上一交易日逐行相同」跳过 → OCC 未结算，**正常**，静默重试
    echo "[跳过] 无新持仓快照（休市/OI未结算/重复）——等下一时点重试"
    exit 0 ;;
esac

# 休市日 / OCC 未结算 → 指纹去重不落盘 → 无新数据就不出报告、不提交（交给后续重试点）。
# ⚠️ 判据必须来自【本次运行的状态 JSON】(SNAP_SAVED)，不能用 git status --porcelain：
# 后者会把【运行前就存在的脏文件】（上一次跑剩的、手工改的）当成"本次有新快照"，
# 于是在实际什么都没抓到的日子照样出报告并提交（codex review 2026-08-28）。
if (( SNAP_SAVED == 0 )); then
    echo "[跳过] 本次运行未落盘任何新快照——等下一时点重试"
    exit 0
fi

# 品种分两类：
#   交易品种 —— gold silver qqq tqqq（有实盘或计划仓位）
#   分析品种 —— wti tlt spy iwm（不交易，但驱动/映射前者：利率→金银、标普持仓→纳指轮动）
#     iwm 的定位不同于 tlt/spy：它与 SPY 相关 0.89，信息大半冗余；加它是为了
#     **提前攒期权链历史**（链不可再生），等本金到位（约 $800）它就是交易候选——
#     点差仅 5%，远好于 TQQQ 的 20%。
#     tlt/spy 的价值不在价格（SPY 与 QQQ 日收益相关 0.95），在【持仓层与偏斜】：
#     实测投机资金在标普长期净空、纳指长期净多，周变化相关仅 -0.07 —— 信息完全独立。
RPT_ST="data/logs/.status_report_${ET_DATE}.json"
rm -f "$RPT_ST"
set +e
REPORT_OUT=$(python3 -m undertow report gold silver wti qqq tqqq tlt spy iwm \
             --no-snapshot --status-file "$RPT_ST" 2>&1)
RPT_RC=$?
set -e
echo "$REPORT_OUT"
RPT_JSON=$(python3 - "$RPT_ST" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    if d.get("schema") != 1:
        raise ValueError("schema")
    print(f"{d.get('overall','crashed')}|{','.join(d.get('failed', []))}")
except Exception:
    print("crashed|")
PYEOF
)
RPT_OVERALL="${RPT_JSON%%|*}"; RPT_BAD="${RPT_JSON#*|}"
echo "[状态] report overall=$RPT_OVERALL rc=$RPT_RC"
case "$RPT_OVERALL" in
  crashed)
    alert "🚨 研报进程异常（ET $ET_NOW）" "rc=$RPT_RC 且状态文件缺失/损坏"
    exit 1 ;;
  failed)
    alert "🚨 研报全部失败（ET $ET_NOW）" "$(printf '%s' "$REPORT_OUT" | tail -1)"
    exit 1 ;;
  partial)
    # ⚠️ cmd_report 只要有一个品种失败就 return 1，所以不能靠退出码分流，
    #    必须读 overall —— 否则"个别品种失败"这个分支永远不可达（codex review）。
    alert "⚠️ 部分品种研报失败（ET $ET_NOW）" "失败：${RPT_BAD}" ;;
esac

# —— 强信号推送：报告若打出 ⚡（近端资金流一边倒），弹 macOS 通知 + 落一份告警文件兜底 ——
# 动机：这种领先信号（复盘 8/19 黄金）值得当天就看到，别等翻报告。宁缺勿滥，多数日不触发。
STRONG_LINES=$(printf '%s\n' "$REPORT_OUT" | grep '⚡' || true)
if [[ -n "$STRONG_LINES" ]]; then
    # 提炼 "品种 ⚡等级方向" 精简摘要（去掉路径/可信度噪音）
    SUMMARY=$(printf '%s\n' "$STRONG_LINES" | sed -E 's/^ *([a-z]+) .*(⚡[^ ]*).*/\1 \2/' | paste -sd '；' -)
    echo "[强信号] $SUMMARY"
    # 兜底：写当日告警文件（即使通知没弹出也留痕；纳入 git 一并备份）
    printf '%s | %s\n%s\n' "$ET_DATE" "$SUMMARY" "$STRONG_LINES" \
        > "data/reports/ALERT_${ET_DATE}.txt"
    # macOS 通知（launchd 跑在用户 GUI 会话，display notification 可弹；失败不影响主流程）
    /usr/bin/osascript -e "display notification \"${SUMMARY}\" with title \"⚡ undertow 强信号\" subtitle \"近端资金流一边倒 · 点开报告看详情\" sound name \"Glass\"" 2>/dev/null || true
fi

git add data/snapshots data/reports data/history
if git diff --cached --quiet; then
    echo "[跳过] 无变更可提交"
    exit 0
fi
git commit -m "每日自动更新 $(TZ=America/New_York date +%F)：期权链快照+四品种报告（launchd 定时任务）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
echo "[完成] 已提交并推送"
