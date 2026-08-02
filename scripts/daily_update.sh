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

# 幂等守卫：当日报告已提交（早前时点已成功）→ 后续重试点直接跳过，省掉重复抓取/出报告
if git cat-file -e "HEAD:data/reports/gold_${ET_DATE}.html" 2>/dev/null; then
    echo "[跳过] 当日报告(${ET_DATE})已提交，本时点无需重复运行"
    exit 0
fi

python3 -m undertow snapshot

# 休市日 / OCC 未结算（OI 与上一交易日逐行相同）→ 指纹去重不落盘 → 无新文件就
# 不出报告、不提交（交给后续重试点）；避免残缺报告（OI 旧、现价新）污染序列
if [[ -z $(git status --porcelain data/snapshots) ]]; then
    echo "[跳过] 无新持仓快照（休市/OI未结算/重复）——等下一时点重试"
    exit 0
fi

python3 -m undertow report gold silver wti qqq --no-snapshot

git add data/snapshots data/reports data/history
if git diff --cached --quiet; then
    echo "[跳过] 无变更可提交"
    exit 0
fi
git commit -m "每日自动更新 $(TZ=America/New_York date +%F)：期权链快照+四品种报告（launchd 定时任务）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
echo "[完成] 已提交并推送"
