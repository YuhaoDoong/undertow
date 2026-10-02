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

# Codex 009 N01：记下运行前 data/ 三个目录里已有的未提交改动 —— 发布时只提交本次运行产物
# （及本任务以前未发布成功的产物，见 lib_publish.sh），他人的改动留在工作区不动。
source scripts/lib_publish.sh
PUBLISH_PENDING="data/logs/.publish_pending_auto"; export PUBLISH_PENDING   # 所有自动化任务共用：彼此的未发布产物互认
publish_begin data/snapshots data/history data/reports
trap 'publish_record data/snapshots data/history data/reports' EXIT   # 早退路径也记下本次产物（如 FAILURE_ 凭证）

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
    fb = ",".join(d.get("fallback_used") or [])       # 主源停更、已由长桥兜住
    su = ",".join(d.get("stale_unresolved") or [])    # 主源停更且【没能】兜住
    print(f"{d.get('overall','crashed')}|{d.get('n_saved',0)}|{d.get('n_failed',0)}|{bad}|{fb}|{su}")
except Exception:
    print("crashed|0|0|||")
PYEOF
)
SNAP_OVERALL="${SNAP_JSON%%|*}"; _R="${SNAP_JSON#*|}"
SNAP_SAVED="${_R%%|*}"; _R="${_R#*|}"
SNAP_NFAIL="${_R%%|*}"; _R="${_R#*|}"
SNAP_BAD="${_R%%|*}"; _R="${_R#*|}"
SNAP_FB="${_R%%|*}"; SNAP_STALE="${_R#*|}"
echo "[状态] snapshot overall=$SNAP_OVERALL saved=$SNAP_SAVED failed=$SNAP_NFAIL rc=$SNAP_RC"\
"${SNAP_FB:+ fallback=$SNAP_FB}${SNAP_STALE:+ stale=$SNAP_STALE}"

