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
echo "==== $(date '+%F %H:%M %Z') | ET $ET_NOW ===="
if (( ET_HOUR < 1 || ET_HOUR >= 9 )); then
    echo "[跳过] ET ${ET_HOUR}时 不在快照窗口(1:00–8:59)——避免旧OI/盘中脏数据"
    exit 0
fi

# —— 末班车兜底：最后一个重试点（ET08:45）跑完若仍缺当日快照，必须当场告警 ——
# 这是"整天没数据"的最后一道防线。前面每个时点失败都会推送，但如果全天所有时点
# 都因 OCC 未结算而静默跳过（这是【正常】行为，不推送），到收盘前就没人知道
# 当天缺数据了。整条交易流程依赖每日研报，缺一天必须让人当场知道。
IS_LAST_SLOT=0
if (( ET_HOUR >= 8 )); then IS_LAST_SLOT=1; fi

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
    /usr/bin/osascript -e "display notification \"仍缺:${MISSING}。这是当日最后一个重试点，缺则当天无数据。\" with title \"🚨 末班车仍缺当日快照\" sound name \"Basso\"" 2>/dev/null || true
    echo "[严重] 末班车(ET${ET_HOUR}时)仍缺：${MISSING}" >&2
fi

notify() {  # $1=标题 $2=正文
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" 2>/dev/null || true
}

# ⚠️ 快照失败必须【当场推送】，不能只写进日志。
# 2026-08-28 复盘发现：8/21 ET02:07、8/22 ET02:09 两次全部品种「网络错误
# nodename nor servname」→「没有保存任何快照」，**只写进了日志文件**。
# 用户不会去翻日志，若当天后续重试点也失败，他会以为一切正常而实际当天无数据。
# 整条交易流程依赖每日研报，静默失败是最危险的失败方式。
SNAP_OUT=$(python3 -m undertow snapshot 2>&1) || true
echo "$SNAP_OUT"
FAILS=$(printf '%s\n' "$SNAP_OUT" | grep -c '快照失败' || true)
if printf '%s\n' "$SNAP_OUT" | grep -q '没有保存任何快照'; then
    if (( FAILS > 0 )); then
        REASON=$(printf '%s\n' "$SNAP_OUT" | grep '快照失败' | head -1 | sed -E 's/.*失败: //; s/ \(http.*//')
        echo "[严重] 全部品种抓取失败（$FAILS 个）：$REASON" >&2
        notify "🚨 快照全部失败（ET $ET_NOW）" "${FAILS} 个品种抓取失败：${REASON}。当日数据可能缺失，请检查网络/接口。"
    else
        # 无 failure 行 = 全部因「与上一交易日逐行相同」被跳过 → OCC 未结算，正常
        echo "[跳过] 无新持仓快照（休市/OI未结算/重复）——等下一时点重试"
    fi
    exit 0
fi
if (( FAILS > 0 )); then
    # 部分失败也要报：有品种落盘了，但另一些抓不到
    REASON=$(printf '%s\n' "$SNAP_OUT" | grep '快照失败' | head -1 | sed -E 's/.*失败: //; s/ \(http.*//')
    notify "⚠️ 部分品种快照失败（ET $ET_NOW）" "${FAILS} 个品种抓取失败：${REASON}"
fi

# 休市日 / OCC 未结算（OI 与上一交易日逐行相同）→ 指纹去重不落盘 → 无新文件就
# 不出报告、不提交（交给后续重试点）；避免残缺报告（OI 旧、现价新）污染序列
if [[ -z $(git status --porcelain data/snapshots) ]]; then
    echo "[跳过] 无新持仓快照（休市/OI未结算/重复）——等下一时点重试"
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
if ! REPORT_OUT=$(python3 -m undertow report gold silver wti qqq tqqq tlt spy iwm --no-snapshot 2>&1); then
    echo "[严重] 研报生成失败" >&2
    echo "$REPORT_OUT" >&2
    notify "🚨 研报生成失败（ET $ET_NOW）" "$(printf '%s' "$REPORT_OUT" | tail -1)"
    exit 1
fi
echo "$REPORT_OUT"
# 研报是整条交易流程的输入：失败/缺品种都必须当场知道
RPT_FAIL=$(printf '%s\n' "$REPORT_OUT" | grep -c '研判报告失败' || true)
if (( RPT_FAIL > 0 )); then
    notify "⚠️ ${RPT_FAIL} 个品种研报失败（ET $ET_NOW）" \
           "$(printf '%s\n' "$REPORT_OUT" | grep '研判报告失败' | head -1)"
fi

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
