#!/bin/zsh
# undertow 每日自动更新（launchd 定时触发，无 LLM 参与）：
#   快照当日期权链 → 有新数据才出报告 → commit + push（= 备份）
# 时窗守卫：只在 ET 凌晨 1:00–8:59 运行（OCC 隔夜 OI 已更新、美股未开盘），
# 错过窗口（如合盖补跑落到美盘时段）宁可跳过也不落脏数据。
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

cd /Users/yhdong/Trading

ET_NOW=$(TZ=America/New_York date '+%F %H:%M')
ET_HOUR=$((10#$(TZ=America/New_York date +%H)))
echo "==== $(date '+%F %H:%M %Z') | ET $ET_NOW ===="
if (( ET_HOUR < 1 || ET_HOUR >= 9 )); then
    echo "[跳过] ET ${ET_HOUR}时 不在快照窗口(1:00–8:59)——避免旧OI/盘中脏数据"
    exit 0
fi

python3 -m undertow snapshot

# 休市日快照去重会不落盘 → 无新文件就不出报告、不提交，避免垃圾报告
if [[ -z $(git status --porcelain data/snapshots) ]]; then
    echo "[跳过] 无新快照（休市/重复数据），不出报告不提交"
    exit 0
fi

python3 -m undertow report gold silver wti --no-snapshot

git add data/snapshots data/reports data/history
if git diff --cached --quiet; then
    echo "[跳过] 无变更可提交"
    exit 0
fi
git commit -m "每日自动更新 $(TZ=America/New_York date +%F)：期权链快照+三品种报告（launchd 定时任务）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
echo "[完成] 已提交并推送"