# 降级成功【也要】出声：数据是补上了，但 Greeks 是本地 BS 自算的
# （主翼 |Δ| 最大偏差 0.22，足以把一条腿踢出/拉进方向判定的主翼区间）。
# 「兜住了」不等于「和平常一样」，读研报的人有权知道今天这份是备份源。
if [[ -n "$SNAP_FB" ]]; then
    alert "🔁 主源停更，已自动切长桥备份源（ET $ET_NOW）" \
          "品种：${SNAP_FB}。数据已补上、研报照常出，但 delta/gamma 为本地 BS 自算\
（主翼最大偏差 0.22），bid/ask 为 0。请留意主源是否恢复。"
fi
# 兜不住才是真事故：主源停更 + 备份源也拿不到 → 这一天的 OI 会永久丢失
if [[ -n "$SNAP_STALE" ]]; then
    alert "🚨 主源停更且备份源也失败（ET $ET_NOW）" \
          "品种：${SNAP_STALE}。两个独立源都拿不到新 OI —— 期权链不可再生，\
请立即人工核对 CBOE 接口与长桥 CLI。"
fi
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
    # 全部因「与上一交易日逐行相同」跳过 → OCC 未结算，**当天之内属正常**，静默重试。
    #
    # ⚠️ 但【连续跨交易日】都 unchanged 就不正常了，那是数据源挂掉的特征。
    # 2026-09-24 实测：CBOE 接口卡在 2026-09-22T15:59:59 超过 34 小时，
    # 9/23 四个时点全报「逐行相同」→ 一份没落盘、报告缺一天、**而且零告警**，
    # 是用户问「9/22 收盘的 OI 呢」才发现的。
    # 原设计只区分「今天还没结算」（正常）与「抓取失败」（告警），
    # 漏掉了第三种：「源还活着但停止更新」——它伪装成前者，却和后者一样致命。
    # 判据：最新快照距今已跨过 STALE_SESSIONS 个交易日仍无新数据 → 告警。
    LAST_SNAP=$(ls -1 data/snapshots/options/GLD/*.json.gz 2>/dev/null | tail -1 \
                | sed -E 's#.*/([0-9]{4}-[0-9]{2}-[0-9]{2})\.json\.gz#\1#')
    if [[ -n "$LAST_SNAP" ]]; then
        # ⚠️ 工作日差与阈值都来自 undertow.core.clock —— cmd_snapshot 的降级开关
        # 用的是同一份实现。这里再写一遍 while 循环必然与那边漂移。
        STALE_DAYS=$(python3 - "$LAST_SNAP" "$ET_DATE" <<'PYEOF'
import sys
from datetime import date
from undertow.core.clock import sessions_between
print(sessions_between(date.fromisoformat(sys.argv[1]),
                       date.fromisoformat(sys.argv[2])))
PYEOF
)
        STALE_SESSIONS=$(python3 -c 'from undertow.core.clock import STALE_SESSIONS; print(STALE_SESSIONS)')
        # ⚠️ 只在 CLI 没报 stale_unresolved 时才用这条 —— 否则同一个事件告警两次。
        # 保留它是因为它**不依赖状态文件解析**：CLI 太旧、字段改名、JSON 损坏时，
        # 这条仍然能响。两条判据同源（sessions_between），不会给出矛盾结论。
        if [[ -z "$SNAP_STALE" ]] && (( STALE_DAYS >= ${STALE_SESSIONS:-2} )); then
            alert "🚨 数据源疑似停更（ET $ET_NOW）" \
                  "最新快照 ${LAST_SNAP}，已跨 ${STALE_DAYS} 个工作日无新 OI。\
连续 unchanged 跨交易日 = 源停止更新，不是「今天还没结算」。请人工核对 CBOE 接口。"
        fi
    fi
    echo "[跳过] 无新持仓快照（休市/OI未结算/重复）——等下一时点重试"\
"${LAST_SNAP:+（最新快照 $LAST_SNAP，距今 ${STALE_DAYS:-?} 个工作日）}"
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
# ⚠️ 这里是【显式列出】而非跑全部品种，有意为之：
# 综合研判的方向票里 COT 是一层，而个股（googl）结构性地没有 CFTC 持仓报告
# —— 不是"待补"，是永远不会有。把它塞进来只会每天多一条 [警告] 与 partial 告警。
# 个股走 walls / gamma / flow（墙位层），不出综合研判。
# 快照层相反：EXPECTED 从 config 派生，个股会自动纳入每日抓取，
# 这样 ΔOI 与买卖方向才攒得起来。
REPORT_OUT=$(python3 -m undertow report gold silver wti qqq tqqq tlt spy iwm \
             --no-snapshot --status-file "$RPT_ST" 2>&1)
RPT_RC=$?
# 新研报体系 v2（用户 2026-09-30：「新建个研报体系……先只放期权墙……之前的研报也照常出」）：只放已证实内容，
# 目前只有期权墙总览；只读已落盘快照。失败只告警，不影响旧研报与后续步骤。
V2_OUT=$(python3 -m undertow report-v2 2>&1); V2_RC=$?
# 模拟仓台账报告（私有输出 data/paper/reports/，不入库）：每天盘前一份；失败只告警（放在 set -e 之前，失败不中断 daily）
PB_OUT=$(python3 scripts/paper_book.py 2>&1); PB_RC=$?
set -e
echo "$REPORT_OUT"
printf '%s\n' "$V2_OUT" | tail -1
# 台账内容私有：告警只写成败与返回码，不带输出内容（rc 3 = 已生成但局部不完整，如影子账汇总失败）
if (( PB_RC == 3 )); then alert "⚠️ 模拟仓台账局部不完整（ET $ET_NOW）" "已生成，见私有 data/paper/reports/.status_paper_book.json"
elif (( PB_RC != 0 )); then alert "⚠️ 模拟仓台账报告失败（ET $ET_NOW）" "rc=$PB_RC，上一份保留；详情见私有输出"; fi
if (( V2_RC != 0 && V2_RC != 3 )); then          # rc=3 = 有品种当日快照未到（正常，下一次 daily 再生成）
  alert "⚠️ 研报 v2 未完整生成（ET $ET_NOW）" "$(printf '%s' "$V2_OUT" | tail -1 | cut -c1-120)"
fi
RPT_JSON=$(python3 - "$RPT_ST" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    if d.get("schema") != 1:
        raise ValueError("schema")
    li = ";".join(f"{i.get('instrument')}:{i.get('error','')[:80]}" for i in d.get("ledger_issues", []))
    print(f"{d.get('overall','crashed')}|{','.join(d.get('failed', []))}|{li}")
except Exception:
    print("crashed||")
PYEOF
)
RPT_OVERALL="${RPT_JSON%%|*}"; _R="${RPT_JSON#*|}"
RPT_BAD="${_R%%|*}"; RPT_LEDGER="${_R#*|}"
# W02（Codex A02/A10）：前瞻台账写入失败/同日冲突不影响研报生成，但必须出声 ——
# 台账是不可再生的事前记录，缺一天就是永久缺一天。只写 stderr 在无人值守时等于没人知道。
if [[ -n "$RPT_LEDGER" ]]; then
    alert "⚠️ 前瞻台账未写入（ET $ET_NOW）" "$RPT_LEDGER"
fi
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

# —— 资金流异常观察推送：报告若打出 ⚡（近端资金流一边倒），弹 macOS 通知 + 落一份文件兜底 ——
# 原动机是把它当领先信号当天推送（复盘 8/19 黄金）；Codex 015 起它是 T3 观察（极强 9/16=56%，p=0.804），
# 仍推送是为了让人当天看到这组数字，但不再称为信号，也不参与任何结论。
STRONG_LINES=$(printf '%s\n' "$REPORT_OUT" | grep '⚡' || true)
if [[ -n "$STRONG_LINES" ]]; then
    # 提炼 "品种 ⚡等级方向" 精简摘要（去掉路径/可信度噪音）
    SUMMARY=$(printf '%s\n' "$STRONG_LINES" | sed -E 's/^ *([a-z]+) .*(⚡[^] ]*).*/\1 \2/' | paste -sd '；' -)
    echo "[强信号] $SUMMARY"
    # 兜底：写当日告警文件（即使通知没弹出也留痕；纳入 git 一并备份）
    printf '%s | %s\n%s\n' "$ET_DATE" "$SUMMARY" "$STRONG_LINES" \
        > "data/reports/ALERT_${ET_DATE}.txt"
    # macOS 通知（launchd 跑在用户 GUI 会话，display notification 可弹；失败不影响主流程）
    # Codex 015：强信号历史检验未通过（T3）→ 通知只作「资金流异常观察」，明说不参与结论
    /usr/bin/osascript -e "display notification \"${SUMMARY}\" with title \"🔎 undertow 资金流异常观察\" subtitle \"强信号未通过验证 · 不参与结论 · 数字见报告\" sound name \"Glass\"" 2>/dev/null || true
fi

# —— 台账回填：用真实收盘价补齐前瞻收益 ——
# 2026-09-08 发现：回填一直是【手动】的 —— 本脚本只跑 snapshot 和 report，
# 于是 8/31 起 8 天没人填，signal_ledger 的 forward_* / trading_gap 全空。
# 而台账是本项目唯一认可的统计口径（手工台账因幸存者偏差已废弃），
# 断了没有任何地方会出声，等于统计在裸奔 —— 这跟本脚本要防的"静默失败"是同一类。
# 放在报告之后、提交之前：backfill 要求价格序列已走过信号日，当天新记的那行
# 本来就填不了，填的是前几天那些已经到期的格子；结果随 data/history 一并提交。
# 价格源是外部依赖，失败只告警不阻断（快照和报告已经成功，不该因回填丢掉提交）。
set +e
BF_OUT=$(python3 -m undertow signals --backfill 2>&1)
BF_RC=$?
set -e
if (( BF_RC == 0 )); then
    printf '%s\n' "$BF_OUT" | grep -E '回填|警告' || true
else
    alert "⚠️ 台账回填失败（ET $ET_NOW）" "rc=$BF_RC：$(printf '%s' "$BF_OUT" | tail -1)"
fi

# —— 前瞻配对影子账（W05，Codex 004 蓝图）：盘前冻结当日机会 + 收盘后结算 ——
# 只读：只读快照/日线/盘口，写 data/history/shadow/；从不下单。
# capture 必须在研报之后（快照已落盘、session 已认证）且在开盘前 —— 迟到的记录会被
# report 归为 late_record、不进主样本（prospective_ok 按 recorded_at < 09:30 ET 判）。
set +e
SH_ST="data/logs/.status_shadow_${ET_DATE}.json"
SH_OUT=$(python3 -m undertow shadow capture --status-file "$SH_ST" 2>&1); SH_RC=$?
SH_OUT2=$(python3 -m undertow shadow settle 2>&1); SH_RC2=$?
set -e
printf '%s\n%s\n' "$SH_OUT" "$SH_OUT2" | grep -E "候选腿|更新|⚠️" | head -20 || true
if (( SH_RC != 0 || SH_RC2 != 0 )); then
    alert "⚠️ 影子账采集/结算失败（ET $ET_NOW）" \
          "capture rc=$SH_RC settle rc=$SH_RC2：$(printf '%s\n%s' "$SH_OUT" "$SH_OUT2" | grep '⚠️' | head -2 | tr '\n' ' ')"
fi

# 方向判断台账（用户 2026-09-28；预登记 skew-reading-v1）：快照已落盘 → 开盘前记录金银偏度读数（首份冻结），
# 再回填已成熟的 1/5/10 日走势。只读；读数是未验证的 T3，不进研报结论。失败告警，不阻断提交。
set +e
DL_OUT=$(python3 -m undertow dirledger record 2>&1); DL_RC=$?
DS_OUT=$(python3 -m undertow dirledger score 2>&1); DS_RC=$?
# 期权多层同向（Codex 022 冻结：conviction-h1-v1-20260928，正式起点 2026-09-29；积累期 T3，不参与决策）
CV_OUT=$(python3 -m undertow dirledger conviction-record 2>&1); CV_RC=$?
# 外部作者价位类判断计分（私有：只读写 data/soul/；用户 2026-09-29「多记录多测试」）；失败不阻断
if [[ -f data/soul/author_levels.jsonl ]]; then
  AL_OUT=$(python3 scripts/author_levels_score.py 2>&1); AL_RC=$?
  (( AL_RC != 0 )) && alert "⚠️ 作者价位计分失败（ET $ET_NOW）" "$(printf '%s' "$AL_OUT" | tail -1 | cut -c1-120)"
fi
# 资金流买卖方推断验证（协议 docs/prereg/2026-09-29_flow_side_check_v0.md）：对前一交易日 X 对照（需今天盘前快照 = X 的结算）；失败不阻断
FS_DAY=$(python3 -c 'from undertow.core import market_calendar as mc; from undertow.core.clock import market_today; print(mc.prev_trading_day(market_today()))' 2>/dev/null)
if [[ -n "$FS_DAY" ]]; then
  FS_OUT=$(python3 scripts/flow_side_check.py "$FS_DAY" gold silver 2>&1); FS_RC=$?
  # rc=3 = 输入未齐（快照/逐分钟未到）→ pending，不告警、不覆盖已有结果；其它非零才告警
  if (( FS_RC != 0 && FS_RC != 3 )); then alert "⚠️ 资金流代理对照失败（ET $ET_NOW）" "$(printf '%s' "$FS_OUT" | tail -1 | cut -c1-120)"; fi
fi
# 逐到期持仓画像（用户 2026-09-29：到期日类型 Q/M/W 与磁吸研究）：开盘前首份冻结，只记录不产生信号
EP_OUT=$(python3 -m undertow shadow expiry-profile 2>&1); EP_RC=$?
set -e
printf '%s\n' "$DL_OUT" | head -3
printf '%s\n' "$CV_OUT" | head -9
printf '%s\n' "$EP_OUT" | tail -3
if (( DL_RC != 0 || DS_RC != 0 || CV_RC != 0 || EP_RC != 0 )); then
    alert "⚠️ 方向判断台账失败（ET $ET_NOW）" "record rc=$DL_RC score rc=$DS_RC conviction rc=$CV_RC expiry-profile rc=$EP_RC：$(printf '%s\n%s\n%s\n%s' "$DL_OUT" "$DS_OUT" "$CV_OUT" "$EP_OUT" | grep '⚠️' | head -2 | tr '\n' ' ')"
fi

# 研报输入存档（用户 2026-09-28：「数据永远是最主要的」）：价格/波动率/FRED/COT 原始下载只在 data/cache
# （gitignore、被覆盖、无版本）。研报刚跑完、缓存最新 → 存每日尾部 + 每月全量到 data/history/inputs/，随下方提交入库。
set +e
AI_OUT=$(python3 -m undertow archive-inputs --status-file "data/logs/.status_inputs_${ET_DATE}.json" 2>&1); AI_RC=$?
set -e
printf '%s\n' "$AI_OUT" | head -3
if (( AI_RC != 0 )); then
    alert "⚠️ 研报输入存档失败（ET $ET_NOW）" "rc=$AI_RC：$(printf '%s' "$AI_OUT" | grep '⚠️' | head -2 | tr '\n' ' ')"
fi

# 盘中时段采样收尾核对（Codex 014 N14-02）：上一交易日各品种各桶是否都成功观测。只读落盘记录。
set +e
PREV_TD=$(python3 -c 'from undertow.core import market_calendar as mc; from undertow.core.clock import market_today as t; d=mc.prev_trading_day(t()); print(d or "")')
if [[ -n "$PREV_TD" ]]; then
    SCK_OUT=$(python3 -m undertow shadow sample --check "$PREV_TD" 2>&1); SCK_RC=$?
    printf '%s\n' "$SCK_OUT" | head -6
    SCK_MK="data/logs/.sample_alerted_check_${PREV_TD}"          # 一天跑四次：同一交易日只告警一次
    if (( SCK_RC != 0 )) && [[ ! -e "$SCK_MK" ]]; then
        : > "$SCK_MK"
        alert "⚠️ 盘中采样 ${PREV_TD} 有缺失/失败的桶（ET $ET_NOW）" "$(printf '%s' "$SCK_OUT" | grep '⚠️' | head -3 | tr '\n' ' ')"
    fi
fi
set -e

# 候选价差的历史盘中成交价（长桥 1 分钟 K 线，用户 2026-09-27）：补到上一交易日为止。
# 已到期合约约一周后长桥就查不到，所以每天补；已存的不重抓。「查不到」是状态不是失败；
# 网络/CLI 故障 → rc=1 → 告警（数据下次补，不会被当成已抓）。
set +e
# --all（2026-09-28 起）：当天的逐分钟已由 session ⑩ intraday 存下、不再占历史配额 → 月配额（10/1 起补满 400）
# 只花在真正缺的旧日子上，覆盖全部品种与规则。只抓缺的，已存/已记查不到的不重抓。
BARS_OUT=$(python3 -m undertow shadow bars --all --status-file "data/logs/.status_bars_${ET_DATE}.json" 2>&1); BARS_RC=$?
set -e
printf '%s\n' "$BARS_OUT" | grep -E "逐分钟|⚠️" | head -5 || true
if (( BARS_RC == 3 )); then
    # 长桥历史 K 线配额用尽：不是故障，但要让人知道（每个 ET 日只提醒一次，免得一天四次狼来了）
    QUOTA_MARK="data/logs/.bars_quota_${ET_DATE}"
    if [[ ! -e "$QUOTA_MARK" ]]; then
        : > "$QUOTA_MARK"
        alert "ℹ️ 期权分钟线：长桥配额用尽（ET $ET_NOW）" "未补完的下次自动续补；到期约一周后合约会查不到"
    fi
elif (( BARS_RC != 0 )); then
    alert "⚠️ 期权分钟线补抓失败（ET $ET_NOW）" "rc=$BARS_RC：$(printf '%s' "$BARS_OUT" | grep '⚠️' | head -2 | tr '\n' ' ')"
fi

# 只提交【不可再生】的：快照（期权链）+ 台账（data/history）。
# data/reports 里的 HTML/PDF 已 gitignore（可由快照+代码重算），
# 这一行留着是为了捞同目录下的 FAILURE_*/ALERT_* —— .gitignore 的两条 ! 例外，
# 它们是"那天确实失败过"的唯一凭证，漏掉就等于把静默失败造回来。
# Codex 008 G10：原先 `git add` 三个目录后用 `git diff --cached` 判断、再普通 `git commit`——
# 索引里若有他人（交叉工作的代理/人）已暂存的文件，会被一起提交。现在只提交这三个目录；
# 发现目录外的暂存 → 停止发布、数据留在磁盘、告警，下次运行再发（不 stash/reset 他人工作）。
# 009 N01：只提交本次运行产物（运行开始时 publish_begin 已记录运行前状态）。
set +e
PUB=$(publish_dirs "每日自动更新 $(TZ=America/New_York date +%F)：期权链快照+台账（launchd 定时任务）

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" data/snapshots data/history data/reports); PUB_RC=$?
set -e
case $PUB_RC in
  0) echo "[完成] 已提交并推送（或无变更）" ;;
  3) echo "[暂停发布] $PUB"
     alert "⚠️ 每日数据未提交（ET $ET_NOW）" "索引里有他人暂存的文件，已停止自动提交；数据已落盘，下次运行再发" ;;
  5) echo "[暂停发布] $PUB"      # 与 3 分开报：2026-09-30 实为发布冲突，却报成「他人暂存」，查错方向
     alert "⚠️ 每日数据未提交（ET $ET_NOW）" "发布冲突：运行前已有未提交改动的路径本次又被写入（${${PUB#*：}[1,160]}），已停止自动提交；数据已落盘，需人工核对后提交" ;;
  4) echo "[警告] 已提交但推送失败"
     alert "⚠️ 每日数据推送失败（ET $ET_NOW）" "已本地提交，git push 失败；数据未备份到远端" ;;
  *) echo "[失败] 提交失败 rc=$PUB_RC"
     alert "⚠️ 每日数据提交失败（ET $ET_NOW）" "publish_dirs rc=$PUB_RC" ;;
esac
exit 0
